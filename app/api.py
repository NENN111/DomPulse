import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import AsyncDatabase, token_hash
from .models import CommentCreate, Profile, StatusChange, Ticket, TicketCreate, TicketDetail
from .analytics import house_metrics
from .max_webhook import keyboard, parse_update, queue_message, save_dialog, store_update, verify_secret
from .sla import calculate_due_at

# 'resolved' records the operator's report, 'confirmed' records the resident's response.
OPERATOR_TRANSITIONS = {
    'new': {'accepted'},
    'accepted': {'in_progress'},
    'in_progress': {'resolved'},
    'reopened': {'in_progress'},
}
RESIDENT_TRANSITIONS = {'resolved': {'confirmed', 'reopened'}}


def now():
    return datetime.now(timezone.utc).isoformat()


def create_app(db_path: str | None = None):
    configured_db_path = db_path if db_path is not None else os.getenv('DOMPULSE_DB')
    if not configured_db_path:
        raise RuntimeError('DOMPULSE_DB is required. Set it explicitly before starting the API.')
    webhook_secret = os.getenv('MAX_WEBHOOK_SECRET')
    if not webhook_secret:
        raise RuntimeError('MAX_WEBHOOK_SECRET is required. Set it explicitly before starting the API.')
    db = AsyncDatabase(configured_db_path)

    @asynccontextmanager
    async def lifespan(app):
        await db.initialize()
        yield

    app = FastAPI(
        title='ДомПульс — API прототипа', version='0.1.0', lifespan=lifespan,
        description='Ядро обращений и транспорт webhook MAX. Внешние системы УК ещё не подключены.',
    )
    auth = HTTPBearer(auto_error=False)

    async def actor(credentials: HTTPAuthorizationCredentials | None = Depends(auth)):
        if credentials is None:
            raise HTTPException(401, 'Требуется авторизация', headers={'WWW-Authenticate': 'Bearer'})
        async with db.connect() as conn:
            cursor = await conn.execute(
                'SELECT u.id,u.name,u.role,u.house_id,h.address FROM users u '
                'JOIN houses h ON h.id=u.house_id WHERE token_hash=?',
                (token_hash(credentials.credentials),),
            )
            row = await cursor.fetchone()
        if row is None:
            raise HTTPException(401, 'Недействительный токен', headers={'WWW-Authenticate': 'Bearer'})
        return dict(row)

    async def accessible(conn, ticket_id, user):
        cursor = await conn.execute('SELECT * FROM tickets WHERE id=?', (ticket_id,))
        row = await cursor.fetchone()
        if row is None or row['house_id'] != user['house_id']:
            raise HTTPException(404, 'Обращение не найдено')
        if user['role'] == 'resident' and row['resident_id'] != user['id']:
            raise HTTPException(404, 'Обращение не найдено')
        return dict(row)

    async def detail(conn, ticket):
        cursor = await conn.execute(
            'SELECT e.id,u.name AS actor_name,u.role AS actor_role,e.kind,e.status,e.text,e.created_at '
            'FROM events e JOIN users u ON u.id=e.actor_id WHERE e.ticket_id=? ORDER BY e.id',
            (ticket['id'],),
        )
        events = await cursor.fetchall()
        cursor = await conn.execute(
            'SELECT id,source,type,external_id,metadata_json,created_at '
            'FROM ticket_attachments WHERE ticket_id=? ORDER BY id',
            (ticket['id'],),
        )
        attachments = []
        for row in await cursor.fetchall():
            item = dict(row)
            item['metadata'] = json.loads(item.pop('metadata_json'))
            attachments.append(item)
        return {**ticket, 'events': [dict(e) for e in events], 'attachments': attachments}

    async def event(conn, ticket_id, user, kind, status, text, timestamp):
        await conn.execute(
            'INSERT INTO events(ticket_id,actor_id,kind,status,text,created_at) VALUES(?,?,?,?,?,?)',
            (ticket_id, user['id'], kind, status, text, timestamp),
        )

    async def notify_resident(conn, ticket, text, timestamp, confirm=False):
        cursor = await conn.execute('SELECT max_user_id FROM max_links WHERE user_id=?',
                                    (ticket['resident_id'],))
        link = await cursor.fetchone()
        if link is None:
            return
        attachments = keyboard([['Мои обращения']])
        if confirm:
            attachments = keyboard([['Да, всё решено'], ['Проблема осталась']])
            await save_dialog(conn, link['max_user_id'], 'confirm', {'ticket_id': ticket['id']}, timestamp)
        await queue_message(conn, link['max_user_id'], text, attachments, timestamp)

    @app.get('/health')
    async def health():
        async with db.connect() as conn:
            cursor = await conn.execute('SELECT 1')
            await cursor.fetchone()
        return {'status': 'ok', 'max_configured': True}

    @app.post('/webhooks/max')
    async def max_webhook(
        request: Request,
        webhook_secret_header: str | None = Header(None, alias='X-Max-Bot-Api-Secret'),
    ):
        verify_secret(webhook_secret, webhook_secret_header)
        payload = await parse_update(request)
        inserted = await store_update(db, payload)
        return {'ok': True, 'duplicate': not inserted}

    @app.get('/api/me', response_model=Profile)
    async def me(user=Depends(actor)):
        return user

    @app.get('/api/metrics/house')
    async def metrics(user=Depends(actor)):
        if user['role'] != 'operator':
            raise HTTPException(403, 'Показатели дома доступны сотруднику УК')
        async with db.connect() as conn:
            result = await house_metrics(conn, user['house_id'])
        result.pop('tickets')
        return result

    @app.post('/api/tickets', response_model=TicketDetail, status_code=201)
    async def create_ticket(body: TicketCreate, user=Depends(actor)):
        if user['role'] != 'resident':
            raise HTTPException(403, 'Обращение создаёт житель')
        ticket_id, timestamp = str(uuid4()), now()
        async with db.connect(write=True) as conn:
            await conn.execute(
                'INSERT INTO tickets(id,house_id,resident_id,category,location,description,status,created_at,updated_at) '
                "VALUES(?,?,?,?,?,?,'new',?,?)",
                (ticket_id, user['house_id'], user['id'], body.category, body.location,
                 body.description, timestamp, timestamp),
            )
            await conn.execute('UPDATE tickets SET due_at=? WHERE id=?',
                               (calculate_due_at('normal', timestamp), ticket_id))
            await event(conn, ticket_id, user, 'created', 'new', body.description, timestamp)
            return await detail(conn, await accessible(conn, ticket_id, user))

    @app.get('/api/tickets', response_model=list[Ticket])
    async def list_tickets(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), user=Depends(actor)):
        clause, args = 'house_id=?', [user['house_id']]
        if user['role'] == 'resident':
            clause += ' AND resident_id=?'
            args.append(user['id'])
        async with db.connect() as conn:
            cursor = await conn.execute(
                f'SELECT * FROM tickets WHERE {clause} ORDER BY created_at DESC, id LIMIT ? OFFSET ?',
                (*args, limit, offset),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    @app.get('/api/tickets/{ticket_id}', response_model=TicketDetail)
    async def get_ticket(ticket_id: str, user=Depends(actor)):
        async with db.connect() as conn:
            return await detail(conn, await accessible(conn, ticket_id, user))

    @app.post('/api/tickets/{ticket_id}/comments', response_model=TicketDetail, status_code=201)
    async def add_comment(ticket_id: str, body: CommentCreate, user=Depends(actor)):
        async with db.connect(write=True) as conn:
            ticket = await accessible(conn, ticket_id, user)
            timestamp = now()
            await event(conn, ticket_id, user, 'comment', ticket['status'], body.text, timestamp)
            await conn.execute('UPDATE tickets SET updated_at=?,version=version+1 WHERE id=?', (timestamp, ticket_id))
            if user['role'] == 'operator':
                await conn.execute('UPDATE tickets SET first_response_at=COALESCE(first_response_at,?) WHERE id=?',
                                   (timestamp, ticket_id))
                await notify_resident(conn, ticket, f"Ответ УК по обращению №{ticket_id[:8]}: {body.text}", timestamp)
            return await detail(conn, await accessible(conn, ticket_id, user))

    @app.post('/api/tickets/{ticket_id}/status', response_model=TicketDetail)
    async def change_status(ticket_id: str, body: StatusChange, user=Depends(actor)):
        async with db.connect(write=True) as conn:
            ticket = await accessible(conn, ticket_id, user)
            if ticket['version'] != body.expected_version:
                raise HTTPException(409, 'Карточка изменилась. Обновите её и повторите действие.')
            transitions = OPERATOR_TRANSITIONS if user['role'] == 'operator' else RESIDENT_TRANSITIONS
            if body.status.value not in transitions.get(ticket['status'], set()):
                raise HTTPException(409, 'Переход недоступен для вашей роли и текущего статуса')
            timestamp = now()
            await conn.execute('UPDATE tickets SET status=?,updated_at=?,version=version+1 WHERE id=?',
                               (body.status.value, timestamp, ticket_id))
            if user['role'] == 'operator':
                await conn.execute('UPDATE tickets SET first_response_at=COALESCE(first_response_at,?) WHERE id=?',
                                   (timestamp, ticket_id))
            if body.status.value == 'confirmed':
                await conn.execute('UPDATE tickets SET closed_at=? WHERE id=?', (timestamp, ticket_id))
            elif body.status.value == 'reopened':
                await conn.execute('UPDATE tickets SET closed_at=NULL WHERE id=?', (ticket_id,))
            await event(conn, ticket_id, user, 'status_changed', body.status.value, body.comment, timestamp)
            if user['role'] == 'operator':
                label = {
                    'accepted': 'принято', 'in_progress': 'в работе', 'resolved': 'отмечено УК как выполненное'
                }[body.status.value]
                await notify_resident(
                    conn, ticket,
                    f"Обращение №{ticket_id[:8]}: {label}.\nКомментарий: {body.comment}",
                    timestamp, confirm=body.status.value == 'resolved',
                )
            return await detail(conn, await accessible(conn, ticket_id, user))

    return app
