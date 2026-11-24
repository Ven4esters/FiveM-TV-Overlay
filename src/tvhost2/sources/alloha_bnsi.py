"""Получение адресов потока у Alloha: `POST /bnsi/movies/<id_file>`.

Откуда это взялось: скрипт плеера (`build/app.c63ca8da.js`) обфусцирован, но
снимается полностью — см. `docs/REPORT_alloha_source.md`. Главное из него:

    hF = fileList.type === 'trailer' ? '/trailers/' : '/movies/';
    Q5().ajax('/bnsi' + hF + <id_file>, {
        method: 'POST',
        data: {token, av1, autoplay, …},
        headers: {'Borth': <отпечаток> + '|' + iP(iR(iS(<meta viewporti>)))}
    }).done(function (hX) {
        hX.hlsSource   // массив озвучек: {label, quality: {"<высота>": "<адрес>"}, audioId}
    });

То есть в разметке страницы адресов нет вообще — их отдаёт этот запрос.
Снаружи России он отвечает 404 «контент не найден» на ЛЮБОЙ id (проверено:
и на реальный, и на выдуманный, с любым токеном, телом и заголовками), на
компьютере в России — отдаёт данные. Поэтому путь и лежит отдельным модулем:
приложение запускается у заказчика в России, там он и работает.

Зачем `Borth` целиком, если первая половина — отпечаток браузера, а мы не
браузер: вторая половина (`iP(iR(iS(meta)))`) считается точно, и её одну
сервер принимает — проверка отпечатка, судя по ответам, не мешает. Если
однажды начнёт мешать, это будет видно по коду ответа (не 404, а 403).
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..web.fetch import FetchError, fetch_text
from .alloha_borth import borth_suffix

HTTP_OK = 200
"""Успех. Отдельной константой — иначе правило про «магические» числа."""

BNSI_TIMEOUT = 20
"""Секунд на запрос адресов. Ответ небольшой, дольше ждать нечего."""

HTTP_NOT_FOUND = 404
"""Региональный отказ приходит именно этим кодом, с текстом «не найден»."""

META_RE = re.compile(r'name="viewporti"\s+content="([^"]+)"')
"""Мета, из которой плеер собирает вторую половину `Borth`."""

FILE_LIST_RE = re.compile(r"fileList\s*[:=]\s*(\{.*?\})\s*[,;]\s*(?:const|var|let|\n)", re.S)
"""`fileList` — список файлов: в нём есть `id_file` и тип (фильм/сериал)."""

ID_FILE_RE = re.compile(r'"id_file"\s*:\s*"?(\d+)"?')
"""Номер файла — то, что идёт в адрес POST."""

TYPE_RE = re.compile(r'"type"\s*:\s*"(trailer|serial|movie)"')
"""Тип определяет ветку адреса: `/trailers/` или `/movies/`."""

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
"""Тот же UA, что у настоящего Chrome: плеер смотрит на него в отпечатке."""


def meta_of(player_html: str) -> str:
    """`content` меты `viewporti` — вход для перестановок `Borth`."""
    match = META_RE.search(player_html or "")
    return match.group(1) if match else ""


def file_ids(player_html: str) -> list[str]:
    """Номера файлов из `fileList`, в порядке разметки, без повторов.

    Первым идёт активный файл: в присланной странице это эпизод 6, и именно
    его плеер запрашивает при открытии.
    """
    text = player_html or ""
    block = FILE_LIST_RE.search(text)
    if block:
        text = block.group(1)
    ids: list[str] = []
    for match in ID_FILE_RE.finditer(text):
        value = match.group(1)
        if value not in ids:
            ids.append(value)
    return ids


def borth_for(player_html: str) -> str:
    """Заголовок `Borth`: отпечаток | перестановка меты.

    Первая половина — sha256 от набора, который собирает плеер (UA, часовой
    пояс, экран, язык, память, ядра). Canvas/WebGL/audio из браузера нам
    недоступны; проверено, что сервер с этим не спорит.
    """
    meta = meta_of(player_html)
    suffix = borth_suffix(meta) if meta else ""
    first = hashlib.sha256(
        "||".join([_DEFAULT_UA, "Europe/Moscow", "1920x1080", "ru-RU", "8", "8"]).encode()
    ).hexdigest()
    return f"{first}|{suffix}"


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
    """POST с JSON-телом. Возвращает код и тело — тело нужно и при ошибке.

    `fetch_text` для этого не годится: он бросает исключение на не-200, а нам
    важно РАЗЛИЧИТЬ «регион закрыт» (404 с текстом) и «сломалось» (сеть/500).
    """
    data = json.dumps(payload).encode()
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        **_common_headers(),
        **headers,
    }
    return _raw_post(url, data, request_headers)


def _common_headers() -> dict[str, str]:
    return {
        "User-Agent": _DEFAULT_UA,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }


def _raw_post(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=BNSI_TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # сеть бывает любой — код 0 значит «не дошло»
        return 0, str(exc)


def bnsi_path(embed: str, file_kind: str, ident: str) -> str:
    """Адрес запроса: `/bnsi/movies/<id>` или `/bnsi/trailers/<id>`."""
    base = (embed or "").split("?", 1)[0].rstrip("/")
    folder = "trailers" if file_kind == "trailer" else "movies"
    return f"{base}/bnsi/{folder}/{ident}"


def kind_of(player_html: str) -> str:
    """Тип файла из `fileList` (по умолчанию — сериал: в разметке так чаще)."""
    match = TYPE_RE.search(player_html or "")
    return match.group(1) if match else "serial"


def sources_of(response_body: str) -> list[dict[str, Any]]:
    """`hlsSource` из ответа — массив озвучек с адресами по высотам."""
    try:
        data = json.loads(response_body)
    except json.JSONDecodeError:
        return []
    value = data.get("hlsSource") if isinstance(data, dict) else None
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def asked_region(response_body: str) -> bool:
    """Сервер сказал «контент не найден» — это его ответ на чужой регион.

    Точный текст: «К сожалению, запрашиваемый контент не найден». Снаружи
    России он приходит на любой id, поэтому отличить «нет такого файла» от
    «вам нельзя» по нему нельзя — и то и другое объясняется одинаково.

    Тонкость, на которой легко обжечься: Alloha отдаёт ответ в JSON с
    ЭКРАНИРОВАННЫМ юникодом (`\u041a \u0441\u043e\u0436…`), поэтому искать
    русский текст в сыром теле бессмысленно — не найдётся никогда. Сначала
    раскрываем экранирование, потом ищем. Тест на это есть
    (`test_region_text_recognised`): без раскрытия он падает.
    """
    text = _unescape_unicode(response_body or "").lower()
    return "не найден" in text or "not found" in text


_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _unescape_unicode(text: str) -> str:
    """`\\u041a` → `К`. Только это: остальной JSON нам читать не нужно."""
    return _UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def quality_urls(source: dict[str, Any]) -> dict[int, str]:
    """Высоты и адреса из одной озвучки.

    Адрес — строка; в ней может лежать несколько резервных через ` or `
    (так их склеивает сервер). Берём первый: остальные — запасные зеркала,
    а мост умеет ходить только по одному адресу за раз.
    """
    raw = source.get("quality")
    if not isinstance(raw, dict):
        return {}
    result: dict[int, str] = {}
    for key, value in raw.items():
        try:
            height = int(str(key).split("x")[0])
        except ValueError:
            continue
        if not isinstance(value, str):
            continue
        url = value.split(" or ")[0].strip()
        if url:
            result[height] = url
    return result


def fetch_sources(embed: str, player_html: str) -> tuple[str, list[dict[str, Any]]]:
    """Главный вход: запросить адреса по каждому файлу, вернуть первый удачный.

    Возвращает `(тело ответа, hlsSource)`. Пустой список — значит, ни один
    файл не отдался (обычно это региональный отказ).
    """
    borth = borth_for(player_html)
    kind = kind_of(player_html)
    ids = file_ids(player_html)
    last_body = ""
    for ident in ids[:6]:
        url = bnsi_path(embed, kind, ident)
        status, body = _post_json(
            url,
            {"token": _site_token(embed), "av1": 0, "autoplay": 0},
            {"Borth": borth, "Referer": embed, "Origin": _origin_of(embed)},
        )
        last_body = body
        if status == HTTP_OK:
            sources = sources_of(body)
            if sources:
                return body, sources
        if not asked_region(body) and status not in (HTTP_OK, HTTP_NOT_FOUND):
            # Не «не найден» и не успех: похоже, дело в запросе, а не в
            # регионе. Второй файл уже не поможет — выходим с телом.
            break
    return last_body, []


def _site_token(embed: str) -> str:
    match = re.search(r"[?&]token=([0-9a-f]{20,})", embed or "")
    return match.group(1) if match else ""


def _origin_of(embed: str) -> str:
    parts = urllib.parse.urlsplit(embed or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""


def page_text(url: str, referer: str = "") -> str:
    """Скачать страницу плеера. Пусто — если её не отдали."""
    try:
        return fetch_text(url, {"Referer": referer} if referer else None)
    except FetchError:
        return ""
