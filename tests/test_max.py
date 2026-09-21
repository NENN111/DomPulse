import asyncio
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app import max_webhook
from app.geocoder import GeocodedAddress
from app.db import AsyncDatabase, Database, token_hash
from app.max_api import MaxAPIError, MaxClient
from app.max_webhook import ticket_review
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


def test_photo_is_kept_when_sent_before_category(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    enroll(client, db, headers, 1450)
    with db.connect(write=True) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO max_dialogs(max_user_id,state,draft_json,updated_at) VALUES(?,?,?,?)",
            (1450, 'category', '{}', '2026-01-01T00:00:00+00:00'),
        )
    payload = update('photo-image', '', 1450)
    payload['message']['body']['attachments'] = [{
        'type': 'image',
        'payload': {'photo_id': 42787123429, 'token': 'photo-token', 'url': 'https://images.test/photo'},
    }]
    response = client.post('/webhooks/max', headers=headers, json=payload)
    assert response.status_code == 200
    with db.connect() as conn:
        dialog = conn.execute('SELECT state,draft_json FROM max_dialogs WHERE max_user_id=1450').fetchone()
    assert dialog['state'] == 'category'
    assert json.loads(dialog['draft_json'])['attachments'][0]['external_id'] == '42787123429'


def test_ticket_review_shows_photo_count():
    text, _ = ticket_review({
        'category': 'yard',
        'location': 'Двор',
        'description': 'Повреждено ограждение.',
        'attachments': [{'type': 'image', 'external_id': 'photo-1'}],
    })
    assert 'Фото: 1 шт.' in text


def test_chatbot_creates_ticket_without_mini_app(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    enroll(client, db, headers, 12345)
    messages = [
        ('m2', 'Сообщить о проблеме'),
        ('m3', 'Отопление'),
        ('m4', 'Подъезд 2, этаж 5'),
        ('m5', 'Батареи остаются холодными со вчерашнего вечера.'),
        ('m5-send', 'Отправить'), ('m6', 'Мои обращения'),
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
    assert 'зарегистрировано' in replies[5]['text']
    assert f"№{ticket['id'][:8]}" in replies[6]['text']
    buttons = json.loads(replies[0]['attachments_json'])[0]['payload']['buttons']
    assert buttons[0][0] == {'type': 'message', 'text': 'Сообщить о проблеме'}


def test_operator_resolution_is_confirmed_in_max_chat(max_app):
    client, db = max_app
    webhook_headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    enroll(client, db, webhook_headers, 12345)
    flow = [
        ('r2', 'Сообщить о проблеме'), ('r3', 'Лифт'),
        ('r4', 'Подъезд 1'), ('r5', 'Лифт не приезжает на этаж уже около часа.'),
        ('r5-send', 'Отправить'),
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
    assert final['closed_at'] is not None
    assert last_event['actor_id'] == 'max-resident-12345'


def test_operator_works_queue_and_sets_priority_inside_max(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    enroll(client, db, headers, 12345)
    resident_flow = [
        ('resident-2', 'Сообщить о проблеме'), ('resident-3', 'Вода'),
        ('resident-4', 'Подвал, стояк 3'), ('resident-5', 'В подвале течёт труба, вода продолжает прибывать.'),
        ('resident-5-send', 'Отправить'),
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
    assert updated['first_response_at'] is not None
    assert [event['kind'] for event in events] == ['created', 'priority_changed', 'status_changed']
    assert 'принято' in resident_notice['text']


def test_operator_sees_grouped_common_problem_signal(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    residents = [
        (12345, [('g2', 'Сообщить о проблеме'), ('g3', 'Отопление'),
                 ('g4', 'Корпус А, подъезд 1'), ('g5', 'Батареи холодные уже второй день.'), ('g6', 'Отправить')]),
        (12400, [('h2', 'Сообщить о проблеме'), ('h3', 'Отопление'),
                 ('h4', 'Корпус А, подъезд 1'), ('h5', 'Холодные батареи не работают второй день.'), ('h6', 'Отправить')]),
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

    for mid, text in [('operator-group-3', 'Открыть сигнал 1'), ('operator-group-4', 'Срочно')]:
        assert client.post('/webhooks/max', headers=headers,
                           json=update(mid, text, user_id=777)).status_code == 200
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM incidents').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM incident_tickets').fetchone()[0] == 2

    enroll(client, db, headers, 12500)
    for mid, text in [
        ('join-1', 'Сообщить о проблеме'), ('join-2', 'Отопление'),
        ('join-3', 'Корпус А, подъезд 1'), ('join-4', 'Батареи холодные второй день.'),
    ]:
        assert client.post('/webhooks/max', headers=headers,
                           json=update(mid, text, user_id=12500)).status_code == 200
    with db.connect() as conn:
        prompt = conn.execute(
            'SELECT text FROM max_outbox WHERE max_user_id=12500 ORDER BY id DESC LIMIT 1'
        ).fetchone()[0]
        assert conn.execute('SELECT count(*) FROM tickets').fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM incident_tickets').fetchone()[0] == 2
    assert 'похожая общая проблема' in prompt
    assert 'Батареи холодные уже второй день' not in prompt

    client.post('/webhooks/max', headers=headers,
                json=update('join-5', 'Да, присоединить', user_id=12500))
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM tickets').fetchone()[0] == 2
    client.post('/webhooks/max', headers=headers,
                json=update('join-6', 'Отправить', user_id=12500))
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM tickets').fetchone()[0] == 3
        assert conn.execute('SELECT count(*) FROM incident_tickets').fetchone()[0] == 3


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
    assert 'Просроченные: 1' in text
    assert 'Средняя первая реакция: 25 мин.' in text
    assert 'Ответ в пределах SLA: 50% (2 заявок)' in text


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


def test_resident_opens_own_ticket_card_but_not_foreign(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    enroll(client, db, headers, 1101)
    for mid, text in [
        ('card-1', 'Сообщить о проблеме'), ('card-2', 'Отопление'),
        ('card-3', 'Подъезд 3'), ('card-4', 'Батареи остаются холодными второй день.'),
        ('card-5', 'Отправить'),
    ]:
        assert client.post('/webhooks/max', headers=headers, json=update(mid, text, 1101)).status_code == 200
    with db.connect() as conn:
        ticket = dict(conn.execute('SELECT * FROM tickets').fetchone())

    client.post('/webhooks/max', headers=headers, json=update('card-list', 'Мои обращения', 1101))
    with db.connect() as conn:
        listed = dict(conn.execute(
            'SELECT * FROM max_outbox WHERE max_user_id=1101 ORDER BY id DESC LIMIT 1'
        ).fetchone())
    buttons = json.loads(listed['attachments_json'])[0]['payload']['buttons']
    assert buttons[0][0]['text'] == f"Обращение #{ticket['id'][:8]}"

    client.post('/webhooks/max', headers=headers,
                json=update('card-open', f"Обращение #{ticket['id'][:8]}", 1101))
    with db.connect() as conn:
        card = conn.execute(
            'SELECT text FROM max_outbox WHERE max_user_id=1101 ORDER BY id DESC LIMIT 1'
        ).fetchone()[0]
    assert 'Категория: Отопление' in card
    assert 'Статус: новое' in card
    assert 'Описание:' in card and 'История:' in card

    enroll(client, db, headers, 1102)
    client.post('/webhooks/max', headers=headers,
                json=update('card-foreign', f"Обращение #{ticket['id'][:8]}", 1102))
    with db.connect() as conn:
        denied = conn.execute(
            'SELECT text FROM max_outbox WHERE max_user_id=1102 ORDER BY id DESC LIMIT 1'
        ).fetchone()[0]
    assert denied == 'Обращение не найдено.'


def test_operator_replies_without_status_change(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    enroll(client, db, headers, 1201)
    for mid, text in [
        ('reply-r1', 'Сообщить о проблеме'), ('reply-r2', 'Вода'),
        ('reply-r3', 'Подвал'), ('reply-r4', 'В подвале течёт труба около стояка.'),
        ('reply-r5', 'Отправить'),
    ]:
        client.post('/webhooks/max', headers=headers, json=update(mid, text, 1201))
    with db.connect() as conn:
        ticket = dict(conn.execute('SELECT * FROM tickets').fetchone())
    enroll(client, db, headers, 1202, role='operator')
    for mid, text in [
        ('reply-o1', f"Заявка #{ticket['id'][:8]}"),
        ('reply-o2', 'Ответить жителю'),
        ('reply-o3', 'Специалист получил сообщение и уточняет причину.'),
    ]:
        response = client.post('/webhooks/max', headers=headers, json=update(mid, text, 1202))
        assert response.status_code == 200
    with db.connect() as conn:
        updated = dict(conn.execute('SELECT * FROM tickets WHERE id=?', (ticket['id'],)).fetchone())
        event = dict(conn.execute(
            "SELECT * FROM events WHERE ticket_id=? AND kind='comment'", (ticket['id'],)
        ).fetchone())
        notice = conn.execute(
            'SELECT text FROM max_outbox WHERE max_user_id=1201 ORDER BY id DESC LIMIT 1'
        ).fetchone()[0]
    assert updated['status'] == 'new'
    assert updated['first_response_at'] is not None
    assert event['text'] == 'Специалист получил сообщение и уточняет причину.'
    assert 'Ответ УК' in notice

def test_sla_alert_is_not_created_after_first_response(tmp_path):
    path = str(tmp_path / 'sla-answered.db')
    db = Database(path)
    db.initialize()
    with db.connect(write=True) as conn:
        conn.execute("INSERT INTO houses VALUES('h1','Дом 1')")
        conn.executemany('INSERT INTO users VALUES(?,?,?,?,?)', [
            ('resident', 'Житель', 'resident', 'h1', token_hash('resident')),
            ('operator', 'Диспетчер', 'operator', 'h1', token_hash('operator')),
        ])
        conn.execute('INSERT INTO max_links VALUES(?,?,?)', (901, 'operator', '2020-01-01T00:00:00+00:00'))
        conn.execute(
            'INSERT INTO tickets(id,house_id,resident_id,category,location,description,status,priority,version,'
            'created_at,updated_at,due_at,first_response_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('answered', 'h1', 'resident', 'water', 'Подвал', 'Вода течёт около стояка', 'accepted',
             'urgent', 2, '2020-01-01T00:00:00+00:00', '2020-01-01T00:02:00+00:00',
             '2020-01-01T00:01:00+00:00', '2020-01-01T00:02:00+00:00'),
        )
    assert asyncio.run(scan_overdue(AsyncDatabase(path))) == 0
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM sla_alerts').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM max_outbox').fetchone()[0] == 0


def test_reopened_via_max_clears_closed_at(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    enroll(client, db, headers, 1301)
    with db.connect(write=True) as conn:
        resident_id = conn.execute(
            'SELECT user_id FROM max_links WHERE max_user_id=1301'
        ).fetchone()[0]
        conn.execute(
            'INSERT INTO tickets(id,house_id,resident_id,category,location,description,status,priority,version,'
            'created_at,updated_at,due_at,first_response_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('reopen-12345678', 'test-house', resident_id, 'water', 'Подвал',
             'Вода снова появилась около стояка', 'resolved', 'normal', 3,
             '2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00',
             '2026-01-02T00:00:00+00:00', '2026-01-01T00:10:00+00:00', '2026-01-01T01:00:00+00:00'),
        )
        conn.execute(
            "INSERT OR REPLACE INTO max_dialogs(max_user_id,state,draft_json,updated_at) VALUES(1301,'confirm',?,?)",
            (json.dumps({'ticket_id': 'reopen-12345678'}), '2026-01-01T01:00:00+00:00'),
        )
    response = client.post('/webhooks/max', headers=headers,
                           json=update('reopen-max', 'Проблема осталась', 1301))
    assert response.status_code == 200
    with db.connect() as conn:
        ticket = dict(conn.execute("SELECT * FROM tickets WHERE id='reopen-12345678'").fetchone())
    assert ticket['status'] == 'reopened'
    assert ticket['closed_at'] is None


def test_manual_address_requires_matching_house_code(max_app):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    with db.connect(write=True) as conn:
        conn.execute('INSERT INTO houses VALUES(?,?)', ('manual-house', 'Москва, улица Лесная, дом 10'))
        conn.execute(
            'INSERT INTO enrollment_codes(code_hash,house_id,role,expires_at,max_uses,created_at) '
            'VALUES(?,?,?,?,?,?)',
            (token_hash('MANUAL-OK-CODE-1'), 'manual-house', 'resident',
             '2099-01-01T00:00:00+00:00', 1, '2026-01-01T00:00:00+00:00'),
        )
    for mid, text in [
        ('manual-start', '/start'),
        ('manual-select', 'Ввести адрес вручную'),
        ('manual-wrong-address', 'Москва, улица Другая, дом 99'),
        ('manual-wrong-code', '/код MANUAL-OK-CODE-1'),
    ]:
        response = client.post('/webhooks/max', headers=headers, json=update(mid, text, 1401))
        assert response.status_code == 200
    with db.connect() as conn:
        assert conn.execute('SELECT 1 FROM max_links WHERE max_user_id=1401').fetchone() is None
        assert conn.execute('SELECT used_count FROM enrollment_codes').fetchone()[0] == 0

    for mid, text in [
        ('manual-select-correct', 'Ввести адрес вручную'),
        ('manual-address', 'Москва, улица Лесная, дом 10'),
        ('manual-code', '/код MANUAL-OK-CODE-1'),
    ]:
        response = client.post('/webhooks/max', headers=headers, json=update(mid, text, 1401))
        assert response.status_code == 200
    with db.connect() as conn:
        linked = conn.execute(
            'SELECT u.house_id FROM max_links l JOIN users u ON u.id=l.user_id WHERE l.max_user_id=1401'
        ).fetchone()
    assert linked['house_id'] == 'manual-house'


def test_gosuslugi_bridge_links_active_temporary_registration(tmp_path, monkeypatch):
    secret = 'bridge-secret-with-at-least-32-characters'
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'test-webhook-secret')
    monkeypatch.setenv('GOSUSLUGI_BRIDGE_URL', 'https://identity.example.test/connect')
    monkeypatch.setenv('GOSUSLUGI_BRIDGE_SECRET', secret)
    path = str(tmp_path / 'gosuslugi.db')
    db = Database(path)
    db.initialize()
    with db.connect(write=True) as conn:
        conn.executemany('INSERT INTO houses VALUES(?,?)', [
            ('permanent-house', 'Москва, улица Постоянная, дом 1'),
            ('temporary-house', 'Москва, улица Временная, дом 2'),
        ])
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    with TestClient(create_app(path)) as client:
        client.post('/webhooks/max', headers=headers, json=update('gos-start', '/start', 1501))
        client.post('/webhooks/max', headers=headers,
                    json=update('gos-select', 'Подтянуть адрес из Госуслуг', 1501))
        with db.connect() as conn:
            message = conn.execute(
                'SELECT text FROM max_outbox WHERE max_user_id=1501 ORDER BY id DESC LIMIT 1'
            ).fetchone()[0]
        state = parse_qs(urlparse(message.splitlines()[-1]).query)['state'][0]
        claim = {
            'state': state,
            'subject_id': 'esia-subject-1501',
            'registrations': [
                {'type': 'permanent', 'house_address': 'Москва, улица Постоянная, дом 1'},
                {'type': 'temporary', 'house_address': 'Москва, улица Временная, дом 2',
                 'valid_until': '2099-12-31'},
            ],
        }
        raw = json.dumps(claim, ensure_ascii=False, separators=(',', ':')).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        invalid = client.post(
            '/api/integrations/gosuslugi/residency', content=raw,
            headers={'Content-Type': 'application/json', 'X-DomPulse-Signature': 'bad'},
        )
        response = client.post(
            '/api/integrations/gosuslugi/residency', content=raw,
            headers={'Content-Type': 'application/json', 'X-DomPulse-Signature': 'sha256=' + signature},
        )
        replay = client.post(
            '/api/integrations/gosuslugi/residency', content=raw,
            headers={'Content-Type': 'application/json', 'X-DomPulse-Signature': signature},
        )
    assert invalid.status_code == 401
    assert response.status_code == 200
    assert response.json() == {'status': 'completed', 'linked': True, 'house_id': 'temporary-house'}
    assert replay.status_code == 409
    with db.connect() as conn:
        linked = conn.execute(
            'SELECT u.house_id FROM max_links l JOIN users u ON u.id=l.user_id WHERE l.max_user_id=1501'
        ).fetchone()
        verification = dict(conn.execute('SELECT * FROM residency_verifications').fetchone())
    assert linked['house_id'] == 'temporary-house'
    assert verification['registration_type'] == 'temporary'
    assert verification['subject_hash'] != claim['subject_id']
    assert verification['house_address'] == 'Москва, улица Временная, дом 2'


def test_free_geocoder_requires_user_district_confirmation(max_app, monkeypatch):
    client, db = max_app
    headers = {'X-Max-Bot-Api-Secret': 'test-webhook-secret'}
    calls = []

    async def fake_geocode(address):
        calls.append(address)
        return GeocodedAddress(
            '\u0420\u043e\u0441\u0441\u0438\u044f, \u041c\u043e\u0441\u043a\u0432\u0430, \u043f\u0440\u043e\u0441\u043f\u0435\u043a\u0442 \u0412\u0435\u0440\u043d\u0430\u0434\u0441\u043a\u043e\u0433\u043e, 88',
            '\u0417\u0410\u041e',
            55.676,
            37.503,
        )

    monkeypatch.setenv('YANDEX_MAPS_API_KEY', 'free-test-key')
    monkeypatch.setenv('YANDEX_MAPS_ALLOW_STORAGE', 'false')
    monkeypatch.setattr(max_webhook, 'geocode_address', fake_geocode)

    messages = [
        ('district-start', '/start'),
        ('district-manual', '\u0412\u0432\u0435\u0441\u0442\u0438 \u0430\u0434\u0440\u0435\u0441 \u0432\u0440\u0443\u0447\u043d\u0443\u044e'),
        ('district-address', '\u0412\u0435\u0440\u043d\u0430\u0434\u0441\u043a\u043e\u0433\u043e 88'),
    ]
    for mid, text_value in messages:
        response = client.post('/webhooks/max', headers=headers, json=update(mid, text_value, 1880))
        assert response.status_code == 200

    with db.connect() as conn:
        dialog = conn.execute('SELECT state,draft_json FROM max_dialogs WHERE max_user_id=1880').fetchone()
        assert conn.execute('SELECT COUNT(*) FROM max_links WHERE max_user_id=1880').fetchone()[0] == 0
    assert dialog['state'] == 'manual_district'
    assert json.loads(dialog['draft_json']) == {'address': '\u0412\u0435\u0440\u043d\u0430\u0434\u0441\u043a\u043e\u0433\u043e 88'}

    client.post('/webhooks/max', headers=headers, json=update('district-wrong', '\u0426\u0410\u041e', 1880))
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM max_links WHERE max_user_id=1880').fetchone()[0] == 0

    client.post('/webhooks/max', headers=headers, json=update('district-correct', '\u0417\u0410\u041e', 1880))
    with db.connect() as conn:
        linked = conn.execute(
            'SELECT h.id,h.address,hd.district FROM max_links l '
            'JOIN users u ON u.id=l.user_id JOIN houses h ON h.id=u.house_id '
            'JOIN house_districts hd ON hd.house_id=h.id WHERE l.max_user_id=1880'
        ).fetchone()
    assert linked['id'].startswith('address-')
    assert linked['address'] == '\u0412\u0435\u0440\u043d\u0430\u0434\u0441\u043a\u043e\u0433\u043e 88'
    assert linked['district'] == '\u0417\u0410\u041e'
    assert calls == ['\u0412\u0435\u0440\u043d\u0430\u0434\u0441\u043a\u043e\u0433\u043e 88'] * 3
