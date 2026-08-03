"""The daemon owning the single persistent Chromium context.

One tab per job, registry mirrored to job.tab_state so a restart can report
lost tabs. All DOM access anywhere in the system goes through this process.
PDF rendering uses a transient headless browser because Chromium cannot print
to PDF from a headed window; it renders the already-archived HTML, so the PDF
matches the archive byte for byte.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from ..config import Config, DataPaths
from ..db import open_db
from ..core import events, repo, state
from . import archive, focus, verbs
import platform


class TabCapReached(Exception):
    pass


class TabNotFound(Exception):
    pass


class BrowserDaemon:
    def __init__(self, config: Config, paths: DataPaths, headless: bool = False):
        self.config = config
        self.paths = paths
        self.headless = headless
        self.tabs: dict[int, Page] = {}
        self._closing: set[int] = set()
        self._playwright = None
        self._context: BrowserContext | None = None
        self._conn = None

    def _keeps_focus(self) -> bool:
        return (
            self.config.browser.focus_policy == "notify_only" and not self.headless
        )

    async def start(self) -> None:
        self._conn = open_db(self.paths, self.config)
        self._mark_lost_tabs()
        self.paths.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        previous = await focus.front_bundle() if self._keeps_focus() else None
        self._playwright = await async_playwright().start()

        brave_executable = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

        options = {
            "user_data_dir": str(self.paths.browser_profile_dir),
            "headless": self.headless,
            # Playwright disables Chrome's sandbox by default. This browser
            # runs third-party job-site code, so the sandbox stays on.
            "chromium_sandbox": True,
            "channel": "chrome",
            "executable_path": brave_executable, # Point directly to Brave
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--enable-gpu",
                "--enable-webgl",
                "--use-gl=angle",
                "--use-angle=metal",
                "--disable-brave-extension",
                "--fingerprinting-canvas-measurements=disabled",
                "--fingerprinting-canvas-image-data-noise=disabled",
                "--enable-features=UserAgentClientHint"
            ],
            "ignore_default_args": [
                "--enable-unsafe-swiftshader",
                "--disable-extensions",
                "--disable-default-apps",
                "--disable-component-extensions-with-background-pages",
                "--disable-field-trial-config",
                "--use-mock-keychain"
            ]
        }

        user_data_dir = options.pop('user_data_dir')
        channel = options.get("channel", None)

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir,
                **options
            )
            assert self._context is not None, "Browser context is not initialized"

            await self._context.add_init_script("""
                (() => {
                    // 1. Hide navigator.webdriver on Prototype
                    Object.defineProperty(Object.getPrototypeOf(navigator), 'webdriver', {
                        get: () => undefined,
                        enumerable: true,
                        configurable: true
                    });

                    // 2. Define standard Chrome Brands
                    const chromeBrands = [
                        { brand: 'Not A;Brand', version: '99' },
                        { brand: 'Chromium', version: '151' },
                        { brand: 'Google Chrome', version: '151' }
                    ];

                    // Standard Brave Client Hints (Brave reports both Chromium and Brave brands)
                    const braveBrands = [
                        { brand: 'Not A;Brand', version: '99' },
                        { brand: 'Chromium', version: '131' },
                        { brand: 'Brave', version: '131' }
                    ];

                    // 3. Robust userAgentData override (Works even if Brave disabled NavigatorUAData)
                    const mockUserAgentData = {
                        brands: braveBrands,
                        mobile: false,
                        platform: 'macOS',
                        getHighEntropyValues: async (hints) => ({
                            brands: chromeBrands,
                            mobile: false,
                            platform: 'macOS',
                            platformVersion: '15.0.0',
                            architecture: 'arm',
                            bitness: '64',
                            model: '',
                            uaFullVersion: '151.0.0.0'
                        }),
                        toJSON: () => ({
                            brands: chromeBrands,
                            mobile: false,
                            platform: 'macOS'
                        })
                    };

                    // Override userAgentData directly on Navigator prototype
                    Object.defineProperty(Navigator.prototype, 'userAgentData', {
                        get: () => mockUserAgentData,
                        enumerable: true,
                        configurable: true
                    });
                })();
            """)
        except Exception:
            if not channel:
                raise
            # The configured channel is not installed here; the bundled build
            # still works, with software rendering.
            options.pop("channel")
            self._context = await self._playwright.chromium.launch_persistent_context(
                **options
            )
        if previous:
            asyncio.get_running_loop().create_task(focus.restore(previous))

    async def stop(self) -> None:
        for job_id in list(self.tabs):
            try:
                await self.close_tab(job_id)
            except Exception:
                pass
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()
        if self._conn is not None:
            self._conn.close()

    def _mark_lost_tabs(self) -> None:
        """Tabs recorded as open belong to a browser that is gone. Ones on
        jobs still in flight are lost and worth replaying; past submission
        the tab has no further use, so it is simply closed."""
        for job in repo.jobs_with_tab_state(self._conn, "open"):
            gone = "none" if job.status in state.TAB_IRRELEVANT else "lost"
            repo.set_tab_state(self._conn, job.id, gone, "browserd")

    def open_tab_count(self) -> int:
        return len(self.tabs)

    def open_tab_ids(self) -> list[int]:
        return sorted(self.tabs)

    async def open_tab(self, job_id: int, url: str) -> dict:
        repo.get_job(self._conn, job_id)
        page = self.tabs.get(job_id)
        previous = None
        if page is None:
            if len(self.tabs) >= self.config.browser.tab_cap:
                raise TabCapReached(
                    f"tab cap of {self.config.browser.tab_cap} reached; "
                    f"open tabs: {self.open_tab_ids()}"
                )
            # Opening a window is what activates the browser on macOS;
            # renavigating an existing tab does not.
            previous = await focus.front_bundle() if self._keeps_focus() else None
            page = await self._context.new_page()
            self.tabs[job_id] = page
            page.on("close", lambda _page, job_id=job_id: self._on_page_closed(job_id))
        await self._navigate(page, url)
        repo.set_tab_state(self._conn, job_id, "open", "browserd")
        if previous:
            asyncio.get_running_loop().create_task(focus.restore(previous))
        return {"url": page.url, "title": await page.title()}

    async def _navigate(self, page: Page, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(self.config.browser.settle_ms / 1000)

    def _page(self, job_id: int) -> Page:
        page = self.tabs.get(job_id)
        if page is None:
            raise TabNotFound(f"no open tab for job {job_id}")
        return page

    def _on_page_closed(self, job_id: int) -> None:
        self.tabs.pop(job_id, None)
        if job_id in self._closing:
            self._closing.discard(job_id)
            return
        # Closed outside the daemon (owner closed the tab or the browser).
        try:
            repo.set_tab_state(self._conn, job_id, "lost", "browserd")
        except Exception:
            pass

    async def focus(self, job_id: int) -> None:
        await self._page(job_id).bring_to_front()

    async def snapshot(self, job_id: int) -> dict:
        page = self._page(job_id)
        tree = await page.locator("body").aria_snapshot()
        return {"a11y_tree": tree, "url": page.url, "title": await page.title()}

    async def archive_page(self, job_id: int) -> dict:
        page = self._page(job_id)
        html = await page.content()

        async def fetch(url: str):
            try:
                response = await self._context.request.get(url, timeout=15000)
                if not response.ok:
                    return None
                body = await response.body()
                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip()
                    or None
                )
                return body, content_type
            except Exception:
                return None

        inlined = await archive.inline_html(html, page.url, fetch)
        # raw_html keeps script tags and embed markers that the inlined
        # archive drops; board fingerprinting reads it.
        return {
            "html": inlined,
            "raw_html": html,
            "url": page.url,
            "title": await page.title(),
        }

    async def screenshot(self, job_id: int, selector: str | None = None) -> bytes:
        page = self._page(job_id)
        if selector:
            locator = page.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            return await locator.screenshot(type="png")
        return await page.screenshot(type="png", full_page=True)

    async def render_pdf(self, html: str) -> bytes:
        browser: Browser = await self._playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="load")
            return await page.pdf(format="A4", print_background=True)
        finally:
            await browser.close()

    async def resolve(self, job_id: int, form_schema: dict) -> dict:
        page = self._page(job_id)
        result = await verbs.resolve_schema(page, form_schema)
        events.append(
            self._conn, actor="browserd", kind="selectors_resolved", job_id=job_id,
            payload={
                "resolved": len(result["resolved"]),
                "unresolved": result["unresolved"],
            },
        )
        self._conn.commit()
        return result

    async def apply(self, job_id: int, actions: list[dict], selector_map: dict) -> list[dict]:
        page = self._page(job_id)
        results = await verbs.apply_actions(page, actions, selector_map)
        if any(action.get("verb") == "click_mapped" for action in actions):
            # A mapped click can navigate or reveal content; let it settle so
            # the caller reads the page it produced.
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(self.config.browser.settle_ms / 1000)
        events.append(
            self._conn, actor="browserd", kind="verbs_applied", job_id=job_id,
            payload={
                "actions": len(results),
                "failed": [r["field_id"] for r in results if not r["ok"]],
            },
        )
        self._conn.commit()
        return results

    async def validate(self, job_id: int, fields: list[dict], selector_map: dict) -> dict:
        page = self._page(job_id)
        return await verbs.validate_fields(page, fields, selector_map)

    async def crop(self, job_id: int, descriptor: dict) -> bytes:
        page = self._page(job_id)
        return await verbs.crop(page, descriptor)

    async def challenge(self, job_id: int) -> dict:
        return await verbs.detect_challenge(self._page(job_id))

    async def page_state(self, job_id: int) -> dict:
        page = self._page(job_id)
        return {"url": page.url, "title": await page.title()}

    async def close_tab(self, job_id: int) -> None:
        page = self._page(job_id)
        self._closing.add(job_id)
        await page.close()
        self.tabs.pop(job_id, None)
        repo.set_tab_state(self._conn, job_id, "none", "browserd")
