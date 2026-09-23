import json
import pytest
from fastapi.testclient import TestClient
from app.api import create_app
from app.db import Database, token_hash


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'test-webhook-secret')
    path = str(tmp_path / 'test.db')
    db = Database(path)
    db.initialize()
    with db.connect(write=True) as conn:
        conn.executemany('INSERT INTO houses VALUES(?,?)', [('h1','Дом 1'), ('h2','Дом 2')])
        for uid, role, house in [('alice','resident','h1'), ('bob','resident','h1'),
                                  ('staff','operator','h1'), ('outsider','operator','h2')]:
            conn.execute('INSERT INTO users VALUES(?,?,?,?,?)', (uid,uid,role,house,token_hash(uid+'-test-only')))
    with TestClient(create_app(path)) as c:
        c.app.state.test_db = db
        yield c


def headers(user):
    return {'Authorization': 'Bearer '+user+'-test-only'}


def create(client):
    r = client.post('/api/tickets', headers=headers('alice'), json={
        'location': 'Подъезд 1, квартира 12', 'description': 'Со вчерашнего вечера холодные батареи.'})
    assert r.status_code == 201
    return r.json()


def change(client, ticket, status, user='staff'):
    return client.post('/api/tickets/'+ticket['id']+'/status', headers=headers(user), json={
        'status': status, 'comment': 'Проверка перехода статуса', 'expected_version': ticket['version']})


def test_complete_flow_with_reopening(client):
    ticket = create(client)
    for status, user in [('accepted','staff'), ('in_progress','staff'), ('resolved','staff'),
                         ('reopened','alice'), ('in_progress','staff'), ('resolved','staff'), ('confirmed','alice')]:
        response = change(client, ticket, status, user)
        assert response.status_code == 200, response.text
        ticket = response.json()
    assert ticket['status'] == 'confirmed'
    assert len(ticket['events']) == 8
    assert [e['status'] for e in ticket['events']] == [
        'new','accepted','in_progress','resolved','reopened','in_progress','resolved','confirmed']


@pytest.mark.parametrize('user', ['bob','outsider'])
def test_private_ticket_is_not_visible_or_writable(client,user):
    ticket = create(client)
    path = '/api/tickets/'+ticket['id']
    assert client.get('/api/tickets',headers=headers(user)).json() == []
    assert client.get(path,headers=headers(user)).status_code == 404
    assert client.post(path+'/comments',headers=headers(user),json={'text':'Чужая запись'}).status_code == 404
    assert change(client,ticket,'accepted',user).status_code == 404


def test_roles_cannot_skip_workflow(client):
    ticket = create(client)
    assert change(client,ticket,'accepted','alice').status_code == 409
    assert change(client,ticket,'confirmed','staff').status_code == 409
    assert change(client,ticket,'resolved','staff').status_code == 409
    assert client.get('/api/tickets/'+ticket['id'],headers=headers('alice')).json()['version'] == 1


def test_stale_update_does_not_overwrite_new_state(client):
    ticket = create(client)
    assert change(client,ticket,'accepted').status_code == 200
    assert change(client,ticket,'in_progress').status_code == 409
    current = client.get('/api/tickets/'+ticket['id'],headers=headers('staff')).json()
    assert current['status'] == 'accepted' and len(current['events']) == 2


def test_staff_reply_is_visible_to_resident(client):
    ticket = create(client)
    path = '/api/tickets/'+ticket['id']
    r = client.post(path+'/comments',headers=headers('staff'),json={'text':'Уточните время появления проблемы.'})
    assert r.status_code == 201
    fetched = client.get(path,headers=headers('alice')).json()
    assert fetched['events'][-1]['actor_role'] == 'operator'
    assert fetched['events'][-1]['text'] == 'Уточните время появления проблемы.'


def test_ticket_detail_returns_attachment_metadata(client):
    ticket = create(client)
    with client.app.state.test_db.connect(write=True) as conn:
        conn.execute(
            'INSERT INTO ticket_attachments(ticket_id,source,type,external_id,metadata_json,created_at) '
            'VALUES(?,?,?,?,?,?)',
            (ticket['id'], 'max', 'image', 'image-42', json.dumps({'width': 640, 'height': 480}),
             '2026-01-01T00:00:00+00:00'),
        )
    response = client.get('/api/tickets/'+ticket['id'], headers=headers('alice'))
    assert response.status_code == 200
    assert response.json()['attachments'] == [{
        'id': 1, 'source': 'max', 'type': 'image', 'external_id': 'image-42',
        'metadata': {'width': 640, 'height': 480}, 'created_at': '2026-01-01T00:00:00+00:00',
    }]


def test_auth_and_validation(client):
    assert client.get('/api/tickets').status_code == 401
    assert client.get('/api/tickets',headers=headers('unknown')).status_code == 401
    body={'location':'  ', 'description':'              '}
    assert client.post('/api/tickets',headers=headers('alice'),json=body).status_code == 422
    body={'location':'Квартира 1','description':'Достаточное описание проблемы','house_id':'h2'}
    assert client.post('/api/tickets',headers=headers('alice'),json=body).status_code == 422
    assert client.post('/api/tickets',headers=headers('staff'),json={
        'location':'Квартира 1','description':'Достаточное описание проблемы'}).status_code == 403
    assert client.get('/api/metrics/house', headers=headers('alice')).status_code == 403
    metrics = client.get('/api/metrics/house', headers=headers('staff'))
    assert metrics.status_code == 200
    assert metrics.json()['active'] == 0


def test_data_persists_after_app_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('MAX_WEBHOOK_SECRET', 'test-webhook-secret')
    path=str(tmp_path/'persistent.db')
    db=Database(path); db.initialize()
    with db.connect(write=True) as conn:
        conn.execute('INSERT INTO houses VALUES(?,?)',('h1','Дом 1'))
        conn.execute('INSERT INTO users VALUES(?,?,?,?,?)',('alice','Alice','resident','h1',token_hash('alice-test-only')))
    with TestClient(create_app(path)) as c:
        ticket=create(c)
    with TestClient(create_app(path)) as c:
        assert c.get('/api/tickets/'+ticket['id'],headers=headers('alice')).json()['description'] == ticket['description']


def test_announcements_operator_can_post_and_residents_receive(client):
    db = client.app.state.test_db
    # Привязать MAX-аккаунты жильцов к их пользователям
    with db.connect(write=True) as conn:
        conn.execute("INSERT INTO max_links(max_user_id, user_id, linked_at) VALUES(?,?,?)",
                     (111, 'alice', '2026-01-01T00:00:00+00:00'))
        conn.execute("INSERT INTO max_links(max_user_id, user_id, linked_at) VALUES(?,?,?)",
                     (222, 'bob', '2026-01-01T00:00:00+00:00'))

    response = client.post('/api/houses/h1/announcements', headers=headers('staff'), json={
        'title': 'Плановое отключение воды',
        'body': '25 сентября с 10:00 до 18:00 будет отключена холодная вода. Наберите запас заранее.',
    })
    assert response.status_code == 201
    data = response.json()
    assert data['title'] == 'Плановое отключение воды'
    assert data['sent_at'] is not None

    # Оба жильца получили уведомление в очередь
    with db.connect() as conn:
        count = conn.execute('SELECT COUNT(*) FROM max_outbox').fetchone()[0]
        messages = [dict(r) for r in conn.execute('SELECT * FROM max_outbox ORDER BY id').fetchall()]
    assert count == 2
    assert all('Плановое отключение воды' in m['text'] for m in messages)
    assert {m['max_user_id'] for m in messages} == {111, 222}

    # Жилец может получить список объявлений
    r = client.get('/api/houses/h1/announcements', headers=headers('alice'))
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Житель другого дома — 404
    r = client.get('/api/houses/h1/announcements', headers=headers('outsider'))
    assert r.status_code == 404

    # Житель не может создать объявление
    r = client.post('/api/houses/h1/announcements', headers=headers('alice'), json={
        'title': 'Тест', 'body': 'Тестовое объявление',
    })
    assert r.status_code == 403
