import asyncio
import ssl

from app import geocoder


def test_geocoder_uses_independent_ca_bundle_and_detects_district(monkeypatch):
    payload = {
        'response': {'GeoObjectCollection': {'featureMember': [{
            'GeoObject': {
                'metaDataProperty': {'GeocoderMetaData': {
                    'text': '\u0420\u043e\u0441\u0441\u0438\u044f, \u041c\u043e\u0441\u043a\u0432\u0430, \u0422\u0432\u0435\u0440\u0441\u043a\u0430\u044f \u0443\u043b\u0438\u0446\u0430, 1',
                    'precision': 'exact',
                    'Address': {'Components': [{'name': '\u0426\u0435\u043d\u0442\u0440\u0430\u043b\u044c\u043d\u044b\u0439 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u0438\u0432\u043d\u044b\u0439 \u043e\u043a\u0440\u0443\u0433'}]},
                }},
                'Point': {'pos': '37.617700 55.755800'},
            },
        }]}}
    }
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params):
            captured['url'] = url
            captured['params'] = params
            return Response()

    monkeypatch.setenv('YANDEX_MAPS_API_KEY', 'test-key')
    monkeypatch.setattr(geocoder.httpx, 'AsyncClient', Client)

    result = asyncio.run(geocoder.geocode_address('\u041c\u043e\u0441\u043a\u0432\u0430, \u0422\u0432\u0435\u0440\u0441\u043a\u0430\u044f, 1'))

    assert isinstance(captured['verify'], ssl.SSLContext)
    assert result.district == '\u0426\u0410\u041e'
    assert result.latitude == 55.7558
    assert result.longitude == 37.6177


def test_geocoder_resolves_district_by_coordinates(monkeypatch):
    forward = {
        'response': {'GeoObjectCollection': {'featureMember': [{
            'GeoObject': {
                'metaDataProperty': {'GeocoderMetaData': {
                    'text': '\u0420\u043e\u0441\u0441\u0438\u044f, \u041c\u043e\u0441\u043a\u0432\u0430, \u0422\u0432\u0435\u0440\u0441\u043a\u0430\u044f \u0443\u043b\u0438\u0446\u0430, 1',
                    'precision': 'exact',
                }},
                'Point': {'pos': '37.614716 55.756315'},
            },
        }]}}
    }
    reverse = {
        'response': {'GeoObjectCollection': {'featureMember': [{
            'GeoObject': {'name': '\u0426\u0435\u043d\u0442\u0440\u0430\u043b\u044c\u043d\u044b\u0439 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u0438\u0432\u043d\u044b\u0439 \u043e\u043a\u0440\u0443\u0433'},
        }]}}
    }
    responses = [forward, reverse]
    requests = []

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params):
            requests.append(params)
            return Response(responses.pop(0))

    monkeypatch.setenv('YANDEX_MAPS_API_KEY', 'test-key')
    monkeypatch.setattr(geocoder.httpx, 'AsyncClient', Client)

    result = asyncio.run(geocoder.geocode_address('\u041c\u043e\u0441\u043a\u0432\u0430, \u0422\u0432\u0435\u0440\u0441\u043a\u0430\u044f, 1'))

    assert result.district == '\u0426\u0410\u041e'
    assert requests[1]['kind'] == 'district'
    assert requests[1]['geocode'] == '37.614716,55.756315'
