"""Повторяемое наполнение демонстрационного контура без изменения привязок MAX."""

import argparse
import secrets
from datetime import datetime, timedelta, timezone

from .db import Database, token_hash


# Адреса служат правдоподобными примерами, а не подтверждённым реестром домов УК.
DISTRICT_ADDRESSES = {
    'ЦАО': ['Москва, улица Большая Грузинская, дом 42', 'Москва, улица Покровка, дом 21'],
    'САО': ['Москва, улица Алабяна, дом 10', 'Москва, улица Дубнинская, дом 30'],
    'СВАО': ['Москва, улица Ботаническая, дом 19', 'Москва, улица Лётчика Бабушкина, дом 23'],
    'ВАО': ['Москва, улица Первомайская, дом 80', 'Москва, улица Амурская, дом 15'],
    'ЮВАО': ['Москва, улица Люблинская, дом 53', 'Москва, улица Юных Ленинцев, дом 47'],
    'ЮАО': ['Москва, улица Кировоградская, дом 9', 'Москва, улица Нагатинская, дом 25'],
    'ЮЗАО': ['Москва, улица Профсоюзная, дом 43', 'Москва, улица Академика Пилюгина, дом 12'],
    'ЗАО': ['Москва, улица Мосфильмовская, дом 17', 'Москва, улица Кастанаевская, дом 39'],
    'СЗАО': ['Москва, улица Народного Ополчения, дом 35', 'Москва, улица Свободы, дом 62'],
    'Зеленоградский': ['Москва, Зеленоград, корпус 1106', 'Москва, Зеленоград, корпус 1459'],
    'Новомосковский': ['Москва, Коммунарка, улица Александры Монаховой, дом 92', 'Москва, Московский, улица Солнечная, дом 5'],
    'Троицкий': ['Москва, Троицк, Октябрьский проспект, дом 15', 'Москва, Троицк, улица Центральная, дом 28'],
}

PROBLEMS = [
    ('water', 'Подвал', 'В подвале обнаружена течь на стояке холодной воды.', 'emergency', 'new'),
    ('heating', 'Подъезд 2', 'Радиаторы на верхних этажах нагреваются неравномерно.', 'urgent', 'in_progress'),
    ('elevator', 'Лифт, подъезд 1', 'Лифт останавливается с задержкой при вызове на этаж.', 'normal', 'accepted'),
    ('cleaning', 'Лестничная клетка', 'После выходных требуется дополнительная уборка площадки.', 'normal', 'confirmed'),
    ('electricity', 'Подъезд 1', 'На лестничной площадке не горит светильник.', 'normal', 'resolved'),
    ('yard', 'Двор', 'У контейнерной площадки скопился крупногабаритный мусор.', 'normal', 'new'),
    ('other', 'Входная группа', 'Доводчик входной двери закрывает дверь с хлопком.', 'normal', 'in_progress'),
]

SLA_MINUTES = {'normal': 1440, 'urgent': 120, 'emergency': 15}
ANNOUNCEMENTS = [
    ('Проверка инженерных систем', 'На этой неделе специалисты проверят оборудование в местах общего пользования. Доступ в квартиры не требуется.'),
    ('Осмотр входных групп', 'В течение дня мастер проверит двери и доводчики подъездов. При обнаружении неисправности сообщите диспетчеру.'),
    ('Плановые работы во дворе', 'В первой половине дня запланированы уборка территории и осмотр контейнерной площадки.'),
    ('Проверка освещения', 'Специалисты осмотрят светильники в подъездах и заменят неисправные лампы.'),
]


def _remove_previous_demo(conn):
    # Удаляются только записи с явными демо-ID и два старых учебных дома.
    demo_tickets = "id LIKE 'demo-%' OR id LIKE 'synthetic-%'"
    conn.execute(f'DELETE FROM sla_alerts WHERE ticket_id IN (SELECT id FROM tickets WHERE {demo_tickets})')
    conn.execute(f'DELETE FROM ticket_attachments WHERE ticket_id IN (SELECT id FROM tickets WHERE {demo_tickets})')
    affected_incidents = [row['incident_id'] for row in conn.execute(
        f'SELECT DISTINCT incident_id FROM incident_tickets WHERE ticket_id IN (SELECT id FROM tickets WHERE {demo_tickets})'
    )]
    conn.execute(f'DELETE FROM incident_tickets WHERE ticket_id IN (SELECT id FROM tickets WHERE {demo_tickets})')
    conn.execute(f'DELETE FROM events WHERE ticket_id IN (SELECT id FROM tickets WHERE {demo_tickets})')
    for incident_id in affected_incidents:
        conn.execute('DELETE FROM incidents WHERE id=? AND NOT EXISTS (SELECT 1 FROM incident_tickets WHERE incident_id=?)',
                     (incident_id, incident_id))
    conn.execute("DELETE FROM incidents WHERE id LIKE 'synthetic-%'")
    conn.execute(f'DELETE FROM tickets WHERE {demo_tickets}')
    conn.execute("DELETE FROM announcements WHERE id LIKE 'demo-%' OR id LIKE 'synthetic-%' OR house_id IN ('house-1','house-2')")
    conn.execute("DELETE FROM user_houses WHERE user_id LIKE 'demo-%' OR user_id LIKE 'synthetic-%' OR house_id IN ('house-1','house-2')")
    conn.execute("DELETE FROM operator_districts WHERE user_id IN ('operator-1','operator-2') OR user_id LIKE 'synthetic-%'")
    conn.execute("DELETE FROM users WHERE id IN ('resident-1','resident-2','operator-1','operator-2') OR id LIKE 'demo-%' OR id LIKE 'synthetic-%'")
    conn.execute("DELETE FROM enrollment_codes WHERE house_id IN ('house-1','house-2') OR house_id LIKE 'synthetic-%'")
    conn.execute("DELETE FROM house_management_companies WHERE house_id IN ('house-1','house-2') OR house_id LIKE 'synthetic-%'")
    conn.execute("DELETE FROM house_districts WHERE house_id IN ('house-1','house-2') OR house_id LIKE 'synthetic-%'")
    conn.execute("DELETE FROM houses WHERE id IN ('house-1','house-2') OR id LIKE 'synthetic-%'")


def populate(db_path):
    db = Database(db_path)
    db.initialize()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with db.connect(write=True) as conn:
        _remove_previous_demo(conn)
        if conn.execute("SELECT 1 FROM management_companies WHERE id='mc-1'").fetchone():
            conn.execute(
                "UPDATE management_companies SET name=?,info_text=? WHERE id='mc-1'",
                ('ООО «ДомПульс Управление»', 'Демонстрационные сведения об управляющей компании. Обращения принимаются через бот ДомПульс.'),
            )
        else:
            conn.execute('INSERT INTO management_companies VALUES(?,?,?)', (
                'mc-1', 'ООО «ДомПульс Управление»',
                'Демонстрационные сведения об управляющей компании. Обращения принимаются через бот ДомПульс.',
            ))
        existing = {row['district']: [] for row in conn.execute('SELECT DISTINCT district FROM house_districts')}
        for row in conn.execute('SELECT h.id,hd.district FROM houses h JOIN house_districts hd ON hd.house_id=h.id'):
            existing[row['district']].append(row['id'])
        for district_index, (district, addresses) in enumerate(DISTRICT_ADDRESSES.items(), 1):
            for house_index, address in enumerate(addresses, 1):
                house_id = f'synthetic-house-{district_index:02d}-{house_index}'
                conn.execute('INSERT INTO houses(id,address) VALUES(?,?)', (house_id, address))
                conn.execute('INSERT INTO house_districts(house_id,district) VALUES(?,?)', (house_id, district))
                conn.execute('INSERT INTO house_management_companies(house_id,company_id) VALUES(?,?)', (house_id, 'mc-1'))
                existing.setdefault(district, []).append(house_id)
        count = 0
        for district_index, district in enumerate(DISTRICT_ADDRESSES, 1):
            for house_index, house_id in enumerate(existing[district], 1):
                resident_id = f'synthetic-resident-{district_index:02d}-{house_index:02d}'
                conn.execute('INSERT INTO users(id,name,role,house_id,token_hash) VALUES(?,?,?,?,?)', (
                    resident_id, f'Житель дома {house_index}', 'resident', house_id, token_hash(secrets.token_urlsafe(32)),
                ))
                conn.execute('INSERT INTO user_houses(user_id,house_id,verification_method,verified_at) VALUES(?,?,?,?)', (
                    resident_id, house_id, 'legacy', now.isoformat(),
                ))
                actor = conn.execute(
                    'SELECT u.id FROM users u JOIN operator_districts od ON od.user_id=u.id WHERE od.district=? AND u.role=? ORDER BY u.id LIMIT 1',
                    (district, 'operator'),
                ).fetchone()
                # У каждого округа есть локальный синтетический оператор для истории действий.
                if actor is None:
                    operator_id = f'synthetic-operator-{district_index:02d}'
                    conn.execute('INSERT INTO users(id,name,role,house_id,token_hash) VALUES(?,?,?,?,?)', (
                        operator_id, f'Диспетчер округа {district}', 'operator', house_id, token_hash(secrets.token_urlsafe(32)),
                    ))
                    conn.execute('INSERT INTO operator_districts(user_id,district) VALUES(?,?)', (operator_id, district))
                else:
                    operator_id = actor['id']
                for ticket_index in range(4):
                    category, location, description, priority, status = PROBLEMS[(district_index + house_index + ticket_index) % len(PROBLEMS)]
                    if category == 'water':
                        priority = 'urgent'
                    if ticket_index == 0 and district_index % 3 == 0 and house_index == 1:
                        category, location, description, priority, status = PROBLEMS[0]
                    created = now - timedelta(hours=3 + house_index * 2 + ticket_index * 20)
                    due = created + timedelta(minutes=SLA_MINUTES[priority])
                    first = None if status == 'new' else created + timedelta(minutes=(25 if ticket_index % 3 else 65))
                    updated = first or created
                    closed = updated + timedelta(hours=2) if status == 'confirmed' else None
                    if closed:
                        updated = closed
                    ticket_id = f'synthetic-ticket-{district_index:02d}-{house_index:02d}-{ticket_index + 1}'
                    conn.execute(
                        'INSERT INTO tickets(id,house_id,resident_id,category,location,description,status,priority,version,created_at,updated_at,due_at,first_response_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (ticket_id, house_id, resident_id, category, location, description, status, priority, 2 if first else 1,
                         created.isoformat(), updated.isoformat(), due.isoformat(), first.isoformat() if first else None,
                         closed.isoformat() if closed else None),
                    )
                    conn.execute('INSERT INTO events(ticket_id,actor_id,kind,status,text,created_at) VALUES(?,?,?,?,?,?)',
                                 (ticket_id, resident_id, 'created', 'new', description, created.isoformat()))
                    if first:
                        conn.execute('INSERT INTO events(ticket_id,actor_id,kind,status,text,created_at) VALUES(?,?,?,?,?,?)',
                                     (ticket_id, operator_id, 'status_changed', status, 'Обращение принято в работу.', first.isoformat()))
                    # Уже просроченные демонстрационные заявки не должны порождать уведомления MAX.
                    if due < now and (first is None or first > due):
                        conn.execute('INSERT INTO sla_alerts(ticket_id,created_at) VALUES(?,?)', (ticket_id, now.isoformat()))
                    count += 1
                announcement_id = f'synthetic-announcement-{district_index:02d}-{house_index:02d}'
                title, body = ANNOUNCEMENTS[(district_index + house_index) % len(ANNOUNCEMENTS)]
                conn.execute('INSERT INTO announcements(id,house_id,title,body,created_by,created_at,sent_at) VALUES(?,?,?,?,?,?,?)', (
                    announcement_id, house_id, title, body,
                    operator_id, (now - timedelta(days=2)).isoformat(), None,
                ))
    return len(DISTRICT_ADDRESSES), sum(len(houses) for houses in existing.values()), count


def main():
    parser = argparse.ArgumentParser(description='Заменить учебные данные правдоподобными синтетическими записями')
    parser.add_argument('--db', required=True, help='Путь к SQLite-базе')
    args = parser.parse_args()
    districts, houses, tickets = populate(args.db)
    print(f'Созданы синтетические данные: {districts} округов, {houses} домов, {tickets} заявок.')


if __name__ == '__main__':
    main()
