"""Воспроизводимо экспортировать фактическую OpenAPI-схему FastAPI."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import create_app


def main():
    os.environ.setdefault('MAX_WEBHOOK_SECRET', 'openapi-export-only')
    schema = create_app(':memory:').openapi()
    (ROOT / 'openapi.json').write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )


if __name__ == '__main__':
    main()
