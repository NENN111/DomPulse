import asyncio

from app.addresses import address_key, canonical_house_address
from app.db import AsyncDatabase, Database


def test_moscow_address_ignores_case_optional_city_and_house_word():
    variants = [
        'лиственничная аллея 16',
        'Москва, Лиственничная аллея, дом 16',
        'г. МОСКВА, ЛИСТВЕННИЧНАЯ АЛЛЕЯ 16',
        'Москва, Лиственничная аллея, д. 16',
        'Россия, Москва, Лиственничная аллея, 16',
    ]
    assert len({address_key(value) for value in variants}) == 1
    assert {canonical_house_address(value) for value in variants} == {
        'Москва, Лиственничная аллея, дом 16'
    }
    assert address_key('Тверь, Лиственничная аллея, дом 16') != address_key(variants[0])
    assert address_key('Лиственничная аллея 18') != address_key(variants[0])
    assert canonical_house_address('Тестовый город, Учебная улица, дом 1') == (
        'Тестовый город, Учебная улица, дом 1'
    )


def test_existing_house_address_is_standardized_without_changing_id(tmp_path):
    path = str(tmp_path / 'houses.db')
    db = Database(path)
    db.initialize()
    with db.connect(write=True) as conn:
        conn.execute('INSERT INTO houses VALUES(?,?)',
                     ('address-original', 'лиственничная аллея 16'))
    asyncio.run(AsyncDatabase(path).initialize())
    with db.connect() as conn:
        row = conn.execute('SELECT id,address FROM houses').fetchone()
    assert row['id'] == 'address-original'
    assert row['address'] == 'Москва, Лиственничная аллея, дом 16'
