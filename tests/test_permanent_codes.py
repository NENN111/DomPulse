import asyncio
from datetime import datetime, timezone

import pytest

from app.access import MOSCOW_DISTRICTS
from app.db import AsyncDatabase, Database, token_hash
from app.max_webhook import enroll_with_code
from app.permanent_codes import issue_codes, revoke_code


def test_permanent_codes_cover_districts_and_can_be_reused_then_revoked(tmp_path):
    path = str(tmp_path / 'codes.db')
    db = Database(path)
    db.initialize()
    with db.connect(write=True) as conn:
        for index, district in enumerate(MOSCOW_DISTRICTS, 1):
            house_id = f'synthetic-house-{index:02d}-1'
            conn.execute('INSERT INTO houses(id,address) VALUES(?,?)', (house_id, f'Home {index}'))
            conn.execute('INSERT INTO house_districts(house_id,district) VALUES(?,?)', (house_id, district))

    output = tmp_path / 'access-codes.json'
    codes = issue_codes(db, output)
    assert len(codes) == 5
    assert [item['district'] for item in codes if item['role'] == 'operator'] == list(MOSCOW_DISTRICTS[:3])
    assert [item['district'] for item in codes if item['role'] == 'resident'] == list(MOSCOW_DISTRICTS[:2])
    assert len({item['code'] for item in codes}) == 5
    assert output.exists()
    with db.connect() as conn:
        rows = conn.execute('SELECT code_hash FROM permanent_enrollment_codes').fetchall()
        assert len(rows) == 5
        assert {row['code_hash'] for row in rows} == {token_hash(item['code']) for item in codes}
    with pytest.raises(FileExistsError):
        issue_codes(db, output)

    operator = codes[0]
    resident = next(item for item in codes if item['role'] == 'resident')

    async def enroll(max_id, code):
        async with AsyncDatabase(path).connect(write=True) as conn:
            return await enroll_with_code(
                conn, max_id, {'user': {'name': f'Test {max_id}'}},
                code, datetime.now(timezone.utc).isoformat(),
            )

    for max_id in (101, 102):
        user, error = asyncio.run(enroll(max_id, operator['code'].lower()))
        assert error is None
        assert user['role'] == 'operator'
    for max_id in (201, 202):
        user, error = asyncio.run(enroll(max_id, resident['code']))
        assert error is None
        assert user['role'] == 'resident'
    with db.connect() as conn:
        assert conn.execute(
            'SELECT used_count FROM permanent_enrollment_codes WHERE code_hash=?',
            (token_hash(operator['code']),),
        ).fetchone()[0] == 2
        assigned = conn.execute(
            'SELECT DISTINCT district FROM operator_districts WHERE user_id IN (?,?)',
            ('max-operator-101', 'max-operator-102'),
        ).fetchall()
        assert [row['district'] for row in assigned] == [operator['district']]

    assert revoke_code(db, operator['code']) is True
    assert revoke_code(db, operator['code']) is False
    user, error = asyncio.run(enroll(103, operator['code']))
    assert user is None and error
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM max_links WHERE max_user_id IN (101,102)').fetchone()[0] == 2
