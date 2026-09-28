from app.db import Database, token_hash
from app.synthetic_data import populate


def test_synthetic_data_replaces_old_demo_and_preserves_max_links(tmp_path):
    path = tmp_path / 'demo.db'
    db = Database(str(path))
    db.initialize()
    with db.connect(write=True) as conn:
        conn.executemany('INSERT INTO houses(id,address) VALUES(?,?)', [
            ('house-1', 'Old training home'),
            ('real-house', 'Moscow, real home'),
        ])
        conn.execute('INSERT INTO house_districts(house_id,district) VALUES(?,?)', ('real-house', 'САО'))
        conn.execute('INSERT INTO users(id,name,role,house_id,token_hash) VALUES(?,?,?,?,?)',
                     ('resident-1', 'Training resident', 'resident', 'house-1', token_hash('old')))
        conn.execute('INSERT INTO users(id,name,role,house_id,token_hash) VALUES(?,?,?,?,?)',
                     ('max-operator', 'Real operator', 'operator', 'real-house', token_hash('real')))
        conn.execute('INSERT INTO operator_districts(user_id,district) VALUES(?,?)', ('max-operator', 'САО'))
        conn.execute('INSERT INTO max_links(max_user_id,user_id,linked_at) VALUES(?,?,?)',
                     (123456, 'max-operator', '2026-09-28T00:00:00+00:00'))

    assert populate(str(path)) == (12, 25, 100)
    assert populate(str(path)) == (12, 25, 100)

    with db.connect() as conn:
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('SELECT COUNT(DISTINCT district) FROM house_districts').fetchone()[0] == 12
        assert conn.execute('SELECT COUNT(*) FROM tickets').fetchone()[0] == 100
        assert conn.execute("SELECT COUNT(*) FROM houses WHERE id='house-1'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM users WHERE id='resident-1'").fetchone()[0] == 0
        assert conn.execute('SELECT user_id FROM max_links WHERE max_user_id=123456').fetchone()[0] == 'max-operator'
        assert conn.execute("SELECT district FROM operator_districts WHERE user_id='max-operator'").fetchone()[0] == 'САО'
