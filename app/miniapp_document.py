"""Build the public Mini App page with local assets in one response."""
import re
from functools import lru_cache
from pathlib import Path

from fastapi.responses import HTMLResponse

WEB_DIR = Path(__file__).resolve().parent.parent / 'web'
CSS_TAG = re.compile(r'<link rel="stylesheet" href="/miniapp/assets/style\.css\?v=[^"]+">')
JS_TAG = re.compile(r'<script src="/miniapp/assets/app\.js\?v=[^"]+" defer></script>')


@lru_cache(maxsize=1)
def miniapp_document() -> str:
    html = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
    css = (WEB_DIR / 'style.css').read_text(encoding='utf-8')
    js = (WEB_DIR / 'app.js').read_text(encoding='utf-8')
    if '</style' in css.lower() or '</script' in js.lower():
        raise ValueError('Mini App assets contain an HTML closing tag')
    if len(CSS_TAG.findall(html)) != 1 or len(JS_TAG.findall(html)) != 1:
        raise ValueError('Mini App asset tags were not found')
    html = CSS_TAG.sub(lambda _: '<style>\n' + css + '\n</style>', html, count=1)
    html = JS_TAG.sub(
        lambda _: '<script>document.addEventListener("DOMContentLoaded", () => {\n'
        + js + '\n});</script>',
        html, count=1,
    )
    return html


def miniapp_response() -> HTMLResponse:
    return HTMLResponse(miniapp_document(), headers={'Cache-Control': 'no-store'})