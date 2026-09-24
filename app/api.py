import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import AsyncDatabase, token_hash
from .models import AnnouncementCreate, Announcement, CommentCreate, GosuslugiResidencyClaim, Profile, StatusChange, Ticket, TicketCreate, TicketDetail
from .analytics import house_metrics
from .access import allowed_house_ids, can_access_house
from .max_webhook import PRIORITY_LABELS, announce_incident, keyboard, parse_update, queue_message, save_dialog, similar_groups, store_update, verify_secret
from .miniapp_auth import InvalidLaunchData, validate_launch_data
from .miniapp_login import redeem_code, session_user_id
from .sla import calculate_due_at
from .residency import complete_verified_link

# 'resolved' records the operator's report, 'confirmed' records the resident's response.
OPERATOR_TRANSITIONS = {
    'new': {'accepted'},
    'accepted': {'in_progress'},
    'in_progress': {'resolved'},
    'reopened': {'in_progress'},
}
RESIDENT_TRANSITIONS = {'resolved': {'confirmed', 'reopened'}}
WEB_DIR = Path(__file__).resolve().parent.parent / 'web'


class MiniappLogin(BaseModel):
    code: str


class PriorityChange(BaseModel):
    priority: str
    expected_version: int


class IncidentConfirm(BaseModel):
    ticket_ids: list[str]
    priority: str


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
    bridge_url = os.getenv('GOSUSLUGI_BRIDGE_URL')
    bridge_secret = os.getenv('GOSUSLUGI_BRIDGE_SECRET')
    if bool(bridge_url) != bool(bridge_secret):
        raise RuntimeError('GOSUSLUGI_BRIDGE_URL and GOSUSLUGI_BRIDGE_SECRET must be set together.')
    if bridge_url and not bridge_url.startswith('https://'):
        raise RuntimeError('GOSUSLUGI_BRIDGE_URL must use HTTPS.')
    if bridge_secret and len(bridge_secret) < 32:
        raise RuntimeError('GOSUSLUGI_BRIDGE_SECRET must contain at least 32 characters.')

    @asynccontextmanager
    async def lifespan(app):
        await db.initialize()
        yield

    app = FastAPI(
        title='ДомПульс — API прототипа', version='0.1.0', lifespan=lifespan,
        description='Ядро обращений и транспорт webhook MAX. Внешние системы УК ещё не подключены.',
    )
    auth = HTTPBearer(auto_error=False)

    async def actor(
        credentials: HTTPAuthorizationCredentials | None = Depends(auth),
        x_max_init_data: str | None = Header(default=None),
        x_miniapp_session: str | None = Header(default=None),
    ):
        max_user_id = None
        if x_max_init_data is not None:
            try:
                max_user_id = validate_launch_data(x_max_init_data, os.getenv('MAX_BOT_TOKEN', ''))
            except InvalidLaunchData as exc:
                raise HTTPException(401, str(exc)) from exc
        elif x_miniapp_session is not None:
            max_user_id = await session_user_id(db, x_miniapp_session)
            if max_user_id is None:
                raise HTTPException(401, 'Срок входа истёк. Запросите новый код в боте.')
        if max_user_id is not None:
            async with db.connect() as conn:
                cursor = await conn.execute(
                    'SELECT u.id,u.name,u.role,u.house_id,h.address FROM max_links l '
                    'JOIN users u ON u.id=l.user_id JOIN houses h ON h.id=u.house_id '
                    'WHERE l.max_user_id=?', (max_user_id,),
                )
                row = await cursor.fetchone()
            if row is None or row['role'] != 'operator':
                raise HTTPException(403, 'Доступно только сотруднику УК')
            return dict(row)
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
        if row is None:
            raise HTTPException(404, 'Обращение не найдено')
        if user['role'] == 'resident':
            if row['resident_id'] != user['id']:
                raise HTTPException(404, 'Обращение не найдено')
        elif not await can_access_house(conn, user, row['house_id']):
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

    @app.post('/api/integrations/gosuslugi/residency')
    async def gosuslugi_residency(
        request: Request,
        claim: GosuslugiResidencyClaim,
        x_dompulse_signature: str | None = Header(default=None),
    ):
        if not bridge_secret:
            raise HTTPException(503, 'Интеграция с Госуслугами не настроена')
        raw = await request.body()
        supplied = (x_dompulse_signature or '').removeprefix('sha256=')
        expected = hmac.new(bridge_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, 'Недействительная подпись интеграции')
        async with db.connect(write=True) as conn:
            status, house = await complete_verified_link(conn, claim.model_dump(), now())
        if status == 'expired':
            raise HTTPException(410, 'Ссылка подтверждения истекла')
        if status in {'invalid', 'already_linked', 'subject_linked'}:
            raise HTTPException(409, 'Подтверждение уже использовано или недействительно')
        return {'status': status, 'linked': status == 'completed', 'house_id': house['id'] if house else None}

    @app.get('/health')
    async def health():
        async with db.connect() as conn:
            cursor = await conn.execute('SELECT 1')
            await cursor.fetchone()
        return {'status': 'ok', 'max_configured': True}

    @app.get('/miniapp', include_in_schema=False)
    async def miniapp_page():
        return FileResponse(WEB_DIR / 'index.html', headers={'Cache-Control': 'no-store'})

    app.mount('/miniapp/assets', StaticFiles(directory=WEB_DIR), name='miniapp-assets')

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
            result = await house_metrics(conn, house_ids=await allowed_house_ids(conn, user))
        result.pop('tickets')
        return result

    @app.post('/api/miniapp/login')
    async def miniapp_login(body: MiniappLogin, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        if len(body.code) > 32:
            raise HTTPException(401, 'Код недействителен или истёк')
        result = await redeem_code(db, body.code)
        if result is None:
            raise HTTPException(401, 'Код недействителен или истёк')
        token, expires_at = result
        return {'token': token, 'expires_at': expires_at}

    @app.get('/api/miniapp/overview')
    async def miniapp_overview(house_id: str | None = None, user=Depends(actor)):
        if user['role'] != 'operator':
            raise HTTPException(403, 'Доступно только сотруднику УК')
        async with db.connect() as conn:
            ids = await allowed_house_ids(conn, user)
            selected = house_id or user['house_id']
            if selected not in ids:
                raise HTTPException(404, 'Дом не найден')
            marks = ','.join('?' for _ in ids)
            rows = await (await conn.execute(
                f'SELECT id,address FROM houses WHERE id IN ({marks}) ORDER BY address', ids,
            )).fetchall()
            metrics = await house_metrics(conn, house_id=selected)
            tickets = metrics.pop('tickets')
            tickets.sort(key=lambda ticket: ticket['created_at'], reverse=True)
            linked = await (await conn.execute(
                'SELECT ticket_id FROM incident_tickets WHERE ticket_id IN '
                '(SELECT id FROM tickets WHERE house_id=?)', (selected,),
            )).fetchall()
            linked_ids = {row['ticket_id'] for row in linked}
            groups = similar_groups([
                ticket for ticket in tickets
                if ticket['status'] != 'confirmed' and ticket['id'] not in linked_ids
            ])
            signals = [{
                'ticket_ids': [ticket['id'] for ticket in group],
                'category': group[0]['category'], 'location': group[0]['location'],
                'count': len(group), 'description': group[0]['description'],
            } for group in groups]
            announcements = await (await conn.execute(
                'SELECT id,title,created_at FROM announcements WHERE house_id=? '
                'ORDER BY created_at DESC LIMIT 3', (selected,),
            )).fetchall()
            return {
                'operator': {'name': user['name']}, 'houses': [dict(row) for row in rows],
                'house_id': selected, 'metrics': metrics, 'tickets': tickets,
                'signals': signals, 'announcements': [dict(row) for row in announcements],
            }

    @app.post('/api/miniapp/tickets/{ticket_id}/priority')
    async def miniapp_priority(ticket_id: str, body: PriorityChange, user=Depends(actor)):
        if user['role'] != 'operator':
            raise HTTPException(403, 'Доступно только сотруднику УК')
        if body.priority not in {'normal', 'urgent', 'emergency'}:
            raise HTTPException(422, 'Недопустимая срочность')
        async with db.connect(write=True) as conn:
            ticket = await accessible(conn, ticket_id, user)
            if ticket['version'] != body.expected_version:
                raise HTTPException(409, 'Карточка изменилась. Обновите её.')
            timestamp = now()
            await conn.execute(
                'UPDATE tickets SET priority=?,due_at=?,first_response_at=COALESCE(first_response_at,?),updated_at=?,version=version+1 WHERE id=?',
                (body.priority, calculate_due_at(body.priority, ticket['created_at']), timestamp, timestamp, ticket_id),
            )
            await event(conn, ticket_id, user, 'priority_changed', ticket['status'],
                        f'Диспетчер установил приоритет: {PRIORITY_LABELS[body.priority]}.', timestamp)
            return await detail(conn, await accessible(conn, ticket_id, user))

    @app.post('/api/miniapp/incidents')
    async def miniapp_confirm_incident(body: IncidentConfirm, house_id: str, user=Depends(actor)):
        if user['role'] != 'operator':
            raise HTTPException(403, 'Доступно только сотруднику УК')
        if body.priority not in {'normal', 'urgent', 'emergency'} or not 2 <= len(body.ticket_ids) <= 20:
            raise HTTPException(422, 'Недопустимые параметры сигнала')
        if len(set(body.ticket_ids)) != len(body.ticket_ids):
            raise HTTPException(422, 'Повторяющиеся обращения')
        async with db.connect(write=True) as conn:
            if not await can_access_house(conn, user, house_id):
                raise HTTPException(404, 'Дом не найден')
            marks = ','.join('?' for _ in body.ticket_ids)
            tickets = await (await conn.execute(
                f'SELECT * FROM tickets WHERE id IN ({marks})', body.ticket_ids,
            )).fetchall()
            if len(tickets) != len(body.ticket_ids) or any(ticket['house_id'] != house_id for ticket in tickets):
                raise HTTPException(404, 'Сигнал не найден')
            linked = await (await conn.execute(
                f'SELECT 1 FROM incident_tickets WHERE ticket_id IN ({marks}) LIMIT 1',
                body.ticket_ids,
            )).fetchone()
            if linked or any(ticket['status'] == 'confirmed' for ticket in tickets):
                raise HTTPException(409, 'Сигнал уже обработан')
            if not any(len(group) == len(tickets) for group in similar_groups([dict(row) for row in tickets])):
                raise HTTPException(409, 'Обращения больше не похожи')
            selected_user = {**user, 'house_id': house_id}
            result = await announce_incident(conn, selected_user, body.ticket_ids, body.priority, now())
            if result is None:
                raise HTTPException(409, 'Сигнал устарел')
            return {'id': result[0], 'count': result[1]}

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
        async with db.connect() as conn:
            if user['role'] == 'resident':
                clause, args = 'resident_id=?', [user['id']]
            else:
                houses = await allowed_house_ids(conn, user)
                marks = ','.join('?' for _ in houses)
                clause, args = f'house_id IN ({marks})', list(houses)
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

    @app.post('/api/houses/{house_id}/announcements', response_model=Announcement, status_code=201)
    async def create_announcement(house_id: str, body: AnnouncementCreate, user=Depends(actor)):
        """Оператор УК публикует объявление: бот мгновенно рассылает его всем жильцам дома в MAX."""
        if user['role'] != 'operator':
            raise HTTPException(403, 'Объявления создаёт сотрудник УК')
        async with db.connect() as conn:
            if not await can_access_house(conn, user, house_id):
                raise HTTPException(404, 'Дом не найден')
        announcement_id = str(uuid4())
        timestamp = now()
        text = f'\U0001f4e3 Объявление от УК\n\n{body.title}\n\n{body.body}'
        async with db.connect(write=True) as conn:
            await conn.execute(
                'INSERT INTO announcements(id,house_id,title,body,created_by,created_at) VALUES(?,?,?,?,?,?)',
                (announcement_id, house_id, body.title, body.body, user['id'], timestamp),
            )
            # Найти всех жильцов дома с привязанным MAX-аккаунтом
            cursor = await conn.execute(
                "SELECT l.max_user_id FROM max_links l JOIN users u ON u.id=l.user_id WHERE u.house_id=? AND u.role='resident'",
                (house_id,)
            )
            residents = await cursor.fetchall()
            for row in residents:
                await queue_message(
                    conn, row['max_user_id'], text,
                    keyboard([['Объявления дома'], ['Мои обращения']]),
                    timestamp,
                )
            await conn.execute('UPDATE announcements SET sent_at=? WHERE id=?', (timestamp, announcement_id))
            cursor = await conn.execute('SELECT * FROM announcements WHERE id=?', (announcement_id,))
            return dict(await cursor.fetchone())

    @app.get('/api/houses/{house_id}/announcements', response_model=list[Announcement])
    async def list_announcements(
        house_id: str,
        limit: int = Query(20, ge=1, le=100),
        user=Depends(actor),
    ):
        """Последние объявления дома — доступно и жильцам, и операторам."""
        async with db.connect() as conn:
            if not await can_access_house(conn, user, house_id):
                raise HTTPException(404, 'Дом не найден')
            cursor = await conn.execute(
                'SELECT * FROM announcements WHERE house_id=? ORDER BY created_at DESC LIMIT ?',
                (house_id, limit),
            )
            return [dict(row) for row in await cursor.fetchall()]

    return app
