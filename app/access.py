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
    cur = await conn.execute('SELECT h.id FROM houses h JOIN house_districts hd ON hd.house_id=h.id JOIN operator_districts od ON od.district=hd.district WHERE od.user_id=?', (user['id'],))
    return sorted({user['house_id'], *(row['id'] for row in await cur.fetchall())})
async def can_access_house(conn, user, house_id): return house_id in await allowed_house_ids(conn, user)
async def operator_districts(conn, user):
    cur = await conn.execute('SELECT district FROM operator_districts WHERE user_id=? ORDER BY district', (user['id'],))
    return [row['district'] for row in await cur.fetchall()]
async def district_house_ids(conn, user, district=None):
    ids = await allowed_house_ids(conn, user)
    if not district: return ids
    cur = await conn.execute('SELECT house_id FROM house_districts WHERE district=?', (district,))
    selected={row['house_id'] for row in await cur.fetchall()}; return [x for x in ids if x in selected]
async def house_district(conn, house_id):
    cur = await conn.execute('SELECT district FROM house_districts WHERE house_id=?', (house_id,)); row=await cur.fetchone()
    return row['district'] if row else 'Округ не указан'
