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
        assert client.get('/api/miniapp/overview', headers={'X-Max-Init-Data': signed_data(102)}).json()['role'] == 'resident'
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
        page = client.get('/miniapp')
        assert page.status_code == 200
        assert '<style>' in page.text
        assert 'document.addEventListener("DOMContentLoaded"' in page.text
        assert '/miniapp/assets/app.js?' not in page.text
        assert client.get('/').status_code == 200
        assert client.head('/miniapp').status_code == 200
        assert client.head('/').status_code == 200
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
import sqlite3

import pytest


def test_operator_district_migration_keeps_one_district_and_allows_colleagues(tmp_path):
    db = Database(str(tmp_path / 'districts.db'))
    db.initialize()
    with db.connect(write=True) as conn:
        conn.executemany('INSERT INTO houses(id,address) VALUES(?,?)', [
            ('one', 'Москва, Дом 1'), ('two', 'Москва, Дом 2'),
        ])
        conn.executemany('INSERT INTO house_districts(house_id,district) VALUES(?,?)', [
            ('one', 'ЦАО'), ('two', 'САО'),
        ])
        conn.executemany('INSERT INTO users(id,name,role,house_id,token_hash) VALUES(?,?,?,?,?)', [
            ('first', 'Первый', 'operator', 'one', token_hash('first-token')),
            ('second', 'Второй', 'operator', 'one', token_hash('second-token')),
        ])
        conn.execute('DROP INDEX uq_operator_user_district')
        conn.execute('CREATE UNIQUE INDEX uq_operator_district ON operator_districts(district)')
        conn.executemany('INSERT INTO operator_districts(user_id,district) VALUES(?,?)', [
            ('first', 'САО'), ('first', 'ЦАО'),
        ])
    db.initialize()
    with db.connect() as conn:
        assignments = conn.execute(
            'SELECT user_id,district FROM operator_districts ORDER BY user_id'
        ).fetchall()
        assert [(row['user_id'], row['district']) for row in assignments] == [
            ('first', 'ЦАО'), ('second', 'ЦАО'),
        ]
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(write=True) as conn:
            conn.execute(
                'INSERT INTO operator_districts(user_id,district) VALUES(?,?)',
                ('first', 'САО'),
            )


def test_operators_share_district_and_miniapp_filters_other_districts(tmp_path, monkeypatch):
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'test-secret')
    monkeypatch.setenv('MAX_BOT_TOKEN', TOKEN)
    path = str(tmp_path / 'team.db')
    db = Database(path)
    db.initialize()
    with db.connect(write=True) as conn:
        conn.executemany('INSERT INTO houses(id,address) VALUES(?,?)', [
            ('one', 'Москва, Дом 1'), ('two', 'Москва, Дом 2'), ('three', 'Москва, Дом 3'),
        ])
        conn.executemany('INSERT INTO house_districts(house_id,district) VALUES(?,?)', [
            ('one', 'ЦАО'), ('two', 'ЦАО'), ('three', 'САО'),
        ])
        conn.executemany('INSERT INTO users(id,name,role,house_id,token_hash) VALUES(?,?,?,?,?)', [
            ('first', 'Первый', 'operator', 'one', token_hash('first-token')),
            ('second', 'Второй', 'operator', 'two', token_hash('second-token')),
        ])
        conn.executemany('INSERT INTO max_links(max_user_id,user_id,linked_at) VALUES(?,?,?)', [
            (201, 'first', '2026-01-01T00:00:00+00:00'),
            (202, 'second', '2026-01-01T00:00:00+00:00'),
        ])
    db.initialize()
    with TestClient(create_app(path)) as client:
        for user_id in (201, 202):
            headers = {'X-Max-Init-Data': signed_data(user_id)}
            overview = client.get('/api/miniapp/overview', headers=headers)
            assert overview.status_code == 200
            assert overview.json()['operator']['district'] == 'ЦАО'
            assert {house['id'] for house in overview.json()['houses']} == {'one', 'two'}
            assert client.get('/api/miniapp/overview?house_id=two', headers=headers).status_code == 200
            assert client.get('/api/miniapp/overview?house_id=three', headers=headers).status_code == 404
        with db.connect(write=True) as conn:
            conn.execute(
                "UPDATE operator_districts SET district='САО' WHERE user_id='first'"
            )
        first_headers = {'X-Max-Init-Data': signed_data(201)}
        reassigned = client.get('/api/miniapp/overview', headers=first_headers)
        assert reassigned.status_code == 200
        assert reassigned.json()['house_id'] == 'three'
        assert [house['id'] for house in reassigned.json()['houses']] == ['three']
        assert client.get('/api/miniapp/overview?house_id=one', headers=first_headers).status_code == 404
        assert {house['id'] for house in client.get(
            '/api/miniapp/overview', headers={'X-Max-Init-Data': signed_data(202)}
        ).json()['houses']} == {'one', 'two'}


def test_resident_miniapp_follows_verified_houses_and_owns_tickets(tmp_path, monkeypatch):
    from app.public_miniapp import create_public_app

    path = str(tmp_path / 'resident-miniapp.db')
    monkeypatch.setenv('DOMPULSE_DB', path)
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'test-webhook-secret')
    monkeypatch.setenv('MAX_BOT_TOKEN', TOKEN)
    db = Database(path)
    db.initialize()
    with db.connect(write=True) as conn:
        conn.executemany('INSERT INTO houses(id,address) VALUES(?,?)', [
            ('one', 'Москва, первый дом'), ('two', 'Москва, второй дом'), ('third', 'Москва, третий дом'),
        ])
        conn.executemany('INSERT INTO users(id,name,role,house_id,token_hash) VALUES(?,?,?,?,?)', [
            ('resident', 'Житель', 'resident', 'one', token_hash('resident-secret')),
            ('other', 'Другой житель', 'resident', 'two', token_hash('other-secret')),
            ('operator', 'Оператор', 'operator', 'one', token_hash('operator-secret')),
        ])
        conn.execute('INSERT INTO max_links(max_user_id,user_id,linked_at) VALUES(?,?,?)',
                     (102, 'resident', '2026-01-01T00:00:00+00:00'))
        conn.executemany('INSERT INTO user_houses(user_id,house_id,verification_method,verified_at,revoked_at) VALUES(?,?,?,?,?)', [
            ('resident', 'one', 'code', '2026-01-01T00:00:00+00:00', None),
            ('resident', 'two', 'code', '2026-01-01T00:00:00+00:00', None),
            ('resident', 'third', 'code', '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00'),
        ])
        conn.executemany('INSERT INTO tickets(id,house_id,resident_id,category,location,description,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)', [
            ('mine-one', 'one', 'resident', 'water', 'Подвал', 'Течёт труба в подвале', 'new', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
            ('mine-two', 'two', 'resident', 'yard', 'Двор', 'Не убран мусор во дворе', 'resolved', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
            ('foreign', 'two', 'other', 'water', 'Кухня', 'Чужая заявка жителя', 'new', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
        ])
        conn.executemany('INSERT INTO announcements(id,house_id,title,body,created_by,created_at) VALUES(?,?,?,?,?,?)', [
            ('notice-one', 'one', 'О доме 1', 'Работы в первом доме', 'operator', '2026-01-01T00:00:00+00:00'),
            ('notice-two', 'two', 'О доме 2', 'Работы во втором доме', 'operator', '2026-01-01T00:00:00+00:00'),
        ])
    headers = {'X-Max-Init-Data': signed_data(102)}
    with TestClient(create_public_app()) as client:
        first = client.get('/api/miniapp/overview', headers=headers)
        assert first.status_code == 200
        assert first.json()['role'] == 'resident'
        assert {house['id'] for house in first.json()['houses']} == {'one', 'two'}
        assert [ticket['id'] for ticket in first.json()['tickets']] == ['mine-one']
        assert [item['id'] for item in first.json()['announcements']] == ['notice-one']
        second = client.get('/api/miniapp/overview?house_id=two', headers=headers)
        assert [ticket['id'] for ticket in second.json()['tickets']] == ['mine-two']
        assert [item['id'] for item in second.json()['announcements']] == ['notice-two']
        assert client.get('/api/miniapp/overview?house_id=third', headers=headers).status_code == 404
        assert client.get('/api/tickets/foreign', headers=headers).status_code == 404
        assert client.post('/api/tickets', headers=headers, json={
            'house_id': 'third', 'category': 'water', 'location': 'Подвал', 'description': 'Вода возле труб в подвале',
        }).status_code == 404
        created = client.post('/api/tickets', headers=headers, json={
            'house_id': 'two', 'category': 'water', 'location': 'Подвал', 'description': 'Вода возле труб в подвале',
        })
        assert created.status_code == 201
        assert created.json()['house_id'] == 'two'
        assert client.post('/api/tickets/mine-two/status', headers=headers, json={
            'status': 'confirmed', 'comment': 'Проблема решена', 'expected_version': 1,
        }).status_code == 200
        assert client.post('/api/miniapp/tickets/mine-one/priority', headers=headers,
                           json={'priority': 'urgent', 'expected_version': 1}).status_code == 403
        assert client.post('/api/tickets', headers={'Authorization': 'Bearer resident-secret'}, json={
            'house_id': 'one', 'location': 'Подвал', 'description': 'Вода возле труб в подвале',
        }).status_code == 401
