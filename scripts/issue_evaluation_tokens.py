"""Выдать отдельные Bearer-токены синтетическим ролям для проверки API."""
import argparse
import json
import secrets
from pathlib import Path

from app.db import Database, token_hash


def main():
    parser = argparse.ArgumentParser(description='Выдать тестовые токены жителя и оператора')
    parser.add_argument('--db', required=True, help='Путь к существующей SQLite-базе')
    parser.add_argument('--output', required=True, help='Локальный файл для передачи проверяющим')
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise SystemExit('Файл учётных данных уже существует. Повторная выдача отменена.')
    db = Database(args.db)
    with db.connect() as conn:
        resident = conn.execute(
            "SELECT id FROM users WHERE role='resident' AND house_id='synthetic-house-01-1' ORDER BY id LIMIT 1"
        ).fetchone()
        operator = conn.execute(
            "SELECT u.id FROM users u JOIN operator_districts od ON od.user_id=u.id "
            "JOIN house_districts hd ON hd.district=od.district "
            "WHERE u.role='operator' AND hd.house_id='synthetic-house-01-1' ORDER BY u.id LIMIT 1"
        ).fetchone()
    if resident is None or operator is None:
        raise SystemExit('Нет синтетического дома и обеих ролей. Сначала создайте тестовые данные.')
    accounts = ((resident['id'], 'resident'), (operator['id'], 'operator'))
    issued = []
    with db.connect(write=True) as conn:
        for user_id, role in accounts:
            row = conn.execute('SELECT role FROM users WHERE id=?', (user_id,)).fetchone()
            if row is None or row['role'] != role:
                raise SystemExit(f'Нет синтетического пользователя {user_id} с ролью {role}. Сначала создайте тестовые данные.')
        for user_id, role in accounts:
            token = secrets.token_urlsafe(32)
            conn.execute('UPDATE users SET token_hash=? WHERE id=?', (token_hash(token), user_id))
            issued.append({'id': user_id, 'role': role, 'token': token})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(issued, stream, ensure_ascii=False, indent=2)
    print(f'Тестовые токены сохранены только локально: {output}')


if __name__ == '__main__':
    main()
