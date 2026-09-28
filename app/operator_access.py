"""Настройка одного округа для оператора и округа для каждого дома."""
import argparse

from .access import MOSCOW_DISTRICTS
from .db import Database


def main():
    parser = argparse.ArgumentParser(description='Настроить округа Москвы для домов и операторов')
    parser.add_argument('--db', required=True, help='Путь к SQLite-базе')
    parser.add_argument('--house-id')
    parser.add_argument('--operator-id')
    parser.add_argument('--district', choices=MOSCOW_DISTRICTS)
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()

    if (args.house_id or args.operator_id) and not args.district:
        parser.error('для назначения дома или оператора укажите --district')
    if not args.list and not args.house_id and not args.operator_id:
        parser.error('укажите --house-id, --operator-id или --list')

    db = Database(args.db)
    db.initialize()
    with db.connect(write=True) as conn:
        if args.house_id:
            if not conn.execute('SELECT 1 FROM houses WHERE id=?', (args.house_id,)).fetchone():
                raise SystemExit(f'Дом не найден: {args.house_id}')
            conn.execute(
                'INSERT INTO house_districts(house_id,district) VALUES(?,?) '
                'ON CONFLICT(house_id) DO UPDATE SET district=excluded.district',
                (args.house_id, args.district),
            )
        if args.operator_id:
            user = conn.execute(
                'SELECT role FROM users WHERE id=?', (args.operator_id,),
            ).fetchone()
            if not user or user['role'] != 'operator':
                raise SystemExit(f'Оператор не найден: {args.operator_id}')
            conn.execute(
                'INSERT INTO operator_districts(user_id,district) VALUES(?,?) '
                'ON CONFLICT(user_id) DO UPDATE SET district=excluded.district',
                (args.operator_id, args.district),
            )
        if args.list:
            rows = conn.execute(
                "SELECT h.id,COALESCE(d.district,'Округ не указан') AS district,h.address "
                'FROM houses h LEFT JOIN house_districts d ON d.house_id=h.id ORDER BY h.id'
            ).fetchall()
            for row in rows:
                print(f"{row['id']}: {row['district']} — {row['address']}")

    if args.house_id:
        print(f'Дом {args.house_id} привязан к округу {args.district}.')
    if args.operator_id:
        print(f'Оператор {args.operator_id} ведёт округ: {args.district}.')


if __name__ == '__main__':
    main()
