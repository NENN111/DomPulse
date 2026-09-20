import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.db import AsyncDatabase, Database, token_hash
from app.max_api import MaxAPIError, MaxClient
from app.outbox import deliver_one, main as worker_main
from app.sla import scan_overdue


@pytest.fixture
def max_app(tmp_path, monkeypatch):
    path = str(tmp_path / 'max.db')
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'test-webhook-secret')
    with TestClient(create_app(path)) as client:
        yield client, Database(path)


def update(mid='message-1', text='/start', user_id=12345):
    return {
        'update_type': 'message_created',
        'message': {
            'sender': {'user_id': user_id},
            'body': {'mid': mid, 'text': text},
        },
    }


def enroll(client, db, headers, user_id, role='resident', house_id='test-house'):
    code = f"LINK-{role[:3].upper()}-{user_id}"
    with db.connect(write=True) as conn:
        conn.execute('INSERT OR IGNORE INTO houses VALUES(?,?)', (house_id, 'Тестовый дом, 1'))
        conn.execute(
            'INSERT INTO enrollment_codes(code_hash,house_id,role,expires_at,max_uses,created_at) VALUES(?,?,?,?,?,?)',
            (token_hash(code), house_id, role, '2099-01-01T00:00:00+00:00', 1,
             '2026-01-01T00:00:00+00:00'),
        )
    response = client.post('/webhooks/max', headers=headers,
                           json=update(f'enroll-{user_id}', f'/код {code}', user_id=user_id))
    assert response.status_code == 200


def test_webhook_verifies_secret_deduplicates_and_queues(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    first = client.post('/webhooks/max', headers=headers, json=update())
    second = client.post('/webhooks/max', headers=headers, json=update())
    assert first.status_code == 200 and first.json()['duplicate'] is False
    assert second.status_code == 200 and second.json()['duplicate'] is True

    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM max_updates').fetchone()[0] == 1
        message = dict(conn.execute('SELECT * FROM max_outbox').fetchone())
    assert message['max_user_id'] == 12345
    assert 'не привязан' in message['text']
    assert '/код' in message['text']


def test_chatbot_creates_ticket_without_mini_app(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    enroll(client, db, headers, 12345)
    messages = [
        ('m2', 'Сообщить о проблеме'),
        ('m3', 'Отопление'),
        ('m4', 'Подъезд 2, этаж 5'),
        ('m5', 'Батареи остаются холодными со вчерашнего вечера.'),
        ('m6', 'Мои обращения'),
    ]
    for mid, text in messages:
        response = client.post('/webhooks/max', headers=headers, json=update(mid, text))
        assert response.status_code == 200 and response.json()['duplicate'] is False

    with db.connect() as conn:
        ticket = dict(conn.execute('SELECT * FROM tickets').fetchone())
        events = conn.execute('SELECT count(*) FROM events WHERE ticket_id=?', (ticket['id'],)).fetchone()[0]
        replies = [dict(row) for row in conn.execute('SELECT * FROM max_outbox ORDER BY id')]
        link = dict(conn.execute('SELECT * FROM max_links').fetchone())
    assert ticket['category'] == 'heating'
    assert ticket['location'] == 'Подъезд 2, этаж 5'
    assert ticket['status'] == 'new' and events == 1
    assert link['max_user_id'] == 12345
    assert 'зарегистрировано' in replies[4]['text']
    assert f"№{ticket['id'][:8]}" in replies[5]['text']
    buttons = json.loads(replies[0]['attachments_json'])[0]['payload']['buttons']
    assert buttons[0][0] == {'type': 'message', 'text': 'Сообщить о проблеме'}


def test_operator_resolution_is_confirmed_in_max_chat(max_app):
    client, db = max_app
    webhook_headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    enroll(client, db, webhook_headers, 12345)
    flow = [
        ('r2', 'Сообщить о проблеме'), ('r3', 'Лифт'),
        ('r4', 'Подъезд 1'), ('r5', 'Лифт не приезжает на этаж уже около часа.'),
    ]
    for mid, text in flow:
        assert client.post('/webhooks/max', headers=webhook_headers, json=update(mid, text)).status_code == 200

    with db.connect(write=True) as conn:
        conn.execute(
            'INSERT INTO users VALUES(?,?,?,?,?)',
            ('staff', 'Диспетчер УК', 'operator', 'test-house', token_hash('staff-secret')),
        )
        ticket_id = conn.execute('SELECT id FROM tickets').fetchone()[0]
    auth = {'Authorization': 'Bearer staff-secret'}
    ticket = client.get(f'/api/tickets/{ticket_id}', headers=auth).json()
    for status in ['accepted', 'in_progress', 'resolved']:
        response = client.post(f'/api/tickets/{ticket_id}/status', headers=auth, json={
            'status': status, 'comment': f'Комментарий: {status}', 'expected_version': ticket['version'],
        })
        assert response.status_code == 200, response.text
        ticket = response.json()

    with db.connect() as conn:
        notice = dict(conn.execute('SELECT * FROM max_outbox ORDER BY id DESC LIMIT 1').fetchone())
    assert 'выполненное' in notice['text']
    buttons = json.loads(notice['attachments_json'])[0]['payload']['buttons']
    assert buttons[0][0]['text'] == 'Да, всё решено'

    response = client.post('/webhooks/max', headers=webhook_headers, json=update('r6', 'Да, всё решено'))
    assert response.status_code == 200
    with db.connect() as conn:
        final = dict(conn.execute('SELECT * FROM tickets WHERE id=?', (ticket_id,)).fetchone())
        last_event = dict(conn.execute(
            'SELECT * FROM events WHERE ticket_id=? ORDER BY id DESC LIMIT 1', (ticket_id,)
        ).fetchone())
    assert final['status'] == 'confirmed'
    assert last_event['actor_id'] == 'max-resident-12345'


def test_operator_works_queue_and_sets_priority_inside_max(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    enroll(client, db, headers, 12345)
    resident_flow = [
        ('resident-2', 'Сообщить о проблеме'), ('resident-3', 'Вода'),
        ('resident-4', 'Подвал, стояк 3'), ('resident-5', 'В подвале течёт труба, вода продолжает прибывать.'),
    ]
    for mid, text in resident_flow:
        assert client.post('/webhooks/max', headers=headers, json=update(mid, text)).status_code == 200
    with db.connect() as conn:
        ticket = dict(conn.execute('SELECT * FROM tickets').fetchone())
    prefix = ticket['id'][:8]

    enroll(client, db, headers, 777, role='operator')
    dispatcher_flow = [
        ('operator-2', 'Очередь дома'),
        ('operator-3', f'Заявка #{prefix}'), ('operator-4', 'Изменить срочность'),
        ('operator-5', 'Авария'), ('operator-6', 'Принять'),
        ('operator-7', 'Дежурная бригада приняла вызов и выезжает.'),
    ]
    for mid, text in dispatcher_flow:
        response = client.post('/webhooks/max', headers=headers, json=update(mid, text, user_id=777))
        assert response.status_code == 200, response.text

    with db.connect() as conn:
        updated = dict(conn.execute('SELECT * FROM tickets WHERE id=?', (ticket['id'],)).fetchone())
        events = [dict(row) for row in conn.execute(
            'SELECT * FROM events WHERE ticket_id=? ORDER BY id', (ticket['id'],)
        )]
        resident_notice = dict(conn.execute(
            'SELECT * FROM max_outbox WHERE max_user_id=12345 ORDER BY id DESC LIMIT 1'
        ).fetchone())
    assert updated['priority'] == 'emergency'
    assert updated['status'] == 'accepted'
    assert [event['kind'] for event in events] == ['created', 'priority_changed', 'status_changed']
    assert 'принято' in resident_notice['text']


def test_operator_sees_grouped_common_problem_signal(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    residents = [
        (12345, [('g2', 'Сообщить о проблеме'), ('g3', 'Отопление'),
                 ('g4', 'Корпус А, подъезд 1'), ('g5', 'Батареи холодные уже второй день.')]),
        (12400, [('h2', 'Сообщить о проблеме'), ('h3', 'Отопление'),
                 ('h4', 'Корпус А, подъезд 1'), ('h5', 'Холодные батареи не работают второй день.')]),
    ]
    for user_id, flow in residents:
        enroll(client, db, headers, user_id)
        for mid, text in flow:
            assert client.post('/webhooks/max', headers=headers,
                               json=update(mid, text, user_id=user_id)).status_code == 200
    enroll(client, db, headers, 777, role='operator')
    for mid, text in [('operator-group-2', 'Общие проблемы дома')]:
        assert client.post('/webhooks/max', headers=headers,
                           json=update(mid, text, user_id=777)).status_code == 200

    with db.connect() as conn:
        response = dict(conn.execute(
            'SELECT * FROM max_outbox WHERE max_user_id=777 ORDER BY id DESC LIMIT 1'
        ).fetchone())
    assert '2 обращения' in response['text']
    assert 'Связанные заявки' in response['text']


def test_sla_scanner_alerts_operator_once(tmp_path):
    path = str(tmp_path / 'sla.db')
    db = Database(path)
    db.initialize()
    with db.connect(write=True) as conn:
        conn.executemany('INSERT INTO houses VALUES(?,?)', [('h1', 'Дом 1')])
        conn.executemany('INSERT INTO users VALUES(?,?,?,?,?)', [
            ('resident', 'Житель', 'resident', 'h1', token_hash('resident')),
            ('operator', 'Диспетчер', 'operator', 'h1', token_hash('operator')),
        ])
        conn.execute('INSERT INTO max_links VALUES(?,?,?)', (900, 'operator', '2020-01-01T00:00:00+00:00'))
        conn.execute(
            'INSERT INTO tickets(id,house_id,resident_id,category,location,description,status,priority,version,created_at,updated_at,due_at) '
            "VALUES('sla-ticket','h1','resident','water','Подвал','Вода течёт уже давно','new','urgent',1,?, ?,?)",
            ('2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00', '2020-01-01T00:01:00+00:00'),
        )
    async_db = AsyncDatabase(path)
    assert asyncio.run(scan_overdue(async_db)) == 1
    assert asyncio.run(scan_overdue(async_db)) == 0
    with db.connect() as conn:
        alert = dict(conn.execute('SELECT * FROM sla_alerts').fetchone())
        notice = dict(conn.execute('SELECT * FROM max_outbox').fetchone())
    assert alert['ticket_id'] == 'sla-ticket'
    assert notice['max_user_id'] == 900 and 'Просрочена' in notice['text']


def test_one_time_code_links_max_user_without_storing_code(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    code = 'SAFE-ABCD-1234-WXYZ'
    with db.connect(write=True) as conn:
        conn.execute('INSERT INTO houses VALUES(?,?)', ('secure-house', 'Проверочный дом, 7'))
        conn.execute(
            'INSERT INTO enrollment_codes(code_hash,house_id,role,expires_at,max_uses,created_at) VALUES(?,?,?,?,?,?)',
            (token_hash(code), 'secure-house', 'operator', '2099-01-01T00:00:00+00:00', 1,
             '2026-01-01T00:00:00+00:00'),
        )
    response = client.post('/webhooks/max', headers=headers,
                           json=update('secure-1', f'/код {code}', user_id=600))
    assert response.status_code == 200
    with db.connect() as conn:
        user = dict(conn.execute(
            'SELECT u.* FROM max_links l JOIN users u ON u.id=l.user_id WHERE l.max_user_id=600'
        ).fetchone())
        stored = conn.execute('SELECT payload_json FROM max_updates WHERE fingerprint IS NOT NULL ORDER BY received_at DESC LIMIT 1').fetchone()[0]
        uses = conn.execute('SELECT used_count FROM enrollment_codes').fetchone()[0]
    assert user['house_id'] == 'secure-house' and user['role'] == 'operator'
    assert code not in stored and '[REDACTED]' in stored
    assert uses == 1


def test_invalid_enrollment_code_is_rate_limited(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    for attempt in range(5):
        response = client.post('/webhooks/max', headers=headers,
                               json=update(f'bad-code-{attempt}', '/код NOT-A-REAL-CODE', user_id=601))
        assert response.status_code == 200
    response = client.post('/webhooks/max', headers=headers,
                           json=update('bad-code-6', '/код NOT-A-REAL-CODE', user_id=601))
    assert response.status_code == 200
    with db.connect() as conn:
        message = conn.execute(
            'SELECT text FROM max_outbox WHERE max_user_id=601 ORDER BY id DESC LIMIT 1'
        ).fetchone()[0]
    assert response.json()['duplicate'] is False
    assert 'Слишком много попыток' in message


def test_operator_sees_aggregated_house_metrics(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    enroll(client, db, headers, 777, role='operator')
    with db.connect(write=True) as conn:
        conn.executemany('INSERT INTO users VALUES(?,?,?,?,?)', [
            ('metric-r1', 'Житель 1', 'resident', 'test-house', token_hash('metric-r1')),
            ('metric-r2', 'Житель 2', 'resident', 'test-house', token_hash('metric-r2')),
        ])
        conn.executemany(
            'INSERT INTO tickets(id,house_id,resident_id,category,location,description,status,priority,version,created_at,updated_at,due_at,first_response_at,closed_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [
                ('metric-1', 'test-house', 'metric-r1', 'water', 'Подвал', 'Течёт труба в подвале',
                 'in_progress', 'emergency', 1, '2000-01-01T00:00:00+00:00', '2000-01-01T00:30:00+00:00',
                 '2000-01-01T00:15:00+00:00', '2000-01-01T00:30:00+00:00', None),
                ('metric-2', 'test-house', 'metric-r2', 'heating', 'Подъезд 1', 'Холодные батареи',
                 'new', 'normal', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                 '2026-01-02T00:00:00+00:00', None, None),
                ('metric-3', 'test-house', 'metric-r1', 'heating', 'Подъезд 1', 'Батареи исправны',
                 'confirmed', 'normal', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00',
                 '2026-01-02T00:00:00+00:00', '2026-01-01T00:20:00+00:00', '2026-01-01T01:00:00+00:00'),
            ],
        )
    response = client.post('/webhooks/max', headers=headers,
                           json=update('metrics-view', 'Показатели дома', user_id=777))
    assert response.status_code == 200
    with db.connect() as conn:
        text = conn.execute(
            'SELECT text FROM max_outbox WHERE max_user_id=777 ORDER BY id DESC LIMIT 1'
        ).fetchone()[0]
    assert 'Активные: 2' in text
    assert 'Аварийные: 1' in text
    assert 'Просроченные: 2' in text
    assert 'Средняя первая реакция: 25 мин.' in text
    assert 'Закрыто в срок: 100% (1 заявок)' in text


def test_webhook_rejects_bad_input_and_missing_config(tmp_path, monkeypatch):
    path = str(tmp_path / 'invalid.db')
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'expected')
    with TestClient(create_app(path)) as client:
        assert client.post('/webhooks/max', json=update()).status_code == 401
        assert client.post(
            '/webhooks/max', headers={'X-Max-Bot-Api-Secret': 'wrong'}, json=update()
        ).status_code == 401
        assert client.post(
            '/webhooks/max', headers={'X-Max-Bot-Api-Secret': 'expected'}, json={}
        ).status_code == 400
        too_large = b'x' * (256 * 1024 + 1)
        assert client.post(
            '/webhooks/max', headers={'X-Max-Bot-Api-Secret': 'expected'}, content=too_large
        ).status_code == 413

    monkeypatch.delenv('MAX_WEBHOOK_SECRET')
    with pytest.raises(RuntimeError, match='MAX_WEBHOOK_SECRET is required'):
        create_app(str(tmp_path / 'disabled.db'))


def test_api_requires_explicit_database_configuration(monkeypatch):
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'test-webhook-secret')
    monkeypatch.delenv('DOMPULSE_DB', raising=False)
    with pytest.raises(RuntimeError, match='DOMPULSE_DB is required'):
        create_app()


def test_max_client_and_worker_require_explicit_configuration(monkeypatch):
    with pytest.raises(ValueError, match='MAX API base URL is required'):
        MaxClient('secret-token', '')
    for name in ('MAX_BOT_TOKEN', 'MAX_API_BASE', 'DOMPULSE_DB'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match='Required configuration is missing'):
        asyncio.run(worker_main())


def test_max_client_uses_official_contract():
    captured = {}

    def handler(request: httpx.Request):
        captured['request'] = request
        return httpx.Response(200, json={'message': {'body': {'mid': 'sent-42'}}})

    async def send():
        async with MaxClient('secret-token', 'https://platform-api2.max.ru', transport=httpx.MockTransport(handler)) as client:
            return await client.send_message(12345, 'Привет', [{'type': 'inline_keyboard'}])

    mid = asyncio.run(send())

    request = captured['request']
    assert str(request.url) == 'https://platform-api2.max.ru/messages?user_id=12345'
    assert request.headers['Authorization'] == 'secret-token'
    assert json.loads(request.content) == {
        'text': 'Привет', 'attachments': [{'type': 'inline_keyboard'}]
    }
    assert mid == 'sent-42'


class FakeMaxClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def send_message(self, user_id, text, attachments):
        self.calls.append((user_id, text, attachments))
        if self.fail:
            raise MaxAPIError('temporary failure')
        return 'max-mid-1'


def queued_db(tmp_path):
    db = Database(str(tmp_path / 'outbox.db'))
    db.initialize()
    with db.connect(write=True) as conn:
        conn.execute(
            'INSERT INTO max_outbox(max_user_id,text,attachments_json,next_attempt_at,created_at) '
            "VALUES(123,'Сообщение','[]','2000-01-01T00:00:00+00:00','2000-01-01T00:00:00+00:00')"
        )
    return AsyncDatabase(str(tmp_path / 'outbox.db'))


def test_outbox_marks_success(tmp_path):
    db = queued_db(tmp_path)
    fake = FakeMaxClient()
    assert asyncio.run(deliver_one(db, fake)) is True
    assert asyncio.run(deliver_one(db, fake)) is False
    with Database(db.path).connect() as conn:
        row = dict(conn.execute('SELECT * FROM max_outbox').fetchone())
    assert row['status'] == 'sent' and row['max_mid'] == 'max-mid-1'
    assert row['attempts'] == 1 and len(fake.calls) == 1


def test_outbox_schedules_retry(tmp_path):
    db = queued_db(tmp_path)
    assert asyncio.run(deliver_one(db, FakeMaxClient(fail=True))) is True
    with Database(db.path).connect() as conn:
        row = dict(conn.execute('SELECT * FROM max_outbox').fetchone())
    assert row['status'] == 'failed'
    assert row['attempts'] == 1 and row['error'] == 'temporary failure'
    assert asyncio.run(deliver_one(db, FakeMaxClient())) is False
