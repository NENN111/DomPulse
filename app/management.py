"""Configure the management-company profile shown to residents in MAX."""
import argparse
from datetime import datetime, timezone

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
    values = (args.company_name, args.office_address, args.working_hours, args.phone, args.emergency_phone)
    if any(not value.strip() or len(value) > 300 for value in values):
        parser.error('Все значения обязательны и не должны быть длиннее 300 символов.')
    db = Database(args.db)
    db.initialize()
    with db.connect(write=True) as conn:
        if conn.execute('SELECT 1 FROM houses WHERE id=?', (args.house_id,)).fetchone() is None:
            parser.error(f'Дом {args.house_id!r} не найден.')
        conn.execute(
            'INSERT INTO house_management_info(house_id,company_name,office_address,working_hours,phone,emergency_phone,updated_at) '
            'VALUES(?,?,?,?,?,?,?) ON CONFLICT(house_id) DO UPDATE SET '
            'company_name=excluded.company_name,office_address=excluded.office_address, '
            'working_hours=excluded.working_hours,phone=excluded.phone, '
            'emergency_phone=excluded.emergency_phone,updated_at=excluded.updated_at',
            (args.house_id, *values, datetime.now(timezone.utc).isoformat()),
        )
    print(f'Информация об УК для дома {args.house_id} сохранена.')


if __name__ == '__main__':
    main()
