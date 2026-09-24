import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from app.api import create_app
from app.db import AsyncDatabase, Database, token_hash
from app.miniapp_login import issue_code
from app.miniapp_auth import InvalidLaunchData, validate_launch_data


TOKEN = 'test-miniapp-bot-token'


def signed_data(user_id, *, age=0):
    values = {'auth_date': str(int(time.time()) - age), 'user': json.dumps({'id': user_id})}
    signed = '\n'.join(f'{key}={value}' for key, value in sorted(values.items()))
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    values['hash'] = hmac.new(secret, signed.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_launch_data_rejects_tampering_and_expiration():
    valid = signed_data(123)
    assert validate_launch_data(valid, TOKEN) == 123
    try:
        validate_launch_data(valid.replace('123', '124'), TOKEN)
        assert False, 'tampered data must fail'
    except InvalidLaunchData:
        pass
    try:
        validate_launch_data(signed_data(123, age=7200), TOKEN)
        assert False, 'expired data must fail'
    except InvalidLaunchData:
        pass


def test_operator_miniapp_uses_linked_role_and_house(tmp_path, monkeypatch):
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'test-secret')
    monkeypatch.setenv('MAX_BOT_TOKEN', TOKEN)
    db = Database(str(tmp_path / 'miniapp.db'))
    db.initialize()
    with db.connect(write=True) as conn:
        conn.executemany('INSERT INTO houses VALUES(?,?)', [('one', 'Дом 1'), ('two', 'Дом 2')])
        conn.executemany('INSERT INTO users VALUES(?,?,?,?,?)', [
            ('operator', 'Диспетчер', 'operator', 'one', token_hash('operator-token')),
            ('resident', 'Житель', 'resident', 'one', token_hash('resident-token')),
            ('other', 'Другой', 'resident', 'two', token_hash('other-token')),
        ])
        conn.executemany('INSERT INTO max_links VALUES(?,?,?)', [
            (101, 'operator', '2026-01-01T00:00:00+00:00'),
            (102, 'resident', '2026-01-01T00:00:00+00:00'),
        ])
        conn.execute(
            "INSERT INTO tickets(id,house_id,resident_id,category,location,description,status,priority,"
            "version,created_at,updated_at,due_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ('one-ticket', 'one', 'resident', 'water', 'Подвал', 'Течёт труба', 'new', 'normal', 1,
             '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00'),
        )
    with TestClient(create_app(str(tmp_path / 'miniapp.db'))) as client:
        assert client.get('/miniapp').status_code == 200
        assert client.get('/api/miniapp/overview').status_code == 401
        assert client.get('/api/miniapp/overview', headers={'X-Max-Init-Data': signed_data(102)}).status_code == 403
        headers = {'X-Max-Init-Data': signed_data(101)}
        overview = client.get('/api/miniapp/overview', headers=headers)
        assert overview.status_code == 200
        assert overview.json()['metrics']['active'] == 1
        assert len(overview.json()['tickets']) == 1
        assert client.get('/api/miniapp/overview?house_id=two', headers=headers).status_code == 404
        changed = client.post('/api/miniapp/tickets/one-ticket/priority', headers=headers,
                              json={'priority': 'emergency', 'expected_version': 1})
        assert changed.status_code == 200
        assert changed.json()['priority'] == 'emergency'
        assert client.post('/api/miniapp/tickets/one-ticket/priority', headers=headers,
                           json={'priority': 'urgent', 'expected_version': 1}).status_code == 409
        with db.connect(write=True) as conn:
            conn.execute(
                'INSERT INTO tickets(id,house_id,resident_id,category,location,description,status,priority,'
                'version,created_at,updated_at,due_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('second-ticket', 'one', 'resident', 'water', 'Подвал', 'Вода в подвале', 'new', 'normal', 1,
                 '2026-01-01T01:00:00+00:00', '2026-01-01T01:00:00+00:00', '2026-01-02T01:00:00+00:00'),
            )
        signal = client.get('/api/miniapp/overview', headers=headers).json()['signals']
        assert len(signal) == 1 and signal[0]['count'] == 2
        confirmed = client.post('/api/miniapp/incidents?house_id=one', headers=headers,
                                json={'ticket_ids': signal[0]['ticket_ids'], 'priority': 'urgent'})
        assert confirmed.status_code == 200
        assert confirmed.json()['count'] == 2
        assert client.get('/api/miniapp/overview', headers=headers).json()['signals'] == []


def test_public_entry_exposes_only_signed_miniapp_routes(tmp_path, monkeypatch):
    from app.public_miniapp import create_public_app
    monkeypatch.setenv('DOMPULSE_DB', str(tmp_path / 'public.db'))
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'test-webhook-secret')
    monkeypatch.setenv('MAX_BOT_TOKEN', TOKEN)
    with TestClient(create_public_app()) as client:
        assert client.get('/miniapp').status_code == 200
        assert client.get('/miniapp/assets/app.js').status_code == 200
        for path in ('/health', '/docs', '/openapi.json', '/api/tickets', '/webhooks/max'):
            assert client.get(path).status_code == 404
        assert client.get('/api/miniapp/overview').status_code == 401
        assert client.get('/api/miniapp/overview', headers={'Authorization': 'Bearer anything'}).status_code == 401



def test_public_code_login_is_single_use_and_role_scoped(tmp_path, monkeypatch):
    from app.public_miniapp import create_public_app

    path = str(tmp_path / 'code-login.db')
    monkeypatch.setenv('DOMPULSE_DB', path)
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'test-webhook-secret')
    monkeypatch.setenv('MAX_BOT_TOKEN', TOKEN)
    db = Database(path)
    db.initialize()
    with db.connect(write=True) as conn:
        conn.execute('INSERT INTO houses VALUES(?,?)', ('home', 'Дом 1'))
        conn.executemany('INSERT INTO users VALUES(?,?,?,?,?)', [
            ('operator', 'Диспетчер', 'operator', 'home', token_hash('operator-token')),
            ('resident', 'Житель', 'resident', 'home', token_hash('resident-token')),
        ])
        conn.executemany('INSERT INTO max_links VALUES(?,?,?)', [
            (101, 'operator', '2026-01-01T00:00:00+00:00'),
            (102, 'resident', '2026-01-01T00:00:00+00:00'),
        ])

    async def get_code(max_user_id):
        async with AsyncDatabase(path).connect(write=True) as conn:
            return await issue_code(conn, max_user_id)

    code = asyncio.run(get_code(101))
    resident_code = asyncio.run(get_code(102))
    with TestClient(create_public_app()) as client:
        assert client.get('/api/miniapp/overview').status_code == 401
        assert client.post('/api/miniapp/login', json={'code': 'WRONG-CODE'}).status_code == 401
        assert client.post('/api/miniapp/login', json={'code': resident_code}).status_code == 401
        login = client.post('/api/miniapp/login', json={'code': code.lower()})
        assert login.status_code == 200
        assert login.headers['cache-control'] == 'no-store'
        token = login.json()['token']
        assert client.post('/api/miniapp/login', json={'code': code}).status_code == 401
        headers = {'X-Miniapp-Session': token}
        assert client.get('/api/miniapp/overview', headers=headers).status_code == 200
        assert client.get('/api/miniapp/overview', headers={'X-Miniapp-Session': 'wrong'}).status_code == 401
        with db.connect(write=True) as conn:
            conn.execute('UPDATE miniapp_sessions SET expires_at=? WHERE token_hash=?',
                         ('2000-01-01T00:00:00+00:00', token_hash(token)))
        assert client.get('/api/miniapp/overview', headers=headers).status_code == 401
