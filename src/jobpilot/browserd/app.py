"""FastAPI wrapper over the daemon: validate, call, map errors to HTTP."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from ..config import Config, DataPaths
from ..core.repo import NotFound
from .daemon import BrowserDaemon, TabCapReached, TabNotFound


class TabOpenRequest(BaseModel):
    job_id: int
    url: str


class RenderPdfRequest(BaseModel):
    html: str


def create_app(config: Config, paths: DataPaths, headless: bool = False) -> FastAPI:
    daemon = BrowserDaemon(config, paths, headless=headless)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await daemon.start()
        try:
            yield
        finally:
            await daemon.stop()

    app = FastAPI(title="jobpilot-browserd", lifespan=lifespan)
    app.state.daemon = daemon

    def translate(error: Exception) -> HTTPException:
        if isinstance(error, NotFound):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, TabNotFound):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, TabCapReached):
            return HTTPException(status_code=409, detail=str(error))
        return HTTPException(status_code=500, detail=str(error))

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "open_tabs": daemon.open_tab_ids()}

    @app.post("/tabs")
    async def open_tab(request: TabOpenRequest) -> dict:
        try:
            return await daemon.open_tab(request.job_id, request.url)
        except Exception as error:
            raise translate(error) from error

    @app.post("/tabs/{job_id}/focus")
    async def focus(job_id: int) -> dict:
        try:
            await daemon.focus(job_id)
        except Exception as error:
            raise translate(error) from error
        return {"focused": job_id}

    @app.get("/tabs/{job_id}/snapshot")
    async def snapshot(job_id: int) -> dict:
        try:
            return await daemon.snapshot(job_id)
        except Exception as error:
            raise translate(error) from error

    @app.get("/tabs/{job_id}/archive")
    async def archive_page(job_id: int) -> dict:
        try:
            return await daemon.archive_page(job_id)
        except Exception as error:
            raise translate(error) from error

    @app.get("/tabs/{job_id}/screenshot")
    async def screenshot(job_id: int, selector: str | None = None) -> Response:
        try:
            image = await daemon.screenshot(job_id, selector)
        except Exception as error:
            raise translate(error) from error
        return Response(content=image, media_type="image/png")

    @app.post("/render/pdf")
    async def render_pdf(request: RenderPdfRequest) -> Response:
        try:
            pdf = await daemon.render_pdf(request.html)
        except Exception as error:
            raise translate(error) from error
        return Response(content=pdf, media_type="application/pdf")

    @app.delete("/tabs/{job_id}")
    async def close_tab(job_id: int) -> dict:
        try:
            await daemon.close_tab(job_id)
        except Exception as error:
            raise translate(error) from error
        return {"closed": job_id}

    return app
