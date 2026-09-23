"""Настройка сведений об управляющей компании для показа в MAX."""

import argparse
import hashlib

from .db import Database


def main():
    parser = argparse.ArgumentParser(description='Настроить информацию об УК для дома')
    parser.add_argument('--db', required=True, help='Явный путь к SQLite-базе')
    parser.add_argument('--house-id', required=True)
    parser.add_argument('--company-name', required=True)
    parser.add_argument('--office-address', required=True)
    parser.add_argument('--working-hours', required=True)
    parser.add_argument('--phone', required=True)
    parser.add_argument('--emergency-phone', required=True)
    args = parser.parse_args()

    values = (
        args.company_name, args.office_address, args.working_hours,
        args.phone, args.emergency_phone,
    )
    if any(not value.strip() or len(value) > 300 for value in values):
        parser.error('Все значения обязательны и не должны быть длиннее 300 символов.')

    db = Database(args.db)
    db.initialize()
    with db.connect(write=True) as conn:
        if conn.execute('SELECT 1 FROM houses WHERE id=?', (args.house_id,)).fetchone() is None:
            parser.error(f'Дом {args.house_id!r} не найден.')
        company_id = 'company-' + hashlib.sha256(
            args.company_name.strip().casefold().encode('utf-8')
        ).hexdigest()[:24]
        info_text = (
            f'Адрес офиса: {args.office_address}\n'
            f'Часы работы: {args.working_hours}\n'
            f'Телефон: {args.phone}\n'
            f'Аварийная служба: {args.emergency_phone}'
        )
        conn.execute(
            'INSERT INTO management_companies(id,name,info_text) VALUES(?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET name=excluded.name,info_text=excluded.info_text',
            (company_id, args.company_name.strip(), info_text),
        )
        conn.execute(
            'INSERT INTO house_management_companies(house_id,company_id) VALUES(?,?) '
            'ON CONFLICT(house_id) DO UPDATE SET company_id=excluded.company_id',
            (args.house_id, company_id),
        )
    print(f'Информация об УК для дома {args.house_id} сохранена.')


if __name__ == '__main__':
    main()
