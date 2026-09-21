"""Local MAX bot: python -m app.polling (no HTTP server required)."""
import argparse
import asyncio
import logging
import os
import shlex
import sys
from pathlib import Path

from .db import AsyncDatabase
from .max_api import MaxAPIError, MaxClient
from .max_webhook import store_update_in_transaction
from .outbox import run_worker

logger = logging.getLogger(__name__)


def python_command(*args: str) -> str:
    arguments = [sys.executable, *args]
    if os.name == 'nt':
        return '& ' + ' '.join("'" + value.replace("'", "''") + "'" for value in arguments)
    return shlex.join(arguments)


def load_env(path: Path):
    """Read literal KEY=value settings; shell environment takes precedence.

    Supports blank lines, full-line comments and optional matching quotes.
    No interpolation or shell execution. Only bot settings are imported.
    """
    if not path.exists():
        return
    allowed = {'DOMPULSE_DB', 'MAX_BOT_TOKEN', 'MAX_API_BASE', 'YANDEX_MAPS_API_KEY', 'YANDEX_GEOCODER_URL'}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if key not in allowed:
            continue
        if not separator:
            raise ValueError(f'.env: ожидается KEY=value в строке {number}')
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f'.env: незакрытая кавычка в строке {number}')
            value = value[1:-1]
        os.environ.setdefault(key, value)


async def prepare_polling(client: MaxClient, delete_webhooks: bool):
    subscriptions = await client.subscriptions()
    if subscriptions and not delete_webhooks:
        raise RuntimeError(
            'У бота подключён webhook. Чтобы отключить его и перейти на локальный режим, '
            'запустите: ' + python_command('-m', 'app.polling', '--delete-webhooks')
        )
    for subscription in subscriptions:
        await client.delete_subscription(subscription['url'])
    if subscriptions and await client.subscriptions():
        raise RuntimeError('Webhook ещё активен; повторите переключение позже.')


async def poll_once(db: AsyncDatabase, client: MaxClient, bot_id: int) -> int:
    async with db.connect() as conn:
        cursor = await conn.execute('SELECT marker FROM max_polling_state WHERE bot_id=?', (bot_id,))
        row = await cursor.fetchone()
        marker = row['marker'] if row else None
    page = await client.get_updates(marker)
    async with db.connect(write=True) as conn:
        for update in page['updates']:
            if update['update_type'] in {'message_created', 'bot_started'}:
                await store_update_in_transaction(conn, update)
        # Commit replies, dialog changes and cursor together. Replays are deduplicated.
        next_marker = page['marker'] if page['marker'] is not None else marker
        await conn.execute(
            'INSERT INTO max_polling_state(bot_id,marker) VALUES(?,?) '
            'ON CONFLICT(bot_id) DO UPDATE SET marker=excluded.marker',
            (bot_id, next_marker),
        )
    return len(page['updates'])


async def poll_forever(db: AsyncDatabase, client: MaxClient, bot_id: int):
    delay = 1
    while True:
        try:
            count = await poll_once(db, client, bot_id)
        except MaxAPIError as exc:
            if not exc.retryable:
                raise
            logger.warning('%s. Повтор через %s с.', exc, delay)
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)
            continue
        delay = 1
        if not count:
            # Avoid a tight loop when the server responds immediately with an empty page.
            await asyncio.sleep(1)


async def run_bot(db: AsyncDatabase, client: MaxClient, bot_id: int):
    tasks = [asyncio.create_task(poll_forever(db, client, bot_id)),
             asyncio.create_task(run_worker(db, client))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            if task.done():
                task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main(env_file: Path = Path('.env'), delete_webhooks: bool = False):
    load_env(env_file)
    token = os.getenv('MAX_BOT_TOKEN', '').strip()
    if not token:
        raise ValueError('Заполните MAX_BOT_TOKEN в .env или переменных окружения.')
    db = AsyncDatabase(os.getenv('DOMPULSE_DB') or '.local/dompulse.db')
    await db.initialize()
    async with db.connect() as conn:
        cursor = await conn.execute('SELECT 1 FROM houses LIMIT 1')
        if await cursor.fetchone() is None:
            raise ValueError(
                'В базе нет домов. Создайте тестовые данные: '
                + python_command('-m', 'app.seed', '--db', db.path)
            )
    async with MaxClient(token, os.getenv('MAX_API_BASE') or 'https://platform-api2.max.ru') as client:
        bot_id = await client.bot_id()
        await prepare_polling(client, delete_webhooks)
        logger.info('Бот %s запущен: long polling, отправка ответов и проверка просрочек. Ctrl+C — остановка.', bot_id)
        await run_bot(db, client, bot_id)


def cli():
    parser = argparse.ArgumentParser(description='Запустить ДомПульс локально через MAX long polling')
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--delete-webhooks', action='store_true',
                        help='Отключить все webhook-подписки этого бота перед запуском')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        asyncio.run(main(args.env_file, args.delete_webhooks))
    except KeyboardInterrupt:
        logger.info('Бот остановлен.')
    except (MaxAPIError, RuntimeError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    cli()
