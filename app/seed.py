"""Explicit local test-data creation; never called automatically on startup."""
import argparse
import json
import secrets
from pathlib import Path
from datetime import datetime, timezone
from .db import Database, token_hash


def main():
    parser = argparse.ArgumentParser(description='Создать синтетические данные и случайные токены для локального теста')
    parser.add_argument('--db', required=True, help='Явный путь к локальной SQLite-базе')
    args = parser.parse_args()
    db = Database(args.db)
    db.initialize()
    accounts = [
        ('resident-1', 'Житель А', 'resident', 'house-1'),
        ('resident-2', 'Житель Б', 'resident', 'house-1'),
        ('operator-1', 'Диспетчер УК А', 'operator', 'house-1'),
        ('operator-2', 'Диспетчер УК Б', 'operator', 'house-2'),
    ]
    credentials = []
    with db.connect(write=True) as conn:
        if conn.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            raise SystemExit('В базе уже есть пользователи. Повторное создание токенов отменено.')
        conn.executemany('INSERT INTO houses VALUES(?,?)',
                         [('house-1', 'Тестовый город, Учебная улица, дом 1'),
                          ('house-2', 'Тестовый город, Учебная улица, дом 2')])
        conn.executemany(
            'INSERT INTO house_management_info VALUES(?,?,?,?,?,?,?)',
            [
                ('house-1', 'УК «ДомПульс»', 'Тестовый город, Учебная улица, дом 3',
                 'Пн–Пт: 09:00–18:00', '+7 (000) 000-00-01', '+7 (000) 000-00-11',
                 datetime.now(timezone.utc).isoformat()),
                ('house-2', 'УК «ДомПульс Север»', 'Тестовый город, Северная улица, дом 5',
                 'Пн–Пт: 09:00–18:00', '+7 (000) 000-00-02', '+7 (000) 000-00-12',
                 datetime.now(timezone.utc).isoformat()),
            ],
        )
        for uid, name, role, house in accounts:
            token = secrets.token_urlsafe(32)
            conn.execute('INSERT INTO users VALUES(?,?,?,?,?)', (uid, name, role, house, token_hash(token)))
            credentials.append({'id': uid, 'name': name, 'role': role, 'token': token})
        out = Path(args.db).parent / 'local-test-users.json'
        with out.open('x', encoding='utf-8') as stream:
            json.dump(credentials, stream, ensure_ascii=False, indent=2)
    print(f'Синтетические пользователи созданы. Локальные токены: {out}. Не публикуйте этот файл.')


if __name__ == '__main__':
    main()
