from datetime import datetime, timedelta, timezone

from .db import AsyncDatabase

SLA_MINUTES = {'normal': 24 * 60, 'urgent': 2 * 60, 'emergency': 15}


def calculate_due_at(priority: str, created_at: str) -> str:
    minutes = SLA_MINUTES[priority]
    return (datetime.fromisoformat(created_at) + timedelta(minutes=minutes)).isoformat()


def is_overdue(due_at: str | None, status: str) -> bool:
    if not due_at or status in {'confirmed'}:
        return False
    return datetime.fromisoformat(due_at) < datetime.now(timezone.utc)


async def scan_overdue(db: AsyncDatabase) -> int:
    """Create one outbox alert per overdue ticket and notify linked operators."""
    from .max_webhook import keyboard, queue_message
    now = datetime.now(timezone.utc).isoformat()
    alerted = 0
    async with db.connect(write=True) as conn:
        cursor = await conn.execute(
            "SELECT t.* FROM tickets t WHERE t.due_at IS NOT NULL AND t.due_at<? "
            "AND t.status!='confirmed' AND NOT EXISTS "
            '(SELECT 1 FROM sla_alerts a WHERE a.ticket_id=t.id)',
            (now,),
        )
        tickets = await cursor.fetchall()
        for ticket in tickets:
            cursor = await conn.execute(
                'INSERT OR IGNORE INTO sla_alerts(ticket_id,created_at) VALUES(?,?)',
                (ticket['id'], now),
            )
            if cursor.rowcount == 0:
                continue
            cursor = await conn.execute(
                'SELECT l.max_user_id FROM max_links l JOIN users u ON u.id=l.user_id '
                "WHERE u.house_id=? AND u.role='operator'",
                (ticket['house_id'],),
            )
            operators = await cursor.fetchall()
            message = (
                f"Просрочена заявка #{ticket['id'][:8]} ({ticket['priority']}).\n"
                f"Место: {ticket['location']}\n"
                'Откройте очередь дома и возьмите её в работу.'
            )
            for operator in operators:
                await queue_message(conn, operator['max_user_id'], message,
                                    keyboard([[f"Заявка #{ticket['id'][:8]}"], ['Очередь дома']]), now)
            alerted += 1
    return alerted
