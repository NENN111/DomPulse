import os
import ssl
from typing import Any

import certifi
import httpx


class MaxAPIError(RuntimeError):
    """MAX rejected a request or returned an invalid response."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def create_tls_context() -> ssl.SSLContext:
    # Explicit CA configuration takes precedence over system and public roots.
    cafile, capath = os.getenv('SSL_CERT_FILE'), os.getenv('SSL_CERT_DIR')
    if cafile or capath:
        return ssl.create_default_context(cafile=cafile or None, capath=capath or None)
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


class MaxClient:
    def __init__(
        self,
        token: str,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not token:
            raise ValueError('MAX bot token is required')
        if not base_url:
            raise ValueError('MAX API base URL is required')
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip('/'),
            headers={'Authorization': token},
            timeout=10,
            transport=transport,
            verify=create_tls_context(),
        )

    async def close(self):
        await self.client.aclose()

    async def request_json(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            # Do not log URLs, headers, tokens or response bodies.
            raise MaxAPIError(
                f'MAX {path}: HTTP {status}', retryable=status == 429 or status >= 500,
            ) from None
        except httpx.TimeoutException:
            raise MaxAPIError(f'MAX {path}: превышено время ожидания ответа; проверьте сеть/VPN') from None
        except httpx.ConnectError as exc:
            cause = exc
            seen = set()
            certificate_error = False
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                if isinstance(cause, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(cause):
                    certificate_error = True
                    break
                cause = cause.__cause__ or cause.__context__
            if certificate_error:
                raise MaxAPIError(
                    f'MAX {path}: не удалось проверить HTTPS-сертификат. '
                    'Проверьте доверенные сертификаты Windows или задайте SSL_CERT_FILE '
                    'с доверенным CA-файлом в формате PEM.', retryable=False,
                ) from None
            raise MaxAPIError(f'MAX {path}: соединение не установлено; проверьте сеть, DNS, VPN и брандмауэр') from None
        except httpx.ProxyError:
            raise MaxAPIError(f'MAX {path}: ошибка подключения через прокси') from None
        except httpx.HTTPError as exc:
            raise MaxAPIError(f'MAX {path}: сетевая ошибка ({type(exc).__name__})') from None
        except ValueError:
            raise MaxAPIError(f'MAX {path}: некорректный JSON', retryable=False) from None
        if not isinstance(payload, dict):
            raise MaxAPIError(f'MAX {path}: ожидался объект JSON', retryable=False)
        return payload

    async def bot_id(self) -> int:
        payload = await self.request_json('GET', '/me')
        if type(payload.get('user_id')) is not int:
            raise MaxAPIError('MAX /me: отсутствует user_id', retryable=False)
        return payload['user_id']

    async def subscriptions(self) -> list[dict[str, Any]]:
        payload = await self.request_json('GET', '/subscriptions')
        subscriptions = payload.get('subscriptions')
        if not isinstance(subscriptions, list) or any(
            not isinstance(item, dict) or not isinstance(item.get('url'), str)
            for item in subscriptions
        ):
            raise MaxAPIError('MAX /subscriptions: некорректный список', retryable=False)
        return subscriptions

    async def delete_subscription(self, url: str):
        payload = await self.request_json('DELETE', '/subscriptions', params={'url': url})
        if payload.get('success') is not True:
            raise MaxAPIError('MAX: не удалось отключить webhook', retryable=False)

    async def get_updates(self, marker: int | None, timeout: int = 30) -> dict[str, Any]:
        params = {'limit': 100, 'timeout': timeout, 'types': 'message_created,bot_started'}
        if marker is not None:
            params['marker'] = marker
        payload = await self.request_json(
            'GET', '/updates', params=params,
            timeout=httpx.Timeout(10, read=timeout + 10),
        )
        if (
            not isinstance(payload.get('updates'), list)
            or 'marker' not in payload
            or (payload['marker'] is not None and type(payload['marker']) is not int)
            or any(not isinstance(item, dict) or not isinstance(item.get('update_type'), str)
                   for item in payload['updates'])
        ):
            raise MaxAPIError('MAX /updates: некорректный пакет событий', retryable=False)
        return payload

    async def send_message(
        self,
        user_id: int,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str | None:
        body: dict[str, Any] = {'text': text}
        if attachments:
            body['attachments'] = attachments
        payload = await self.request_json('POST', '/messages', params={'user_id': user_id}, json=body)

        message = payload.get('message', payload) if isinstance(payload, dict) else {}
        body_payload = message.get('body', {}) if isinstance(message, dict) else {}
        return body_payload.get('mid') or message.get('mid')

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
