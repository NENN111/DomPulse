import sys

from app import management
from app.db import Database


def test_management_command_creates_and_updates_house_profile(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'dompulse.db'
    db = Database(str(path))
    db.initialize()
    with db.connect(write=True) as conn:
        conn.execute("INSERT INTO houses VALUES('house-1','Тестовый дом')")

    monkeypatch.setattr(sys, 'argv', [
        'management', '--db', str(path), '--house-id', 'house-1',
        '--company-name', 'ООО «Тестовая УК»', '--office-address', 'Москва, дом 1',
        '--working-hours', 'Пн–Пт 09:00–18:00', '--phone', '+7 000 000-00-00',
        '--emergency-phone', '+7 000 000-00-01',
    ])
    management.main()

    with db.connect() as conn:
        profile = dict(conn.execute(
            'SELECT mc.* FROM management_companies mc '
            'JOIN house_management_companies hmc ON hmc.company_id=mc.id '
            "WHERE hmc.house_id='house-1'"
        ).fetchone())
    assert profile['name'] == 'ООО «Тестовая УК»'
    assert 'Аварийная служба: +7 000 000-00-01' in profile['info_text']
    assert 'сохранена' in capsys.readouterr().out
