"""Minimal real-Redis/uvicorn×4 harness for release verification only."""

import os

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from auth.global_rate_limiter import setup_rate_limit_middleware


app = FastAPI()
setup_rate_limit_middleware(app)


class _InjectVerifiedUser(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.user = {"user_id": 980001, "is_admin": False}
        return await call_next(request)


app.add_middleware(_InjectVerifiedUser)


@app.get("/health")
async def health():
    return {"pid": os.getpid()}


@app.get("/api/ordinary")
async def ordinary():
    return {"pid": os.getpid(), "bucket": "normal"}


@app.get("/api/meijiehezi/publish-history")
async def history():
    return {"pid": os.getpid(), "bucket": "publish_history_read"}
