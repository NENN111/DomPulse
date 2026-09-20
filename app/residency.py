"""Безопасная привязка жителя по подтверждённому адресу регистрации."""

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .db import token_hash


def normalize_house_address(value: str) -> str:
    """Нормализует адрес для строгого сопоставления с известным домом."""
    normalized = value.casefold().replace('ё', 'е')
    normalized = re.sub(r'[.,;:]+', ' ', normalized)
    return re.sub(r'\s+', ' ', normalized).strip()


def integration_enabled() -> bool:
    return bool(os.getenv('GOSUSLUGI_BRIDGE_URL'))


def display_name(payload: dict, max_user_id: int) -> str:
    source = payload.get('user') or ((payload.get('message') or {}).get('sender') or {})
    name = source.get('name') if isinstance(source, dict) else None
    return name or f'Пользователь MAX {max_user_id}'


def append_state(url: str, state: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['state'] = state
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


async def begin_verified_link(conn, max_user_id: int, name: str, timestamp: str) -> str | None:
    if not integration_enabled():
        return None
    state = secrets.token_urlsafe(32)
    expires_at = (datetime.fromisoformat(timestamp) + timedelta(minutes=15)).isoformat()
    await conn.execute(
        "UPDATE gosuslugi_link_attempts SET status='cancelled' "
        "WHERE max_user_id=? AND status='pending'", (max_user_id,),
    )
    await conn.execute(
        'INSERT INTO gosuslugi_link_attempts('
        'state_hash,max_user_id,display_name,status,expires_at,created_at) VALUES(?,?,?,?,?,?)',
        (token_hash(state), max_user_id, name, 'pending', expires_at, timestamp),
    )
    return append_state(os.environ['GOSUSLUGI_BRIDGE_URL'], state)


async def resolve_house(conn, house_address: str, fias_house_id: str | None):
    if fias_house_id:
        cursor = await conn.execute(
            "SELECT h.* FROM house_identities i JOIN houses h ON h.id=i.house_id "
            "WHERE i.provider='fias' AND i.external_id=?", (fias_house_id,),
        )
        house = await cursor.fetchone()
        if house:
            return house
    expected = normalize_house_address(house_address)
    cursor = await conn.execute('SELECT * FROM houses')
    matches = [row for row in await cursor.fetchall() if normalize_house_address(row['address']) == expected]
    return matches[0] if len(matches) == 1 else None


def select_registrations(registrations: list[dict], today: str) -> list[dict]:
    active = [
        item for item in registrations
        if item.get('type') in {'temporary', 'permanent'}
        and (not item.get('valid_until') or str(item['valid_until']) >= today)
    ]
    return sorted(active, key=lambda item: item['type'] != 'temporary')


async def complete_verified_link(conn, claim: dict, timestamp: str):
    cursor = await conn.execute(
        'SELECT * FROM gosuslugi_link_attempts WHERE state_hash=?',
        (token_hash(claim['state']),),
    )
    attempt = await cursor.fetchone()
    if attempt is None or attempt['status'] != 'pending':
        return 'invalid', None
    if attempt['expires_at'] <= timestamp:
        await conn.execute(
            "UPDATE gosuslugi_link_attempts SET status='expired' WHERE state_hash=?",
            (attempt['state_hash'],),
        )
        return 'expired', None

    selected = None
    house = None
    for registration in select_registrations(claim['registrations'], timestamp[:10]):
        house = await resolve_house(
            conn, registration['house_address'], registration.get('fias_house_id'),
        )
        if house:
            selected = registration
            break
    if house is None:
        await conn.execute(
            "UPDATE gosuslugi_link_attempts SET status='unmatched',completed_at=? WHERE state_hash=?",
            (timestamp, attempt['state_hash']),
        )
        await queue_link_message(
            conn, attempt['max_user_id'],
            'Подтверждённый адрес не найден среди подключённых домов. Введите адрес вручную.',
            [['Ввести адрес вручную'], ['У меня есть код']], timestamp,
        )
        return 'unmatched', None

    cursor = await conn.execute('SELECT 1 FROM max_links WHERE max_user_id=?', (attempt['max_user_id'],))
    if await cursor.fetchone():
        return 'already_linked', None
    subject_hash = hmac.new(
        os.environ['GOSUSLUGI_BRIDGE_SECRET'].encode(), claim['subject_id'].encode(), hashlib.sha256,
    ).hexdigest()
    cursor = await conn.execute(
        "SELECT 1 FROM residency_verifications WHERE provider='gosuslugi' AND subject_hash=?",
        (subject_hash,),
    )
    if await cursor.fetchone():
        return 'subject_linked', None

    internal_id = f"max-resident-{attempt['max_user_id']}"
    await conn.execute(
        'INSERT INTO users(id,name,role,house_id,token_hash) VALUES(?,?,?,?,?)',
        (internal_id, attempt['display_name'], 'resident', house['id'],
         token_hash(secrets.token_urlsafe(32))),
    )
    await conn.execute(
        'INSERT INTO max_links(max_user_id,user_id,linked_at) VALUES(?,?,?)',
        (attempt['max_user_id'], internal_id, timestamp),
    )
    await conn.execute(
        'INSERT INTO residency_verifications('
        'user_id,provider,subject_hash,registration_type,house_address,fias_house_id,verified_at) '
        'VALUES(?,?,?,?,?,?,?)',
        (internal_id, 'gosuslugi', subject_hash, selected['type'], house['address'],
         selected.get('fias_house_id'), timestamp),
    )
    if selected.get('fias_house_id'):
        await conn.execute(
            "INSERT OR IGNORE INTO house_identities(house_id,provider,external_id) VALUES(?,'fias',?)",
            (house['id'], selected['fias_house_id']),
        )
    await conn.execute(
        "UPDATE gosuslugi_link_attempts SET status='completed',completed_at=? WHERE state_hash=?",
        (timestamp, attempt['state_hash']),
    )
    await queue_link_message(
        conn, attempt['max_user_id'],
        f"Адрес подтверждён через Госуслуги. Профиль привязан к дому: {house['address']}.",
        [['Сообщить о проблеме'], ['Мои обращения']], timestamp,
    )
    return 'completed', dict(house)


async def queue_link_message(conn, max_user_id: int, text: str, rows: list[list[str]], timestamp: str):
    attachments = [{
        'type': 'inline_keyboard',
        'payload': {'buttons': [[{'type': 'message', 'text': label} for label in row] for row in rows]},
    }]
    await conn.execute(
        'INSERT INTO max_outbox(max_user_id,text,attachments_json,next_attempt_at,created_at) '
        'VALUES(?,?,?,?,?)',
        (max_user_id, text, json.dumps(attachments, ensure_ascii=False), timestamp, timestamp),
    )
