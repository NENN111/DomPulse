"""Настройка округов Моск#2ы для домов и операторов."""
import argparse
from .access import MOSCOW_DISTRICTS
from .db import Database

def main():
    p=argparse.ArgumentParser(description="Настроить округа Москвы для домов и операторов")
    p.add_argument('--db',required=True,help='Путь к SQLite-базе'); p.add_argument('--house-id'); p.add_argument('--district',action='append',choices=MOSCOW_DISTRICTS); p.add_argument('--operator-id'); p.add_argument('--list',action='store_true')
    a=p.parse_args(); db=Database(a.db); db.initialize()
    with db.connect(write=True) as c:
        if a.house_id and a.district:
            if not c.execute('SELECT 1 FROM houses WHERE id=?',(a.house_id,)).fetchone(): raise SystemExit(f'Дом не найден: {a.house_id}')
            c.execute('INSERT INTO house_districts(house_id,district) VALUES(?,?) ON CONFLICT(house_id) DO UPDATE SET district=excluded.district',(a.house_id,a.district[0]))
        if a.operator_id and a.district:
            u=c.execute('SELECT id,role FROM users WHERE id=?',(a.operator_id,)).fetchone()
            if not u or u['role']!='operator': raise SystemExit(f'Оператор не найден: {a.operator_id}')
            c.execute('DELETE FROM operator_districts WHERE user_id=?',(a.operator_id,)); c.executemany('INSERT INTO operator_districts(user_id,district) VALUES(?,?)',[(a.operator_id,d) for d in dict.fromkeys(a.district)])
        if a.list:
            for r in c.execute("SELECT h.id,COALESCE(d.district,'Округ не указан') district,h.address FROM houses h LEFT JOIN house_districts d ON d.house_id=h.id ORDER BY h.id").fetchall(): print(f"{r['id']}: {r['district']} ? {r['address']}")
        if not a.list and not a.house_id and not a.operator_id: p.error('??????? --house-id/--operator-id ??? --list')
    if a.house_id and a.district: print(f"??? {a.house_id} ???????? ? ?????? {a.district[0]}.")
    if a.operator_id and a.district: print(f"???????? {a.operator_id} ????? ??????: {', '.join(dict.fromkeys(a.district))}.")
if __name__=='__main__': main()
