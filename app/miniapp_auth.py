"""Проверка подписанных данных запуска мини-приложения MAX."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InvalidLaunchData(ValueError):
    pass


def validate_launch_data(raw: str, bot_token: str, max_age_seconds: int = 3600) -> int:
    if not raw or not bot_token or len(raw) > 8192:
        raise InvalidLaunchData('Данные запуска отсутствуют')
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        values = dict(pairs)
        if len(values) != len(pairs) or len(pairs) > 32 or len([key for key, _ in pairs if key == 'hash']) != 1:
            raise ValueError('Повторяющиеся параметры')
        supplied = values.pop('hash')
        if len(supplied) != 64:
            raise ValueError('Некорректная подпись')
        signed = '\n'.join(f'{key}={value}' for key, value in sorted(values.items()))
        secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, signed.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError('Неверная подпись')
        issued = int(values['auth_date'])
        if issued > time.time() + 60 or time.time() - issued > max_age_seconds:
            raise ValueError('Истёк срок данных запуска')
        user_id = json.loads(values['user'])['id']
        if type(user_id) is not int or user_id <= 0:
            raise ValueError('Некорректный пользователь')
        return user_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidLaunchData('Недействительные данные запуска MAX') from exc
