import asyncio
import ssl
import httpx
import pytest

from app import polling
from app.db import AsyncDatabase, Database
from app.enrollment import create_code
from app.max_api import MaxAPIError, MaxClient, create_tls_context
from app.outbox import deliver_one


def update(mid, text):
    return {'update_type': 'message_created', 'message': {
        'sender': {'user_id': 123}, 'body': {'mid': mid, 'text': text},
    }}


def test_polling_http_contract_and_long_read_timeout():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == '/me':
            return httpx.Response(200, json={'user_id': 42})
        if request.url.path == '/subscriptions':
            return httpx.Response(200, json={'subscriptions': []})
        return httpx.Response(200, json={'updates': [], 'marker': 123456789012345})

    async def scenario():
        async with MaxClient('test-secret', 'https://max.test', httpx.MockTransport(handler)) as client:
            assert await client.bot_id() == 42
            assert await client.subscriptions() == []
            await client.get_updates(None)
            await client.get_updates(0)
            await client.get_updates(123456789012345)

    asyncio.run(scenario())
    assert all(request.headers['Authorization'] == 'test-secret' for request in requests)
    assert 'marker' not in requests[2].url.params
    assert requests[3].url.params['marker'] == '0'
    assert requests[4].url.params['marker'] == '123456789012345'
    assert requests[2].url.params['types'] == 'message_created,bot_started'
    assert requests[2].extensions['timeout']['read'] > 30


@pytest.mark.parametrize('status,retryable', [(401, False), (405, False), (429, True), (503, True)])
def test_polling_http_errors(status, retryable):
    async def scenario():
        transport = httpx.MockTransport(lambda request: httpx.Response(status, text='private body'))
        async with MaxClient('test-secret', 'https://max.test', transport) as client:
            with pytest.raises(MaxAPIError) as error:
                await client.get_updates(10)
            assert error.value.retryable is retryable
            assert 'private body' not in str(error.value)
            assert 'test-secret' not in str(error.value)
    asyncio.run(scenario())


@pytest.mark.parametrize('error,message,retryable', [
    (httpx.ConnectError('[SSL: CERTIFICATE_VERIFY_FAILED] private-data'), 'HTTPS-сертификат', False),
    (httpx.ReadTimeout('private-data'), 'время ожидания', True),
    (httpx.ProxyError('private-data'), 'прокси', True),
    (httpx.ConnectError('private-data'), 'соединение не установлено', True),
])
def test_network_diagnostics_are_specific_and_hide_private_data(error, message, retryable):
    def handler(request):
        raise error

    async def scenario():
        async with MaxClient('secret-token', 'https://max.test', httpx.MockTransport(handler)) as client:
            for operation in (client.bot_id, lambda: client.send_message(123, 'private message')):
                with pytest.raises(MaxAPIError) as result:
                    await operation()
                assert message in str(result.value)
                assert result.value.retryable is retryable
                assert 'private' not in str(result.value)
                assert 'secret-token' not in str(result.value)
    asyncio.run(scenario())


def test_tls_verification_stays_enabled_and_explicit_ca_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    monkeypatch.delenv('SSL_CERT_DIR', raising=False)
    context = create_tls_context()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats()['x509_ca'] > 0
    monkeypatch.setenv('SSL_CERT_FILE', str(tmp_path / 'missing-ca.pem'))
    with pytest.raises(FileNotFoundError):
        create_tls_context()


def test_webhook_switch_requires_flag_and_checks_delete_result():
    calls = []
    subscribed = True

    def handler(request):
        nonlocal subscribed
        calls.append(request)
        if request.method == 'DELETE':
            assert request.url.params['url'] == 'https://example.test/webhooks/max'
            subscribed = False
            return httpx.Response(200, json={'success': True})
        return httpx.Response(200, json={
            'subscriptions': [{'url': 'https://example.test/webhooks/max'}] if subscribed else [],
        })

    async def scenario():
        async with MaxClient('token', 'https://max.test', httpx.MockTransport(handler)) as client:
            with pytest.raises(RuntimeError, match='--delete-webhooks'):
                await polling.prepare_polling(client, False)
            assert all(request.method == 'GET' for request in calls)
            await polling.prepare_polling(client, True)
        bad = httpx.MockTransport(lambda request: httpx.Response(200, json={'success': False}))
        async with MaxClient('token', 'https://max.test', bad) as client:
            with pytest.raises(MaxAPIError, match='не удалось'):
                await client.delete_subscription('https://example.test/webhooks/max')
    asyncio.run(scenario())


def test_polling_enrollment_ticket_delivery_and_resume(tmp_path):
    path = str(tmp_path / 'bot.db')
    sync = Database(path)
    sync.initialize()
    with sync.connect(write=True) as conn:
        conn.execute("INSERT INTO houses VALUES('house-1','Тестовый дом')")
    code, _ = create_code(sync, 'house-1', 'resident', 24, 1)
    updates = [update(str(i), text) for i, text in enumerate([
        f'/код {code}', 'Сообщить о проблеме', 'Отопление', 'Подъезд 1', 'Холодные батареи со вчерашнего дня',
    ])]
    markers, sent = [], []

    def handler(request):
        if request.url.path == '/messages':
            sent.append(request)
            return httpx.Response(200, json={'message': {'body': {'mid': 'sent'}}})
        markers.append(request.url.params.get('marker'))
        return httpx.Response(200, json={'updates': updates, 'marker': 99})

    async def scenario():
        async with MaxClient('token', 'https://max.test', httpx.MockTransport(handler)) as client:
            await polling.poll_once(AsyncDatabase(path), client, 42)
            # Simulate restart and replay the same events; replies must not duplicate.
            await polling.poll_once(AsyncDatabase(path), client, 42)
            while await deliver_one(AsyncDatabase(path), client):
                pass
    asyncio.run(scenario())
    assert markers == [None, '99']
    assert len(sent) == 5
    with sync.connect() as conn:
        assert conn.execute('SELECT count(*) FROM tickets').fetchone()[0] == 1
        assert conn.execute('SELECT marker FROM max_polling_state').fetchone()[0] == 99
        assert code not in ''.join(row[0] for row in conn.execute('SELECT payload_json FROM max_updates'))


def test_failed_batch_rolls_back_replies_and_marker(tmp_path, monkeypatch):
    db = AsyncDatabase(str(tmp_path / 'bot.db'))
    original = polling.store_update_in_transaction

    async def fail_second(conn, payload):
        if payload['message']['body']['mid'] == 'bad':
            raise RuntimeError('failed event')
        return await original(conn, payload)

    monkeypatch.setattr(polling, 'store_update_in_transaction', fail_second)

    async def scenario():
        await db.initialize()
        async with db.connect(write=True) as conn:
            await conn.execute('INSERT INTO max_polling_state VALUES(42,10)')
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            'updates': [update('good', '/start'), update('bad', '/start')], 'marker': 20,
        }))
        async with MaxClient('token', 'https://max.test', transport) as client:
            with pytest.raises(RuntimeError, match='failed event'):
                await polling.poll_once(db, client, 42)
    asyncio.run(scenario())
    with Database(db.path).connect() as conn:
        assert conn.execute('SELECT marker FROM max_polling_state').fetchone()[0] == 10
        assert conn.execute('SELECT count(*) FROM max_updates').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM max_outbox').fetchone()[0] == 0


def test_null_marker_preserves_cursor_and_bad_response_does_not_advance(tmp_path):
    db = AsyncDatabase(str(tmp_path / 'bot.db'))

    async def scenario():
        await db.initialize()
        async with db.connect(write=True) as conn:
            await conn.execute('INSERT INTO max_polling_state VALUES(42,10)')
        for payload in ({'updates': [], 'marker': None}, {'updates': [], 'marker': 'bad'}):
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
            async with MaxClient('token', 'https://max.test', transport) as client:
                if payload['marker'] is None:
                    await polling.poll_once(db, client, 42)
                else:
                    with pytest.raises(MaxAPIError):
                        await polling.poll_once(db, client, 42)
    asyncio.run(scenario())
    with Database(db.path).connect() as conn:
        assert conn.execute('SELECT marker FROM max_polling_state').fetchone()[0] == 10


def test_network_retry_preserves_marker(tmp_path, monkeypatch):
    db = AsyncDatabase(str(tmp_path / 'bot.db'))
    markers, delays = [], []

    def handler(request):
        markers.append(request.url.params['marker'])
        if len(markers) == 1:
            raise httpx.ConnectError('offline', request=request)
        return httpx.Response(200, json={'updates': [], 'marker': 11})

    async def fake_sleep(delay):
        delays.append(delay)
        if len(markers) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(polling.asyncio, 'sleep', fake_sleep)

    async def scenario():
        await db.initialize()
        async with db.connect(write=True) as conn:
            await conn.execute('INSERT INTO max_polling_state VALUES(42,10)')
        async with MaxClient('token', 'https://max.test', httpx.MockTransport(handler)) as client:
            with pytest.raises(asyncio.CancelledError):
                await polling.poll_forever(db, client, 42)
    asyncio.run(scenario())
    assert markers == ['10', '10']
    assert delays == [1, 1]


def test_env_loads_literals_without_overwriting_shell(tmp_path, monkeypatch):
    for key in ('MAX_BOT_TOKEN', 'MAX_API_BASE', 'DOMPULSE_DB'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('MAX_BOT_TOKEN', 'from-shell')
    env = tmp_path / '.env'
    env.write_text('\ufeff# settings\nMAX_BOT_TOKEN="from-file"\nDOMPULSE_DB="C:\\data\\bot.db"\n'
                   'MAX_API_BASE=https://max.test\nMAX_WEBHOOK_SECRET=unused\n', encoding='utf-8')
    polling.load_env(env)
    assert polling.os.environ['MAX_BOT_TOKEN'] == 'from-shell'
    assert polling.os.environ['DOMPULSE_DB'] == 'C:\\data\\bot.db'


def test_missing_token_fails_before_network(tmp_path, monkeypatch):
    monkeypatch.delenv('MAX_BOT_TOKEN', raising=False)
    with pytest.raises(ValueError, match='MAX_BOT_TOKEN'):
        asyncio.run(polling.main(tmp_path / 'absent.env'))


def test_sender_runs_during_long_poll_and_finishes_on_shutdown(tmp_path):
    db = AsyncDatabase(str(tmp_path / 'bot.db'))

    async def scenario():
        waiting, sending, finish_send = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Client:
            async def get_updates(self, marker):
                waiting.set()
                await asyncio.Event().wait()

            async def send_message(self, *args):
                sending.set()
                await finish_send.wait()
                return 'delivered'

        await db.initialize()
        async with db.connect(write=True) as conn:
            await conn.execute(
                'INSERT INTO max_outbox(max_user_id,text,next_attempt_at,created_at) '
                "VALUES(123,'Ответ','2000-01-01','2000-01-01')"
            )
        task = asyncio.create_task(polling.run_bot(db, Client(), 42))
        await asyncio.wait_for(waiting.wait(), 3)
        await asyncio.wait_for(sending.wait(), 3)
        task.cancel()
        finish_send.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        async with db.connect() as conn:
            cursor = await conn.execute('SELECT status FROM max_outbox')
            assert (await cursor.fetchone())['status'] == 'sent'
    asyncio.run(scenario())
