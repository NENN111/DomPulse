"""Create short-lived enrollment codes. Run only from a trusted operator machine."""
import argparse
import secrets
import string
from datetime import datetime, timedelta, timezone

from .db import Database, token_hash


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    raw = ''.join(secrets.choice(alphabet) for _ in range(16))
    return '-'.join(raw[index:index + 4] for index in range(0, 16, 4))


def create_code(db: Database, house_id: str, role: str, ttl_hours: int, max_uses: int) -> tuple[str, str]:
    code = make_code()
    created_at = now()
    expires_at = (datetime.fromisoformat(created_at) + timedelta(hours=ttl_hours)).isoformat()
    with db.connect(write=True) as conn:
        if conn.execute('SELECT 1 FROM houses WHERE id=?', (house_id,)).fetchone() is None:
            raise ValueError(f'Дом {house_id!r} не найден')
        conn.execute(
            'INSERT INTO enrollment_codes(code_hash,house_id,role,expires_at,max_uses,created_at) '
            'VALUES(?,?,?,?,?,?)',
            (token_hash(code), house_id, role, expires_at, max_uses, created_at),
        )
    return code, expires_at


def main():
    parser = argparse.ArgumentParser(description='Выпустить одноразовый код привязки MAX-пользователя')
    parser.add_argument('--db', required=True, help='Явный путь к SQLite-базе')
    parser.add_argument('--house-id', required=True)
    parser.add_argument('--role', choices=['resident', 'operator'], required=True)
    parser.add_argument('--ttl-hours', type=int, default=24, choices=range(1, 24 * 31 + 1))
    parser.add_argument('--max-uses', type=int, default=1, choices=range(1, 1001))
    args = parser.parse_args()
    db = Database(args.db)
    db.initialize()
    code, expires_at = create_code(db, args.house_id, args.role, args.ttl_hours, args.max_uses)
    print(f'Код: {code}')
    print(f'Действует до: {expires_at}')
    print('Передайте код адресно. Пользователь вводит в MAX: /код <код>.')


if __name__ == '__main__':
    main()
