"""Единая проверка доступа оператора к домам и округам."""
MOSCOW_DISTRICTS = ("ЦАО", "САО", "СВАО", "ВАО", "ЮВАО", "ЮАО", "ЮЗАО", "ЗАО", "СЗАО", "Зеленоградский", "Новомосковский", "Троицкий")
async def allowed_house_ids(conn, user):
    if user['role'] != 'operator':
        cur = await conn.execute(
            'SELECT house_id,revoked_at FROM user_houses WHERE user_id=? ORDER BY verified_at,house_id',
            (user['id'],),
        )
        memberships = await cur.fetchall()
        if memberships:
            return [row['house_id'] for row in memberships if row['revoked_at'] is None]
        return [user['house_id']]
    cur = await conn.execute(
        'SELECT DISTINCT h.id FROM operator_districts od '
        'LEFT JOIN house_districts hd ON hd.district=od.district '
        'LEFT JOIN houses h ON h.id=hd.house_id '
        'WHERE od.user_id=? ORDER BY h.id',
        (user['id'],),
    )
    rows = await cur.fetchall()
    if rows:
        return [row['id'] for row in rows if row['id'] is not None]
    return [user['house_id']]


async def can_access_house(conn, user, house_id): return house_id in await allowed_house_ids(conn, user)
