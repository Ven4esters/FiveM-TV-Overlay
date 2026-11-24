"""HLS-мост: переписывание плейлистов на наш прокси.

Зачем мост вообще: ссылки CDN подписаны на IP и протухают, а браузер игры
ходит к ним напрямую — тогда он видит протухшую ссылку и падает. Мост
забирает плейлист сам, свежим запросом, и отдаёт сегменты через себя.

ГЛАВНОЕ, чему научил v1 (ERR-081): hls.js не умеет объяснять обрыв.
Если мост падает молча (закрыл сокет без ответа), снаружи это выглядит как
`levelLoadError` без причины, и плеер не может переключиться на другое
качество. Поэтому ЛЮБАЯ ошибка здесь превращается в настоящий HTTP-код с
текстом причины. Это не косметика — это то, что позволяет видео играть,
когда одно зеркало легло.
"""

from __future__ import annotations

import logging
import urllib.parse

log = logging.getLogger(__name__)

PLAYLIST_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl")


def is_playlist(body: bytes) -> bool:
    """Это плейлист или сегмент? Смотрим содержимое, а не расширение.

    Ошибка v1: судили по `.m3u8` в адресе, а у VK и Kodik путь сегмента
    спрятан в параметре `?url=`. В результате сегмент принимали за плейлист
    и пытались переписать — плеер получал мусор. Содержимое не обманет:
    любой HLS-плейлист начинается с `#EXTM3U`.
    """
    return body.lstrip()[:7] == b"#EXTM3U"


def is_error_playlist(body: bytes) -> bool:
    """Плейлист-ошибка, который собрали мы сами, а не источник."""
    return b"#EXT-X-OVERLAY-ERROR:" in body


def rewrite(playlist: str, base_url: str, mount: str = "/hls") -> str:
    """Переписать все ссылки плейлиста на наш прокси.

    base_url — адрес, относительно которого в плейлисте лежат относительные
    пути. Это НЕ адрес master-плейлиста, если внутри лежат варианты
    (`#EXT-X-STREAM-INF`): у Kodik варианты — отдельные файлы на разных
    зеркалах, и каждый живёт относительно своего адреса. Поэтому варианты
    уже должны быть абсолютными — их подставляет сборщик master.
    """
    out: list[str] = []
    for line in playlist.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            # в тегах тоже бывают ссылки: ключи шифрования, аудио-дорожки
            # (URI="..."), карты инициализации (URI="...")
            out.append(_rewrite_tag_uris(stripped, base_url, mount))
            continue
        out.append(proxify(stripped, base_url, mount))
    return "\n".join(out)


def _rewrite_tag_uris(line: str, base_url: str, mount: str) -> str:
    """Переписать URI="..." внутри тега, не трогая остальной текст."""
    marker = 'URI="'
    if marker not in line:
        return line
    parts: list[str] = []
    rest = line
    while marker in rest:
        head, tail = rest.split(marker, 1)
        link, _, after = tail.partition('"')
        parts.append(head)
        parts.append(marker + proxify(link, base_url, mount) + '"')
        rest = after
    parts.append(rest)
    return "".join(parts)


def proxify(link: str, base_url: str, mount: str = "/hls") -> str:
    """Абсолютный адрес источника → адрес через наш прокси.

    Уже переписанные ссылки не оборачиваем дважды — иначе получается
    `/hls?url=/hls?url=...` и второй уровень не разбирается.
    """
    link = link.strip()
    if not link:
        return link
    if link.startswith((f"{mount}?", "/kodik-master?")):
        return link
    absolute = urllib.parse.urljoin(base_url, link)
    return f"{mount}?url={urllib.parse.quote(absolute, safe='')}"


def build_master(qualities: list[tuple[int, int, int, str]], audio_url: str = "") -> str:
    """Собрать master-плейлист из готовых ссылок качеств.

    qualities — [(height, width, bitrate_kbps, absolute_url), ...].
    audio_url — отдельная аудиодорожка (YouTube HLS): hls.js играет её как
    аудиогруппу `EXT-X-MEDIA`, и это штатный режим, а не костыль.

    Ссылки в master АБСОЛЮТНЫЕ: мост переписывает их при отдаче. Собирать
    «один base + имя файла» нельзя — у Kodik качества лежат на разных
    зеркалах (p12/p13/p14), и половина ссылок станет 404 (это была
    реальная ошибка v1, ERR-041).
    """
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    if audio_url:
        lines.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Звук",'
            f'DEFAULT=YES,AUTOSELECT=YES,URI="{audio_url}"'
        )
    for height, width, bitrate_kbps, url in sorted(qualities, key=lambda q: q[0], reverse=True):
        band = f"BANDWIDTH={max(bitrate_kbps, 1) * 1000}"
        if height > 0 and width > 0:
            band += f",RESOLUTION={width}x{height}"
        # Высота может быть неизвестна (мастер источника не разобрался) —
        # тогда RESOLUTION просто не пишем. `RESOLUTION=0x0` невалиден и
        # ломает разбор у hls.js, а BANDWIDTH по спецификации необязателен
        # только в крайнем случае, поэтому его оставляем всегда.
        if audio_url:
            band += ',AUDIO="audio"'
        lines.append(f"#EXT-X-STREAM-INF:{band}")
        lines.append(url)
    return "\n".join(lines) + "\n"


def error_playlist(reason: str) -> bytes:
    """Валидный HLS-плейлист с причиной отказа.

    Зачем плейлист, а не HTML: hls.js разберёт его и покажет понятную
    ошибку вместо «network error». Код ответа при этом 502 — плеер
    попробует другое качество.
    """
    quoted = urllib.parse.quote(reason[:160], safe="")
    return f"#EXTM3U\n#EXT-X-OVERLAY-ERROR:{quoted}\n".encode()


def decode_error(body: bytes) -> str:
    """Достать причину из плейлиста-ошибки (для журнала и интерфейса)."""
    marker = b"#EXT-X-OVERLAY-ERROR:"
    if marker not in body:
        return ""
    raw = body.split(marker, 1)[1].split(b"\n", 1)[0].decode("ascii", "replace")
    return urllib.parse.unquote(raw)
