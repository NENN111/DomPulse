from collections import Counter
from datetime import datetime

from .sla import is_overdue


def minutes_between(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60


async def house_metrics(conn, house_id: str) -> dict:
    cursor = await conn.execute('SELECT * FROM tickets WHERE house_id=?', (house_id,))
    tickets = [dict(row) for row in await cursor.fetchall()]
    active = [ticket for ticket in tickets if ticket['status'] != 'confirmed']
    overdue = [ticket for ticket in tickets if is_overdue(ticket['due_at'], ticket['first_response_at'])]
    first_responses = [
        minutes_between(ticket['created_at'], ticket['first_response_at'])
        for ticket in tickets if ticket['first_response_at']
    ]
    responded = [ticket for ticket in tickets if ticket['first_response_at'] and ticket['due_at']]
    on_time = [ticket for ticket in responded if ticket['first_response_at'] <= ticket['due_at']]
    categories = Counter(ticket['category'] for ticket in active)
    locations = Counter(ticket['location'] for ticket in active)
    return {
        'active': len(active),
        'emergency': sum(ticket['priority'] == 'emergency' for ticket in active),
        'overdue': len(overdue),
        'average_first_response_minutes': round(sum(first_responses) / len(first_responses), 1)
        if first_responses else None,
        'responded_with_sla_data': len(responded),
        'first_response_on_time_percent': round(100 * len(on_time) / len(responded), 1)
        if responded else None,
        'top_categories': categories.most_common(3),
        'top_locations': locations.most_common(3),
        'tickets': tickets,
    }
