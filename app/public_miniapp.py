"""Узкий публичный вход для Mini App, без остальных маршрутов API."""
import os
import re
import secrets
from pathlib import Path

import uvicorn
from fastapi import Request
from fastapi.responses import PlainTextResponse

from .api import create_app
from .polling import load_env


PUBLIC_GET = (
    re.compile(r'^/miniapp$'),
    re.compile(r'^/miniapp/assets/(?:app\.js|style\.css)$'),
    re.compile(r'^/api/miniapp/overview$'),
    re.compile(r'^/api/tickets/[^/]+$'),
)
PUBLIC_POST = (
    re.compile(r'^/api/miniapp/tickets/[^/]+/priority$'),
    re.compile(r'^/api/miniapp/incidents$'),
    re.compile(r'^/api/miniapp/login$'),
    re.compile(r'^/api/tickets/[^/]+/(?:comments|status)$'),
)


def create_public_app():
    app = create_app()

    @app.middleware('http')
    async def only_miniapp(request: Request, call_next):
        patterns = PUBLIC_GET if request.method == 'GET' else PUBLIC_POST if request.method == 'POST' else ()
        if not any(pattern.fullmatch(request.url.path) for pattern in patterns):
            return PlainTextResponse('Не найдено', status_code=404)
        if request.url.path.startswith('/api/'):
            if request.headers.get('Authorization'):
                return PlainTextResponse('Требуется вход через MAX', status_code=401)
            if request.url.path != '/api/miniapp/login' and not (
                request.headers.get('X-Max-Init-Data') or request.headers.get('X-Miniapp-Session')
            ):
                return PlainTextResponse('Требуется вход через MAX', status_code=401)
        return await call_next(request)

    return app


def main():
    load_env(Path('.env'))
    if not os.getenv('MAX_WEBHOOK_SECRET'):
        os.environ['MAX_WEBHOOK_SECRET'] = secrets.token_urlsafe(32)
    uvicorn.run('app.public_miniapp:create_public_app', factory=True, host='127.0.0.1', port=8081)


if __name__ == '__main__':
    main()
