import asyncio

from app.access import allowed_house_ids
from app.db import AsyncDatabase, Database, token_hash


def test_operator_access_keeps_legacy_and_empty_district_rules(tmp_path):
    path = str(tmp_path / 'access.db')
    db = Database(path)
    db.initialize()
    with db.connect(write=True) as conn:
        conn.executemany(
            'INSERT INTO houses(id,address) VALUES(?,?)',
            [('legacy', 'Legacy house'), ('a', 'First house'), ('b', 'Second house')],
        )
        conn.execute(
            'INSERT INTO users(id,name,role,house_id,token_hash) VALUES(?,?,?,?,?)',
            ('operator', 'Operator', 'operator', 'legacy', token_hash('test-token')),
        )

    async def houses():
        async with AsyncDatabase(path).connect() as conn:
            return await allowed_house_ids(
                conn, {'id': 'operator', 'role': 'operator', 'house_id': 'legacy'}
            )

    assert asyncio.run(houses()) == ['legacy']
    with db.connect(write=True) as conn:
        conn.execute(
            'INSERT INTO operator_districts(user_id,district) VALUES(?,?)',
            ('operator', 'district-1'),
        )
    assert asyncio.run(houses()) == []
    with db.connect(write=True) as conn:
        conn.executemany(
            'INSERT INTO house_districts(house_id,district) VALUES(?,?)',
            [('b', 'district-1'), ('a', 'district-1')],
        )
    assert asyncio.run(houses()) == ['a', 'b']
