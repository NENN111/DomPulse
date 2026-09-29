import argparse
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .access import MOSCOW_DISTRICTS
from .db import Database, token_hash

ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def make_code() -> str:
    raw = ''.join(secrets.choice(ALPHABET) for _ in range(16))
    return '-'.join(raw[index:index + 4] for index in range(0, 16, 4))


def issue_codes(
    db: Database,
    output: Path,
    operator_count: int = 3,
    resident_count: int = 2,
) -> list[dict]:
    if not 1 <= operator_count <= len(MOSCOW_DISTRICTS):
        raise ValueError('operator_count must be between 1 and 12')
    if not 1 <= resident_count <= len(MOSCOW_DISTRICTS):
        raise ValueError('resident_count must be between 1 and 12')
    if output.exists():
        raise FileExistsError(output)
    db.initialize()
    issued_at = datetime.now(timezone.utc).isoformat()
    with db.connect(write=True) as conn:
        rows = conn.execute(
            'SELECT hd.district,h.id AS house_id,h.address FROM houses h '
            'JOIN house_districts hd ON hd.house_id=h.id '
            "ORDER BY CASE WHEN h.id LIKE 'synthetic-house-%' THEN 0 ELSE 1 END,h.id"
        ).fetchall()
        houses = {}
        for row in rows:
            houses.setdefault(row['district'], dict(row))
        selected_districts = MOSCOW_DISTRICTS[:max(operator_count, resident_count)]
        missing = [district for district in selected_districts if district not in houses]
        if missing:
            raise ValueError('No houses for districts: ' + ', '.join(missing))
        chosen = [(district, 'operator') for district in MOSCOW_DISTRICTS[:operator_count]]
        chosen += [(district, 'resident') for district in MOSCOW_DISTRICTS[:resident_count]]
        result = []
        for district, role in chosen:
            house = houses[district]
            existing = conn.execute(
                'SELECT 1 FROM permanent_enrollment_codes '
                'WHERE house_id=? AND role=? AND revoked_at IS NULL',
                (house['house_id'], role),
            ).fetchone()
            if existing:
                raise ValueError(f'An active permanent code already exists for {role} in {district}')
            code = make_code()
            conn.execute(
                'INSERT INTO permanent_enrollment_codes'
                '(code_hash,house_id,role,created_at) VALUES(?,?,?,?)',
                (token_hash(code), house['house_id'], role, issued_at),
            )
            result.append({
                'district': district, 'role': role, 'house_id': house['house_id'],
                'address': house['address'], 'code': code,
            })
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        output.chmod(0o600)
    return result


def revoke_code(db: Database, code: str) -> bool:
    db.initialize()
    normalized = code.strip().upper()
    with db.connect(write=True) as conn:
        cursor = conn.execute(
            'UPDATE permanent_enrollment_codes SET revoked_at=? '
            'WHERE code_hash=? AND revoked_at IS NULL',
            (datetime.now(timezone.utc).isoformat(), token_hash(normalized)),
        )
        return cursor.rowcount == 1


def main():
    parser = argparse.ArgumentParser(
        description='\u0412\u044b\u0434\u0430\u0442\u044c \u0438\u043b\u0438 \u043e\u0442\u043e\u0437\u0432\u0430\u0442\u044c \u043f\u043e\u0441\u0442\u043e\u044f\u043d\u043d\u044b\u0435 \u043a\u043e\u0434\u044b \u043f\u0440\u0438\u0432\u044f\u0437\u043a\u0438 MAX'
    )
    parser.add_argument('--db', required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--output', type=Path)
    action.add_argument('--revoke')
    parser.add_argument('--operators', type=int, default=3)
    parser.add_argument('--residents', type=int, default=2)
    args = parser.parse_args()
    db = Database(args.db)
    if args.revoke:
        if not revoke_code(db, args.revoke):
            raise SystemExit('\u0414\u0435\u0439\u0441\u0442\u0432\u0443\u044e\u0449\u0438\u0439 \u043a\u043e\u0434 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d')
        print('\u041a\u043e\u0434 \u043e\u0442\u043e\u0437\u0432\u0430\u043d')
    else:
        result = issue_codes(db, args.output, args.operators, args.residents)
        print(f'\u0412\u044b\u0434\u0430\u043d\u043e {len(result)} \u043a\u043e\u0434\u043e\u0432. \u0424\u0430\u0439\u043b: {args.output}')


if __name__ == '__main__':
    main()
