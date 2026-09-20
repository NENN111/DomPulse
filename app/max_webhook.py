import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request

from .db import AsyncDatabase, token_hash
from .analytics import house_metrics
from .sla import calculate_due_at, is_overdue

MAX_WEBHOOK_BYTES = 256 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_secret(expected: str | None, supplied: str | None):
    if not expected:
        raise HTTPException(503, 'MAX webhook is not configured')
    if supplied is None or not hmac.compare_digest(expected, supplied):
        raise HTTPException(401, 'Invalid MAX webhook secret')


async def parse_update(request: Request) -> dict[str, Any]:
    content_length = request.headers.get('content-length')
    if content_length:
        try:
            if int(content_length) > MAX_WEBHOOK_BYTES:
                raise HTTPException(413, 'MAX update is too large')
        except ValueError as exc:
            raise HTTPException(400, 'Invalid Content-Length') from exc
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise HTTPException(413, 'MAX update is too large')
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, 'Invalid JSON') from exc
    if not isinstance(payload, dict) or not isinstance(payload.get('update_type'), str):
        raise HTTPException(400, 'update_type is required')
    return payload


def fingerprint(payload: dict[str, Any]) -> str:
    message = payload.get('message') or {}
    message_body = message.get('body') or {} if isinstance(message, dict) else {}
    mid = message_body.get('mid') if isinstance(message_body, dict) else None
    stable = f"{payload['update_type']}:{mid}" if mid else json.dumps(
        payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False
    )
    return hashlib.sha256(stable.encode()).hexdigest()


def max_user_id(payload: dict[str, Any]) -> int | None:
    message = payload.get('message')
    if isinstance(message, dict):
        sender = message.get('sender')
        if isinstance(sender, dict) and isinstance(sender.get('user_id'), int):
            return sender['user_id']
    user = payload.get('user')
    if isinstance(user, dict) and isinstance(user.get('user_id'), int):
        return user['user_id']
    return None


CATEGORIES = {
    'Отопление': 'heating',
    'Вода': 'water',
    'Электричество': 'electricity',
    'Лифт': 'elevator',
    'Уборка': 'cleaning',
    'Двор': 'yard',
    'Другое': 'other',
}
CATEGORY_LABELS = {value: key for key, value in CATEGORIES.items()}
STATUS_LABELS = {
    'new': 'новое', 'accepted': 'принято', 'in_progress': 'в работе',
    'resolved': 'ожидает проверки', 'confirmed': 'выполнено', 'reopened': 'возвращено в работу',
}
PRIORITY_LABELS = {'normal': 'обычная', 'urgent': 'срочно', 'emergency': 'авария'}
OPERATOR_ACTIONS = {
    'new': ('Принять', 'accepted'),
    'accepted': ('Начать работу', 'in_progress'),
    'in_progress': ('Отметить выполнено', 'resolved'),
    'reopened': ('Вернуть в работу', 'in_progress'),
}


def keyboard(rows: list[list[str]]) -> list[dict[str, Any]]:
    return [{
        'type': 'inline_keyboard',
        'payload': {'buttons': [[{'type': 'message', 'text': label} for label in row] for row in rows]},
    }]


async def queue_message(conn, user_id: int, text: str, attachments: list[dict[str, Any]], timestamp: str):
    await conn.execute(
        'INSERT INTO max_outbox(max_user_id,text,attachments_json,next_attempt_at,created_at) '
        'VALUES(?,?,?,?,?)',
        (user_id, text, json.dumps(attachments, ensure_ascii=False), timestamp, timestamp),
    )


def menu(role: str = 'resident') -> tuple[str, list[dict[str, Any]]]:
    if role == 'operator':
        return (
            'ДомПульс: рабочее место диспетчера УК. Откройте очередь своего дома.',
            keyboard([['Очередь дома'], ['Общие проблемы дома'], ['Показатели дома']]),
        )
    return (
        'ДомПульс связывает жильца с управляющей компанией. Что хотите сделать?',
        keyboard([['Сообщить о проблеме'], ['Мои обращения']]),
    )


def message_text(payload: dict[str, Any]) -> str:
    message = payload.get('message')
    if not isinstance(message, dict):
        return ''
    body = message.get('body')
    return (body.get('text') or '').strip() if isinstance(body, dict) else ''


def stored_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Avoid retaining a one-time enrollment code in the webhook event log."""
    safe = json.loads(json.dumps(payload, ensure_ascii=False))
    text = message_text(payload)
    if text.lower().startswith('/код '):
        message = safe.get('message')
        if isinstance(message, dict) and isinstance(message.get('body'), dict):
            message['body']['text'] = '/код [REDACTED]'
    return safe


async def linked_user(conn, user_id: int):
    cursor = await conn.execute(
        'SELECT u.* FROM max_links l JOIN users u ON u.id=l.user_id WHERE l.max_user_id=?',
        (user_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def record_failed_link_attempt(conn, user_id: int, timestamp: str) -> bool:
    cursor = await conn.execute('SELECT * FROM max_link_attempts WHERE max_user_id=?', (user_id,))
    attempt = await cursor.fetchone()
    if attempt and attempt['blocked_until'] and attempt['blocked_until'] > timestamp:
        return True
    failures = (attempt['failed_count'] if attempt else 0) + 1
    blocked_until = None
    if failures >= 5:
        blocked_until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        failures = 0
    await conn.execute(
        'INSERT INTO max_link_attempts(max_user_id,failed_count,blocked_until) VALUES(?,?,?) '
        'ON CONFLICT(max_user_id) DO UPDATE SET failed_count=excluded.failed_count,blocked_until=excluded.blocked_until',
        (user_id, failures, blocked_until),
    )
    return blocked_until is not None


async def enroll_with_code(conn, user_id: int, payload: dict[str, Any], code: str, timestamp: str):
    cursor = await conn.execute('SELECT * FROM max_link_attempts WHERE max_user_id=?', (user_id,))
    attempt = await cursor.fetchone()
    if attempt and attempt['blocked_until'] and attempt['blocked_until'] > timestamp:
        return None, 'Слишком много попыток. Повторите через 15 минут.'
    normalized = code.strip().upper()
    if not 8 <= len(normalized) <= 64:
        blocked = await record_failed_link_attempt(conn, user_id, timestamp)
        return None, 'Слишком много попыток. Повторите через 15 минут.' if blocked else 'Код не принят. Проверьте его и повторите попытку.'
    cursor = await conn.execute(
        'SELECT * FROM enrollment_codes WHERE code_hash=? AND revoked_at IS NULL',
        (token_hash(normalized),),
    )
    enrollment = await cursor.fetchone()
    if enrollment is None or enrollment['expires_at'] <= timestamp or enrollment['used_count'] >= enrollment['max_uses']:
        blocked = await record_failed_link_attempt(conn, user_id, timestamp)
        return None, 'Слишком много попыток. Повторите через 15 минут.' if blocked else 'Код не принят. Проверьте его и повторите попытку.'

    source_user = payload.get('user') or ((payload.get('message') or {}).get('sender') or {})
    display_name = source_user.get('name') if isinstance(source_user, dict) else None
    internal_id = f'max-{enrollment["role"]}-{user_id}'
    await conn.execute(
        'INSERT INTO users(id,name,role,house_id,token_hash) VALUES(?,?,?,?,?)',
        (internal_id, display_name or f"Пользователь MAX {user_id}", enrollment['role'], enrollment['house_id'],
         token_hash(secrets.token_urlsafe(32))),
    )
    await conn.execute('INSERT INTO max_links(max_user_id,user_id,linked_at) VALUES(?,?,?)',
                       (user_id, internal_id, timestamp))
    await conn.execute('UPDATE enrollment_codes SET used_count=used_count+1 WHERE id=?', (enrollment['id'],))
    await conn.execute('DELETE FROM max_link_attempts WHERE max_user_id=?', (user_id,))
    return await linked_user(conn, user_id), None


async def notify_ticket_resident(conn, ticket, text: str, timestamp: str, confirm=False):
    cursor = await conn.execute('SELECT max_user_id FROM max_links WHERE user_id=?', (ticket['resident_id'],))
    link = await cursor.fetchone()
    if link is None:
        return
    attachments = keyboard([['Мои обращения']])
    if confirm:
        attachments = keyboard([['Да, всё решено'], ['Проблема осталась']])
        await save_dialog(conn, link['max_user_id'], 'confirm', {'ticket_id': ticket['id']}, timestamp)
    await queue_message(conn, link['max_user_id'], text, attachments, timestamp)


async def operator_queue(conn, user, user_id: int, timestamp: str):
    cursor = await conn.execute(
        "SELECT * FROM tickets WHERE house_id=? AND status!='confirmed' "
        "ORDER BY CASE WHEN due_at IS NOT NULL AND due_at<? THEN 0 ELSE 1 END, "
        "CASE priority WHEN 'emergency' THEN 0 WHEN 'urgent' THEN 1 ELSE 2 END, created_at LIMIT 5",
        (user['house_id'], timestamp),
    )
    tickets = await cursor.fetchall()
    if not tickets:
        await save_dialog(conn, user_id, 'operator_select', {}, timestamp)
        return 'В очереди нет активных обращений.', keyboard([['Обновить очередь'], ['Меню']])
    lines = ['Очередь дома:']
    buttons = []
    for ticket in tickets:
        prefix = ticket['id'][:8]
        lines.append(
            f"• #{prefix} · {PRIORITY_LABELS[ticket['priority']]} · {STATUS_LABELS[ticket['status']]}"
            f"{' · ПРОСРОЧЕНО' if is_overdue(ticket['due_at'], ticket['status']) else ''}\n"
            f"  {ticket['location']} — {ticket['description'][:70]}"
        )
        buttons.append([f'Заявка #{prefix}'])
    buttons.extend([['Обновить очередь'], ['Меню']])
    await save_dialog(conn, user_id, 'operator_select', {}, timestamp)
    return '\n'.join(lines), keyboard(buttons)


STOP_WORDS = {
    'это', 'как', 'так', 'что', 'при', 'для', 'уже', 'еще', 'ещё', 'или', 'был', 'была',
    'более', 'после', 'очень', 'когда', 'весь', 'надо', 'есть', 'дом', 'подъезд', 'этаж',
}


def issue_tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r'[a-zа-яё0-9]+', value.lower())
        if len(token) >= 3 and token not in STOP_WORDS
    }


def similarity(left, right) -> float:
    if left['category'] != right['category']:
        return 0.0
    left_location, right_location = issue_tokens(left['location']), issue_tokens(right['location'])
    location_match = bool(left_location & right_location)
    left_description, right_description = issue_tokens(left['description']), issue_tokens(right['description'])
    union = left_description | right_description
    description_match = len(left_description & right_description) / len(union) if union else 0.0
    if location_match and description_match >= 0.12:
        return 1.0
    if location_match:
        return 0.7
    return description_match if description_match >= 0.35 else 0.0


def similar_groups(tickets: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for ticket in tickets:
        matching = [group for group in groups if any(similarity(ticket, item) >= 0.7 for item in group)]
        if not matching:
            groups.append([ticket])
        else:
            primary = matching[0]
            primary.append(ticket)
            for extra in matching[1:]:
                primary.extend(extra)
                groups.remove(extra)
    return [group for group in groups if len(group) >= 2]


async def operator_insights(conn, user, user_id: int, timestamp: str):
    cursor = await conn.execute(
        "SELECT t.* FROM tickets t LEFT JOIN incident_tickets it ON it.ticket_id=t.id "
        "WHERE t.house_id=? AND t.status!='confirmed' AND it.ticket_id IS NULL "
        'ORDER BY t.created_at DESC LIMIT 100',
        (user['house_id'],),
    )
    tickets = [dict(row) for row in await cursor.fetchall()]
    groups = sorted(similar_groups(tickets), key=len, reverse=True)
    if not groups:
        await save_dialog(conn, user_id, 'idle', {}, timestamp)
        return 'Новых повторяющихся проблем пока не обнаружено.', keyboard([['Очередь дома'], ['Меню']])
    groups = groups[:5]
    lines = ['Возможные общедомовые проблемы:']
    buttons = []
    for number, group in enumerate(groups, 1):
        sample = group[0]
        ids = ', '.join(f"#{item['id'][:8]}" for item in group)
        lines.append(
            f"\n{number}. {len(group)} обращения · категория «{CATEGORY_LABELS[sample['category']]}» · {sample['location']}\n"
            f"  Связанные заявки: {ids}"
        )
        buttons.append([f'Открыть сигнал {number}'])
    lines.append('\nЭто рекомендация: перед объявлением общего инцидента проверьте заявки вручную.')
    buttons.extend([['Обновить сигналы'], ['Очередь дома']])
    await save_dialog(conn, user_id, 'operator_insights', {
        'groups': [[ticket['id'] for ticket in group] for group in groups],
    }, timestamp)
    return '\n'.join(lines), keyboard(buttons)


async def incident_signal_card(conn, user, ticket_ids: list[str]):
    if not ticket_ids or any(not isinstance(ticket_id, str) for ticket_id in ticket_ids):
        return None
    placeholders = ','.join('?' for _ in ticket_ids)
    cursor = await conn.execute(
        f"SELECT * FROM tickets WHERE house_id=? AND status!='confirmed' AND id IN ({placeholders}) "
        'ORDER BY created_at',
        (user['house_id'], *ticket_ids),
    )
    tickets = [dict(row) for row in await cursor.fetchall()]
    if len(tickets) < 2:
        return None
    sample = tickets[0]
    text = (
        'Проверка общего инцидента\n\n'
        f"Похоже на одну проблему: {CATEGORY_LABELS[sample['category']].lower()}\n"
        f"Место: {sample['location']}\n"
        f"Затронуто обращений: {len(tickets)}\n\n"
        'Подтвердите только если это действительно одна причина. Затем выберите срочность — '
        'она будет применена к этим обращениям, а жители получат уведомление.'
    )
    return tickets, text


async def announce_incident(conn, user, ticket_ids: list[str], priority: str, timestamp: str):
    signal = await incident_signal_card(conn, user, ticket_ids)
    if signal is None:
        return None
    tickets, _ = signal
    incident_id = str(uuid4())
    sample = tickets[0]
    await conn.execute(
        'INSERT INTO incidents(id,house_id,category,location,priority,created_by,created_at) VALUES(?,?,?,?,?,?,?)',
        (incident_id, user['house_id'], sample['category'], sample['location'], priority, user['id'], timestamp),
    )
    for ticket in tickets:
        await conn.execute('INSERT INTO incident_tickets(incident_id,ticket_id) VALUES(?,?)', (incident_id, ticket['id']))
        await conn.execute(
            'UPDATE tickets SET priority=?,due_at=?,updated_at=?,version=version+1 WHERE id=?',
            (priority, calculate_due_at(priority, ticket['created_at']), timestamp, ticket['id']),
        )
        note = (
            'УК подтвердила общий инцидент по нескольким обращениям. '
            f"Срочность: {PRIORITY_LABELS[priority]}."
        )
        await conn.execute(
            'INSERT INTO events(ticket_id,actor_id,kind,status,text,created_at) VALUES(?,?,?,?,?,?)',
            (ticket['id'], user['id'], 'incident_announced', ticket['status'], note, timestamp),
        )
        await notify_ticket_resident(
            conn, ticket,
            f"Обновление по обращению №{ticket['id'][:8]}\n\n"
            'УК подтвердила общую проблему по нескольким обращениям дома.\n'
            f"Место: {ticket['location']}\n"
            f"Срочность: {PRIORITY_LABELS[priority]}.\n\n"
            'Мы сообщим здесь о следующих изменениях.',
            timestamp,
        )
    return incident_id, len(tickets)


def format_minutes(value: float | None) -> str:
    if value is None:
        return 'ещё нет данных'
    if value < 60:
        return f'{value:g} мин.'
    hours, minutes = divmod(round(value), 60)
    return f'{hours} ч. {minutes} мин.'


async def operator_metrics(conn, user, user_id: int, timestamp: str):
    metrics = await house_metrics(conn, user['house_id'])
    active = [ticket for ticket in metrics['tickets'] if ticket['status'] != 'confirmed']
    groups = similar_groups(active)
    categories = ', '.join(
        f"{CATEGORY_LABELS[category]} — {count}"
        for category, count in metrics['top_categories']
    ) or 'нет активных'
    locations = ', '.join(
        f'{location} — {count}' for location, count in metrics['top_locations']
    ) or 'нет активных'
    closed = (
        f"{metrics['closed_on_time_percent']:g}% ({metrics['closed_with_sla_data']} заявок)"
        if metrics['closed_on_time_percent'] is not None else 'ещё нет данных'
    )
    text = (
        'Показатели дома\n\n'
        f"Активные: {metrics['active']}\n"
        f"Аварийные: {metrics['emergency']}\n"
        f"Просроченные: {metrics['overdue']}\n"
        f"Средняя первая реакция: {format_minutes(metrics['average_first_response_minutes'])}\n"
        f"Закрыто в срок: {closed}\n"
        f"Общие сигналы: {len(groups)}\n\n"
        f"Частые категории: {categories}\n"
        f"Проблемные места: {locations}"
    )
    await save_dialog(conn, user_id, 'idle', {}, timestamp)
    return text, keyboard([['Очередь дома'], ['Общие проблемы дома'], ['Показатели дома']])


def operator_ticket_card(ticket) -> tuple[str, list[dict[str, Any]]]:
    lines = [
        f"Заявка #{ticket['id'][:8]}",
        f"Категория: {ticket['category']}",
        f"Место: {ticket['location']}",
        f"Статус: {STATUS_LABELS[ticket['status']]}",
        f"Приоритет: {PRIORITY_LABELS[ticket['priority']]}",
        f"Срок реакции: {ticket['due_at'] or 'не установлен'}"
        f"{' · ПРОСРОЧЕНО' if is_overdue(ticket['due_at'], ticket['status']) else ''}",
        '', ticket['description'],
    ]
    buttons: list[list[str]] = []
    action = OPERATOR_ACTIONS.get(ticket['status'])
    if action:
        buttons.append([action[0]])
    buttons.extend([['Изменить срочность'], ['Очередь дома']])
    return '\n'.join(lines), keyboard(buttons)


async def resident_ticket_card(conn, ticket, user):
    cursor = await conn.execute(
        'SELECT e.kind,e.status,e.text,e.created_at,u.name,u.role FROM events e '
        'JOIN users u ON u.id=e.actor_id WHERE e.ticket_id=? ORDER BY e.id', (ticket['id'],),
    )
    events = await cursor.fetchall()
    cursor = await conn.execute(
        'SELECT i.category,i.location,i.priority FROM incidents i JOIN incident_tickets it ON it.incident_id=i.id '
        'WHERE it.ticket_id=?', (ticket['id'],),
    )
    incident = await cursor.fetchone()
    lines = [
        f"Обращение №{ticket['id'][:8]}",
        f"Категория: {CATEGORY_LABELS[ticket['category']]}",
        f"Место: {ticket['location']}",
        f"Статус: {STATUS_LABELS[ticket['status']]}",
        f"Срочность: {PRIORITY_LABELS[ticket['priority']]}",
        '', f"Описание: {ticket['description']}",
    ]
    if incident:
        lines.extend(['', 'УК подтвердила общий инцидент по нескольким обращениям дома.'])
    lines.extend(['', 'История:'])
    for event in events:
        author = 'Вы' if event['role'] == 'resident' and event['name'] == user['name'] else 'УК'
        timestamp_label = event['created_at'][:16].replace('T', ' ')
        lines.append(f"• {timestamp_label} · {author}: {event['text']}")
    return '\n'.join(lines), keyboard([['Мои обращения'], ['Сообщить о проблеме'], ['Меню']])


async def save_dialog(conn, user_id: int, state: str, draft: dict[str, Any], timestamp: str):
    await conn.execute(
        'INSERT INTO max_dialogs(max_user_id,state,draft_json,updated_at) VALUES(?,?,?,?) '
        'ON CONFLICT(max_user_id) DO UPDATE SET state=excluded.state,'
        'draft_json=excluded.draft_json,updated_at=excluded.updated_at',
        (user_id, state, json.dumps(draft, ensure_ascii=False), timestamp),
    )


async def process_message(conn, payload: dict[str, Any], user_id: int, timestamp: str):
    text = message_text(payload)
    user = await linked_user(conn, user_id)

    if user is None and text.lower().startswith('/код '):
        user, error = await enroll_with_code(conn, user_id, payload, text[5:], timestamp)
        if error:
            return error, []
        await save_dialog(conn, user_id, 'idle', {}, timestamp)
        reply, attachments = menu(user['role'])
        return 'Профиль успешно привязан к дому.\n\n' + reply, attachments
    if user is None:
        suffix = ' Для привязки введите /код <одноразовый-код>.'
        return ('Профиль не привязан к дому. Обратитесь к организатору демонстрации.' + suffix, [])

    cursor = await conn.execute('SELECT * FROM max_dialogs WHERE max_user_id=?', (user_id,))
    dialog = await cursor.fetchone()
    state = dialog['state'] if dialog else 'idle'
    draft = json.loads(dialog['draft_json']) if dialog else {}

    if payload['update_type'] == 'bot_started' or text.lower() in {'/start', 'меню'}:
        await save_dialog(conn, user_id, 'idle', {}, timestamp)
        return menu(user['role'])
    if text == 'Отмена':
        await save_dialog(conn, user_id, 'idle', {}, timestamp)
        reply, attachments = menu(user['role'])
        return 'Создание обращения отменено.\n\n' + reply, attachments
    if user['role'] == 'operator':
        if text == 'Показатели дома':
            return await operator_metrics(conn, user, user_id, timestamp)
        if text == 'Общие проблемы дома':
            return await operator_insights(conn, user, user_id, timestamp)
        if text in {'Очередь дома', 'Обновить очередь'}:
            return await operator_queue(conn, user, user_id, timestamp)
        if text.startswith('Заявка #'):
            prefix = text.removeprefix('Заявка #').strip()
            if len(prefix) != 8 or not prefix.isalnum():
                return 'Не удалось открыть заявку. Вернитесь в очередь.', keyboard([['Очередь дома']])
            cursor = await conn.execute(
                'SELECT * FROM tickets WHERE house_id=? AND id LIKE ? LIMIT 2',
                (user['house_id'], prefix + '%'),
            )
            tickets = await cursor.fetchall()
            if len(tickets) != 1:
                return 'Заявка не найдена или идентификатор неоднозначен.', keyboard([['Очередь дома']])
            ticket = tickets[0]
            await save_dialog(conn, user_id, 'operator_ticket', {
                'ticket_id': ticket['id'], 'status': ticket['status'],
            }, timestamp)
            return operator_ticket_card(ticket)
        if state == 'operator_ticket':
            action = OPERATOR_ACTIONS.get(draft.get('status'))
            if text == 'Изменить срочность':
                await save_dialog(conn, user_id, 'operator_priority', draft, timestamp)
                return 'Выберите приоритет. «Авария» — риск безопасности, воды или электроснабжения.', keyboard([
                    ['Обычная срочность'], ['Срочно'], ['Авария'], ['Отмена'],
                ])
            if action and text == action[0]:
                await save_dialog(conn, user_id, 'operator_comment', {
                    **draft, 'target_status': action[1], 'action_label': action[0],
                }, timestamp)
                return f"{action[0]}. Напишите короткий комментарий для жителя.", keyboard([['Отмена']])
        if state == 'operator_priority':
            priorities = {'Обычная срочность': 'normal', 'Срочно': 'urgent', 'Авария': 'emergency'}
            priority = priorities.get(text)
            if priority is None:
                return 'Выберите один из уровней срочности.', keyboard([
                    ['Обычная срочность'], ['Срочно'], ['Авария'], ['Отмена'],
                ])
            cursor = await conn.execute('SELECT * FROM tickets WHERE id=? AND house_id=?',
                                        (draft.get('ticket_id'), user['house_id']))
            ticket = await cursor.fetchone()
            if ticket is None:
                return 'Заявка не найдена.', keyboard([['Очередь дома']])
            await conn.execute('UPDATE tickets SET priority=?,due_at=?,updated_at=?,version=version+1 WHERE id=?',
                               (priority, calculate_due_at(priority, ticket['created_at']), timestamp, ticket['id']))
            await conn.execute(
                'INSERT INTO events(ticket_id,actor_id,kind,status,text,created_at) VALUES(?,?,?,?,?,?)',
                (ticket['id'], user['id'], 'priority_changed', ticket['status'],
                 f"Диспетчер установил приоритет: {PRIORITY_LABELS[priority]}.", timestamp),
            )
            cursor = await conn.execute('SELECT * FROM tickets WHERE id=?', (ticket['id'],))
            updated = await cursor.fetchone()
            await save_dialog(conn, user_id, 'operator_ticket', {'ticket_id': ticket['id'], 'status': updated['status']}, timestamp)
            return operator_ticket_card(updated)
        if state == 'operator_comment':
            if not 3 <= len(text) <= 4000:
                return 'Комментарий должен содержать от 3 до 4000 символов.', keyboard([['Отмена']])
            cursor = await conn.execute('SELECT * FROM tickets WHERE id=? AND house_id=?',
                                        (draft.get('ticket_id'), user['house_id']))
            ticket = await cursor.fetchone()
            expected_source = next(
                (source for source, action in OPERATOR_ACTIONS.items() if action[1] == draft.get('target_status')
                 and action[0] == draft.get('action_label')),
                None,
            )
            if ticket is None or ticket['status'] != expected_source:
                return 'Статус заявки уже изменился. Откройте её заново.', keyboard([['Очередь дома']])
            target = draft['target_status']
            await conn.execute('UPDATE tickets SET status=?,updated_at=?,version=version+1 WHERE id=?',
                               (target, timestamp, ticket['id']))
            if target == 'confirmed':
                await conn.execute('UPDATE tickets SET closed_at=? WHERE id=?', (timestamp, ticket['id']))
            await conn.execute('UPDATE tickets SET first_response_at=COALESCE(first_response_at,?) WHERE id=?',
                               (timestamp, ticket['id']))
            await conn.execute(
                'INSERT INTO events(ticket_id,actor_id,kind,status,text,created_at) VALUES(?,?,?,?,?,?)',
                (ticket['id'], user['id'], 'status_changed', target, text, timestamp),
            )
            label = STATUS_LABELS[target]
            await notify_ticket_resident(
                conn, ticket,
                f"Обращение №{ticket['id'][:8]}: {label}.\nКомментарий УК: {text}",
                timestamp, confirm=target == 'resolved',
            )
            await save_dialog(conn, user_id, 'idle', {}, timestamp)
            return f"Заявка #{ticket['id'][:8]}: статус «{label}» сохранён.", keyboard([['Очередь дома']])
        return 'Выберите действие из меню диспетчера.', keyboard([['Очередь дома']])
    if text == 'Мои обращения':
        cursor = await conn.execute(
            'SELECT * FROM tickets WHERE resident_id=? ORDER BY created_at DESC LIMIT 5',
            (user['id'],),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 'У вас пока нет обращений.', keyboard([['Сообщить о проблеме']])
        lines = ['Последние обращения:']
        for ticket in rows:
            lines.append(
                f"• №{ticket['id'][:8]} — {ticket['location']} — {STATUS_LABELS[ticket['status']]}"
            )
        return '\n'.join(lines), keyboard([['Сообщить о проблеме'], ['Меню']])
    if text == 'Сообщить о проблеме':
        await save_dialog(conn, user_id, 'category', {}, timestamp)
        labels = list(CATEGORIES)
        return 'Выберите категорию проблемы.', keyboard([
            labels[0:2], labels[2:4], labels[4:6], labels[6:] + ['Отмена'],
        ])
    if state == 'confirm' and text in {'Да, всё решено', 'Проблема осталась'}:
        cursor = await conn.execute(
            'SELECT * FROM tickets WHERE id=? AND resident_id=?',
            (draft.get('ticket_id'), user['id']),
        )
        ticket = await cursor.fetchone()
        if ticket is None or ticket['status'] != 'resolved':
            await save_dialog(conn, user_id, 'idle', {}, timestamp)
            return 'Статус обращения уже изменился.', keyboard([['Мои обращения'], ['Меню']])
        status = 'confirmed' if text == 'Да, всё решено' else 'reopened'
        comment = 'Житель подтвердил устранение проблемы.' if status == 'confirmed' else 'Житель сообщил, что проблема осталась.'
        await conn.execute(
            'UPDATE tickets SET status=?,updated_at=?,version=version+1 WHERE id=?',
            (status, timestamp, ticket['id']),
        )
        await conn.execute(
            'INSERT INTO events(ticket_id,actor_id,kind,status,text,created_at) VALUES(?,?,?,?,?,?)',
            (ticket['id'], user['id'], 'status_changed', status, comment, timestamp),
        )
        await save_dialog(conn, user_id, 'idle', {}, timestamp)
        reply = 'Спасибо, обращение завершено.' if status == 'confirmed' else 'Обращение возвращено в работу УК.'
        return reply, keyboard([['Мои обращения'], ['Сообщить о проблеме']])
    if state == 'category':
        category = CATEGORIES.get(text)
        if not category:
            return 'Выберите категорию кнопкой ниже.', keyboard([[name] for name in CATEGORIES] + [['Отмена']])
        await save_dialog(conn, user_id, 'location', {'category': category}, timestamp)
        return 'Где возникла проблема? Например: «подъезд 2, этаж 5» или «квартира 14».', keyboard([['Отмена']])
    if state == 'location':
        if not 2 <= len(text) <= 160:
            return 'Укажите место проблемы — от 2 до 160 символов.', keyboard([['Отмена']])
        draft['location'] = text
        await save_dialog(conn, user_id, 'description', draft, timestamp)
        return 'Опишите, что произошло и когда вы это заметили.', keyboard([['Отмена']])
    if state == 'description':
        if not 10 <= len(text) <= 4000:
            return 'Описание должно содержать от 10 до 4000 символов.', keyboard([['Отмена']])
        ticket_id = str(uuid4())
        await conn.execute(
            'INSERT INTO tickets(id,house_id,resident_id,category,location,description,status,created_at,updated_at) '
            "VALUES(?,?,?,?,?,?,'new',?,?)",
            (ticket_id, user['house_id'], user['id'], draft['category'], draft['location'], text,
             timestamp, timestamp),
        )
        await conn.execute(
            'INSERT INTO events(ticket_id,actor_id,kind,status,text,created_at) VALUES(?,?,?,?,?,?)',
            (ticket_id, user['id'], 'created', 'new', text, timestamp),
        )
        await conn.execute('UPDATE tickets SET due_at=? WHERE id=?',
                           (calculate_due_at('normal', timestamp), ticket_id))
        await save_dialog(conn, user_id, 'idle', {}, timestamp)
        return (
            f'Обращение №{ticket_id[:8]} зарегистрировано и передано в очередь дома. '
            'Здесь появятся уведомления об изменении статуса.',
            keyboard([['Мои обращения'], ['Сообщить о проблеме']]),
        )
    return menu()


async def store_update(db: AsyncDatabase, payload: dict[str, Any]) -> bool:
    """Persist an update once and queue the response in the same transaction."""
    async with db.connect(write=True) as conn:
        return await store_update_in_transaction(conn, payload)


async def store_update_in_transaction(conn, payload: dict[str, Any]) -> bool:
    """Shared dialog processing for webhook and polling batch transactions."""
    update_fingerprint = fingerprint(payload)
    timestamp = utc_now()
    user_id = max_user_id(payload)
    cursor = await conn.execute(
        'INSERT OR IGNORE INTO max_updates(fingerprint,update_type,payload_json,received_at) '
        'VALUES(?,?,?,?)',
        (update_fingerprint, payload['update_type'], json.dumps(stored_payload(payload), ensure_ascii=False), timestamp),
    )
    if cursor.rowcount == 0:
        return False
    if user_id is not None:
        reply, attachments = await process_message(conn, payload, user_id, timestamp)
        await queue_message(conn, user_id, reply, attachments, timestamp)
    return True
