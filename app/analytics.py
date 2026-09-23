from collections import Counter
from datetime import datetime

from .sla import is_overdue


def minutes_between(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60


async def house_metrics(conn, house_id: str) -> dict:
    cursor = await conn.execute('SELECT * FROM tickets WHERE house_id=?', (house_id,))
    tickets = [dict(row) for row in await cursor.fetchall()]
    active = [ticket for ticket in tickets if ticket['status'] != 'confirmed']
    overdue = [ticket for ticket in active if is_overdue(ticket['due_at'], ticket['status'])]
    first_responses = [
        minutes_between(ticket['created_at'], ticket['first_response_at'])
        for ticket in tickets if ticket['first_response_at']
    ]
    closed = [ticket for ticket in tickets if ticket['closed_at'] and ticket['due_at']]
    on_time = [ticket for ticket in closed if ticket['closed_at'] <= ticket['due_at']]
    categories = Counter(ticket['category'] for ticket in active)
    locations = Counter(ticket['location'] for ticket in active)
    return {
        'active': len(active),
        'emergency': sum(ticket['priority'] == 'emergency' for ticket in active),
        'overdue': len(overdue),
        'average_first_response_minutes': round(sum(first_responses) / len(first_responses), 1)
        if first_responses else None,
        'closed_with_sla_data': len(closed),
        'closed_on_time_percent': round(100 * len(on_time) / len(closed), 1) if closed else None,
        'top_categories': categories.most_common(3),
        'top_locations': locations.most_common(3),
        'tickets': tickets,
    }
