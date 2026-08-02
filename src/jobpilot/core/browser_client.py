"""HTTP client for the browser daemon. The only way core touches the browser."""

from __future__ import annotations

import httpx


class BrowserDaemonError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BrowserDaemonDown(BrowserDaemonError):
    pass


class BrowserClient:
    def __init__(self, base_url: str, timeout: float = 90.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TransportError as error:
            raise BrowserDaemonDown(
                f"browser daemon unreachable: {error}"
            ) from error
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("detail", "")
            except ValueError:
                detail = response.text
            raise BrowserDaemonError(
                f"{method} {path}: {response.status_code} {detail}",
                status_code=response.status_code,
            )
        return response

    def health(self) -> dict:
        return self._request("GET", "/health").json()

    def open_tab(self, job_id: int, url: str) -> dict:
        return self._request(
            "POST", "/tabs", json={"job_id": job_id, "url": url}
        ).json()

    def focus(self, job_id: int) -> None:
        self._request("POST", f"/tabs/{job_id}/focus")

    def snapshot(self, job_id: int) -> dict:
        return self._request("GET", f"/tabs/{job_id}/snapshot").json()

    def archive(self, job_id: int) -> dict:
        return self._request("GET", f"/tabs/{job_id}/archive").json()

    def screenshot(self, job_id: int, selector: str | None = None) -> bytes:
        params = {"selector": selector} if selector else {}
        return self._request("GET", f"/tabs/{job_id}/screenshot", params=params).content

    def render_pdf(self, html: str) -> bytes:
        return self._request("POST", "/render/pdf", json={"html": html}).content

    def close_tab(self, job_id: int) -> None:
        self._request("DELETE", f"/tabs/{job_id}")

    def close(self) -> None:
        self._client.close()
