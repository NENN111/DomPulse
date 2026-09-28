"""Узкий публичный вход для Mini App, без остальных маршрутов API."""
import os
import re
import secrets
from pathlib import Path

import uvicorn
from fastapi import Request
from fastapi.responses import PlainTextResponse, Response

from .api import create_app
from .miniapp_document import miniapp_response
from .polling import load_env


PUBLIC_GET = (
    re.compile(r'^/miniapp$'),
    re.compile(r'^/miniapp/assets/(?:app\.js|style\.css)$'),
    re.compile(r'^/api/miniapp/overview$'),
    re.compile(r'^/api/tickets/[^/]+$'),
    re.compile(r'^/health$'),
    re.compile(r'^/openapi.json$'),
    re.compile(r'^/api/me$'),
    re.compile(r'^/api/tickets$'),
    re.compile(r'^/api/metrics/house$'),
    re.compile(r'^/api/houses/[^/]+/announcements$'),
)
PUBLIC_POST = (
    re.compile(r'^/api/tickets$'),
    re.compile(r'^/api/miniapp/tickets/[^/]+/priority$'),
    re.compile(r'^/api/miniapp/incidents$'),
    re.compile(r'^/api/miniapp/login$'),
    re.compile(r'^/api/tickets/[^/]+/(?:comments|status)$'),
)


BEARER_GET = (
    re.compile(r'^/api/me$'),
    re.compile(r'^/api/tickets$'),
    re.compile(r'^/api/tickets/[^/]+$'),
    re.compile(r'^/api/metrics/house$'),
    re.compile(r'^/api/houses/[^/]+/announcements$'),
)
BEARER_POST = (
    re.compile(r'^/api/tickets$'),
    re.compile(r'^/api/tickets/[^/]+/(?:comments|status)$'),
)


def create_public_app():
    app = create_app()

    @app.middleware('http')
    async def only_miniapp(request: Request, call_next):
        if request.url.path in {'/', '/miniapp'}:
            if request.method == 'HEAD':
                return Response(status_code=200, headers={'Cache-Control': 'no-store'})
            if request.method == 'GET' and request.url.path == '/':
                return miniapp_response()
        patterns = PUBLIC_GET if request.method == 'GET' else PUBLIC_POST if request.method == 'POST' else ()
        if not any(pattern.fullmatch(request.url.path) for pattern in patterns):
            return PlainTextResponse('Не найдено', status_code=404)
        if request.url.path.startswith('/api/'):
            bearer = request.headers.get('Authorization')
            bearer_patterns = BEARER_GET if request.method == 'GET' else BEARER_POST if request.method == 'POST' else ()
            if bearer and not any(pattern.fullmatch(request.url.path) for pattern in bearer_patterns):
                return PlainTextResponse('Требуется вход через MAX', status_code=401)
            if not bearer and request.url.path != '/api/miniapp/login' and not (
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
