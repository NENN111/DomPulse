"""Локальный API и предпросмотр Mini App с настройками из .env."""
from pathlib import Path
import os
import secrets

import uvicorn

from .polling import load_env


def main():
    load_env(Path('.env'))
    if not os.getenv('MAX_WEBHOOK_SECRET'):
        os.environ['MAX_WEBHOOK_SECRET'] = secrets.token_urlsafe(32)
    uvicorn.run('app.api:create_app', factory=True, host='127.0.0.1', port=8080)


if __name__ == '__main__':
    main()
