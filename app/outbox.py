import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

from .db import AsyncDatabase
from .max_api import MaxAPIError, MaxClient
from .sla import scan_overdue


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def without_unavailable_app(attachments: list[dict]) -> list[dict] | None:
    """Keep the menu usable while MAX has no Mini App URL for the bot."""
    changed = False
    fallback = []
    for attachment in attachments:
        if attachment.get('type') != 'inline_keyboard':
            fallback.append(attachment)
            continue
        payload = attachment.get('payload', {})
        rows = []
        for row in payload.get('buttons', []):
            buttons = [button for button in row if button.get('type') != 'open_app']
            changed |= len(buttons) != len(row)
            if buttons:
                rows.append(buttons)
        fallback.append({**attachment, 'payload': {**payload, 'buttons': rows}})
    return fallback if changed else None


async def deliver_one(db: AsyncDatabase, client: MaxClient) -> bool:
    now = utc_now()
    lease_expired_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    async with db.connect(write=True) as conn:
        cursor = await conn.execute(
            "SELECT * FROM max_outbox WHERE "
            "(status='pending' AND next_attempt_at<=?) OR "
            "(status='failed' AND retryable=1 AND next_attempt_at<=?) OR "
            "(status='sending' AND COALESCE(sending_at, created_at)<=?) "
            'ORDER BY id LIMIT 1',
            (now, now, lease_expired_at),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        message = dict(row)
        await conn.execute(
            "UPDATE max_outbox SET status='sending',attempts=attempts+1,sending_at=?,error=NULL WHERE id=?",
            (now, message['id']),
        )

    attachments = json.loads(message['attachments_json'])
    delivered_text = message['text']
    try:
        try:
            mid = await client.send_message(message['max_user_id'], delivered_text, attachments)
        except MaxAPIError as exc:
            fallback = without_unavailable_app(attachments)
            if 'MAX /messages: HTTP 400' not in str(exc) or fallback is None:
                raise
            delivered_text += '\n\nМини-приложение пока не подключено в настройках MAX.'
            attachments = fallback
            mid = await client.send_message(message['max_user_id'], delivered_text, attachments)
    except MaxAPIError as exc:
        retryable = int(exc.retryable)
        delay_seconds = min(300, 2 ** min(message['attempts'] + 1, 8))
        retry_at = (
            (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
            if retryable else '9999-12-31T23:59:59+00:00'
        )
        async with db.connect(write=True) as conn:
            await conn.execute(
                "UPDATE max_outbox SET status='failed',retryable=?,sending_at=NULL,next_attempt_at=?,error=? WHERE id=?",
                (retryable, retry_at, str(exc)[:1000], message['id']),
            )
        return True

    async with db.connect(write=True) as conn:
        await conn.execute(
            "UPDATE max_outbox SET status='sent',sending_at=NULL,max_mid=?,sent_at=?,text=?,attachments_json=? WHERE id=?",
            (mid, utc_now(), delivered_text, json.dumps(attachments, ensure_ascii=False), message['id']),
        )
    return True


async def run_worker(db: AsyncDatabase, client: MaxClient):
    last_sla_scan = 0.0
    while True:
        if time.monotonic() - last_sla_scan >= 15:
            await scan_overdue(db)
            last_sla_scan = time.monotonic()
        # Finish an in-flight delivery when Ctrl+C stops the combined runner.
        delivery = asyncio.create_task(deliver_one(db, client))
        try:
            delivered = await asyncio.shield(delivery)
        except asyncio.CancelledError:
            await delivery
            raise
        if not delivered:
            await asyncio.sleep(1)


async def main():
    required = ('MAX_BOT_TOKEN', 'MAX_API_BASE', 'DOMPULSE_DB')
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Required configuration is missing: {', '.join(missing)}")
    token = os.environ['MAX_BOT_TOKEN']
    base_url = os.environ['MAX_API_BASE']
    db = AsyncDatabase(os.environ['DOMPULSE_DB'])
    await db.initialize()
    async with MaxClient(token, base_url) as client:
        await run_worker(db, client)


if __name__ == '__main__':
    asyncio.run(main())
