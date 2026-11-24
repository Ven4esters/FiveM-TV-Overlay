"""Сетевой слой: один способ сходить к источнику и один способ объяснить сбой.

Зачем отдельный модуль: в v1 запросы были рассыпаны по прокси и источникам,
и каждый обрабатывал ошибки по-своему — где-то падал молча, где-то отдавал
HTML вместо плейлиста. Здесь один вход `fetch()`, один тип ошибки `FetchError`
и один набор заголовков на источник.

Заголовки важны не меньше логики: VK и YouTube требуют Chrome-отпечаток,
Kodik подписывает запросы под домен-партнёр и не любит чужой Referer.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT_S = 25
"""Сколько ждём ответа. Плейлисты и первые сегменты приходят за секунды,
а 25 с хватает на медленный мобильный прокси в игре."""

SERVER_ERROR_MIN = 500
"""Коды от 500 — вина источника; отдаём плееру 502, чтобы он повторил."""

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"

_CHROME_HOSTS = ("okcdn.ru", "googlevideo.com", "vk.com", "vkvideo.ru", "vkvideo", "mycdn.me")


@dataclass
class Response:
    """Ответ источника. Байты, а не строка: сегменты бинарные."""

    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    final_url: str = ""
    """Адрес после редиректов — от него считаются относительные пути."""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", self.headers.get("Content-Type", ""))

    def text(self) -> str:
        """Плейлисты и HTML: UTF-8 с заменой битых байтов."""
        return self.body.decode("utf-8", "replace")


class FetchError(Exception):
    """Сбой запроса с человеческой причиной.

    `reason` идёт в журнал и в интерфейс. Это не «исключение ради
    исключения»: hls.js видит только HTTP-код, а человек — текст.
    """

    def __init__(self, reason: str, status: int = 502) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def headers_for(url: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Заголовки под конкретный CDN.

    Chrome-отпечаток нужен VK/YouTube/VK-CDN: с Firefox-UA они отдают
    403 на подписанных ссылках. Остальным (Kodik, Rutube) Firefox
    подходит лучше — проверено на живых запросах.
    """
    low = (url or "").lower()
    ua = CHROME_UA if any(h in low for h in _CHROME_HOSTS) else FIREFOX_UA
    headers = {
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    if extra:
        headers.update(extra)
    return headers


def fetch(
    url: str,
    extra_headers: dict[str, str] | None = None,
    timeout: int = TIMEOUT_S,
    max_bytes: int | None = None,
) -> Response:
    """Запрос GET с разбором ошибок.

    max_bytes — предел чтения. Для плейлистов он не нужен, а для сегментов
    опасен: видео на 60 секунд это мегабайты, и читать его целиком в память
    ради ответа плееру нельзя.
    """
    if not url:
        raise FetchError("пустой адрес", 400)
    # не-ASCII в адресе ломает putrequest (это был реальный сбой ERR-081)
    safe_url = urllib.parse.quote(url, safe=":/?&=#%+@!$'()*,;[]~")
    request = urllib.request.Request(safe_url, headers=headers_for(url, extra_headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes) if max_bytes else response.read()
            headers = {k.lower(): v for k, v in response.headers.items()}
            return Response(
                status=response.status,
                body=body,
                headers=headers,
                final_url=response.geturl(),
            )
    except urllib.error.HTTPError as exc:
        code = exc.code
        detail = ""
        try:
            detail = exc.read(200).decode("utf-8", "replace").strip()
        except Exception:
            detail = ""
        reason = f"HTTP {code}"
        if detail:
            reason += f": {detail[:120]}"
        raise FetchError(reason, status=502 if code >= SERVER_ERROR_MIN else code) from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"источник недоступен: {exc.reason}", 502) from exc
    except TimeoutError as exc:
        raise FetchError("источник не ответил вовремя", 504) from exc
    except UnicodeEncodeError as exc:
        # отдельная причина: в адресе кириллица или пробел
        raise FetchError(f"недопустимые символы в адресе: {exc}", 400) from exc
    except OSError as exc:
        raise FetchError(f"обрыв связи: {exc.strerror or exc}", 502) from exc


def fetch_text(
    url: str, extra_headers: dict[str, str] | None = None, timeout: int = TIMEOUT_S
) -> str:
    """Текст плейлиста или страницы."""
    return fetch(url, extra_headers, timeout).text()


def probe_alive(
    urls: list[str], headers: dict[str, str] | None = None, timeout: int = 6
) -> set[str]:
    """Какие ссылки РЕАЛЬНО отдают плейлист прямо сейчас.

    Это лечит ERR-081 у корня: Kodik раздаёт качества с разных зеркал
    (p12/p13/p14), и одно может не ответить на конкретный файл. Если такое
    качество попадёт в master, плеер выберет его по битрейту и упадёт.

    Проверка параллельная: три запроса по 6 с идут одновременно, поэтому
    резолв замедляется максимум на 6 с (обычно — на доли секунды).

    Пустой результат значит «проверить не удалось» — вызывающий код тогда
    оставляет всё как есть: пустой список качеств хуже мёртвого зеркала.
    """
    if not urls:
        return set()
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    def alive(url: str) -> bool:
        try:
            response = fetch(url, headers, timeout=timeout, max_bytes=1024)
        except FetchError:
            return False
        return response.body.lstrip()[:7] == b"#EXTM3U"

    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        results = list(pool.map(alive, urls))
    live = {url for url, ok in zip(urls, results, strict=True) if ok}
    if live and len(live) < len(urls):
        log.info("из %d ссылок живы %d — мёртвые убраны", len(urls), len(live))
    return live


def json_get(url: str, headers: dict[str, str] | None = None, timeout: int = TIMEOUT_S) -> Any:
    """JSON-ответ источника (Twitch GQL, API каталогов)."""
    import json  # noqa: PLC0415

    text = fetch_text(url, headers, timeout)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(f"источник вернул не JSON: {text[:100]}", 502) from exc
