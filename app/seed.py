"""Создание нового демонстрационного контура и локальных учётных данных."""
import argparse
import json
import secrets
from pathlib import Path

from .db import Database, token_hash
from .synthetic_data import populate


def main():
    parser = argparse.ArgumentParser(description='Создать синтетические данные и локальные токены')
    parser.add_argument('--db', required=True, help='Путь к SQLite-базе')
    args = parser.parse_args()
    db = Database(args.db)
    db.initialize()
    with db.connect() as conn:
        if conn.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            raise SystemExit('В базе уже есть пользователи. Повторная выдача локальных токенов отменена.')
    populate(args.db)
    accounts = [
        ('synthetic-resident-01-01', 'resident'),
        ('synthetic-operator-01', 'operator'),
    ]
    credentials = []
    with db.connect(write=True) as conn:
        for user_id, role in accounts:
            token = secrets.token_urlsafe(32)
            conn.execute('UPDATE users SET token_hash=? WHERE id=?', (token_hash(token), user_id))
            name = conn.execute('SELECT name FROM users WHERE id=?', (user_id,)).fetchone()['name']
            credentials.append({'id': user_id, 'name': name, 'role': role, 'token': token})
    out = Path(args.db).parent / 'local-test-users.json'
    with out.open('x', encoding='utf-8') as stream:
        json.dump(credentials, stream, ensure_ascii=False, indent=2)
    print(f'Синтетические данные созданы. Локальные токены: {out}. Не публикуйте этот файл.')


if __name__ == '__main__':
    main()
