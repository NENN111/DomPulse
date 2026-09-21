"""Проверка московского адреса через HTTP Геокодер Яндекса."""
from dataclasses import dataclass
import os
import httpx

DISTRICTS = {
    "центральный административный округ": "ЦАО", "северный административный округ": "САО",
    "северо-восточный административный округ": "СВАО", "восточный административный округ": "ВАО",
    "юго-восточный административный округ": "ЮВАО", "южный административный округ": "ЮАО",
    "юго-западный административный округ": "ЮЗАО", "западный административный округ": "ЗАО",
    "северо-западный административный округ": "СЗАО", "зеленоградский административный округ": "Зеленоградский",
    "новомосковский административный округ": "Новомосковский", "троицкий административный округ": "Троицкий",
}

class GeocoderError(RuntimeError):
    pass

@dataclass(frozen=True)
class GeocodedAddress:
    normalized_address: str
    district: str
    latitude: float
    longitude: float


def _strings(value):
    if isinstance(value, str): yield value
    elif isinstance(value, dict):
        for item in value.values(): yield from _strings(item)
    elif isinstance(value, list):
        for item in value: yield from _strings(item)


def _district(payload):
    text=' '.join(x.lower() for x in _strings(payload))
    for name, code in DISTRICTS.items():
        if name in text: return code
    return None

async def geocode_address(address: str) -> GeocodedAddress:
    key=os.getenv('YANDEX_MAPS_API_KEY')
    if not key: raise GeocoderError('Геокодер Яндекса не настроен')
    url=os.getenv('YANDEX_GEOCODER_URL','https://geocode-maps.yandex.ru/1.x/')
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response=await client.get(url, params={'apikey':key,'geocode':address,'format':'json','lang':'ru_RU'})
            response.raise_for_status(); payload=response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GeocoderError('Не удалось проверить адрес через геокодер') from exc
    members=payload.get('response',{}).get('GeoObjectCollection',{}).get('featureMember',[])
    if not members: raise GeocoderError('Адрес не найден в геокодере')
    obj=members[0].get('GeoObject',{}); meta=obj.get('metaDataProperty',{}).get('GeocoderMetaData',{})
    precision=meta.get('precision')
    if precision not in {'exact','number'}: raise GeocoderError('Укажите адрес до номера дома')
    pos=(obj.get('Point',{}).get('pos') or '').split()
    try: longitude, latitude=float(pos[0]), float(pos[1])
    except (ValueError, IndexError) as exc: raise GeocoderError('Геокодер не вернул координаты дома') from exc
    district=_district(obj)
    if not district: raise GeocoderError('Не удалось определить административный округ')
    return GeocodedAddress(meta.get('text',address),district,latitude,longitude)
