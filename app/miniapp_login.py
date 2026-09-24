"""Одноразовый вход оператора в сайт через чат MAX."""
import secrets
from datetime import datetime, timedelta, timezone

from .db import token_hash

ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
CODE_MINUTES = 10
SESSION_HOURS = 8


def normalize_code(value: str) -> str:
    return ''.join(char for char in value.upper() if char not in ' -')


async def issue_code(conn, max_user_id: int) -> str:
    now = datetime.now(timezone.utc)
    code = ''.join(secrets.choice(ALPHABET) for _ in range(10))
    await conn.execute('DELETE FROM miniapp_login_codes WHERE expires_at<=?', (now.isoformat(),))
    await conn.execute(
        'INSERT INTO miniapp_login_codes(code_hash,max_user_id,expires_at,created_at) VALUES(?,?,?,?)',
        (token_hash(code), max_user_id, (now + timedelta(minutes=CODE_MINUTES)).isoformat(), now.isoformat()),
    )
    return code[:5] + '-' + code[5:]


async def redeem_code(db, supplied: str) -> tuple[str, str] | None:
    code = normalize_code(supplied)
    if len(code) != 10 or any(char not in ALPHABET for char in code):
        return None
    now = datetime.now(timezone.utc)
    async with db.connect(write=True) as conn:
        row = await (await conn.execute(
            'SELECT c.max_user_id FROM miniapp_login_codes c '
            'JOIN max_links l ON l.max_user_id=c.max_user_id '
            'JOIN users u ON u.id=l.user_id '
            'WHERE c.code_hash=? AND c.used_at IS NULL AND c.expires_at>? AND u.role=?',
            (token_hash(code), now.isoformat(), 'operator'),
        )).fetchone()
        if row is None:
            return None
        await conn.execute('UPDATE miniapp_login_codes SET used_at=? WHERE code_hash=?',
                           (now.isoformat(), token_hash(code)))
        token = secrets.token_urlsafe(32)
        expires = (now + timedelta(hours=SESSION_HOURS)).isoformat()
        await conn.execute(
            'INSERT INTO miniapp_sessions(token_hash,max_user_id,expires_at,created_at) VALUES(?,?,?,?)',
            (token_hash(token), row['max_user_id'], expires, now.isoformat()),
        )
        await conn.execute('DELETE FROM miniapp_sessions WHERE expires_at<=?', (now.isoformat(),))
        return token, expires


async def session_user_id(db, token: str) -> int | None:
    if not token or len(token) > 128:
        return None
    async with db.connect() as conn:
        row = await (await conn.execute(
            'SELECT max_user_id FROM miniapp_sessions WHERE token_hash=? AND expires_at>?',
            (token_hash(token), datetime.now(timezone.utc).isoformat()),
        )).fetchone()
    return row['max_user_id'] if row else None
