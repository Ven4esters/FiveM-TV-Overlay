"""Rutube — первый перенесённый источник.

Что важно сохранить из v1 (тег v1.0-working):

1. Rutube отдаёт ОДНУ HLS-дорожку на качество, видео и звук вместе.
   Нельзя брать «первый попавшийся m3u8» — так видео становится без звука.

2. Битрейт обязан быть настоящим (`tbr`). ERR-067: завышенный BANDWIDTH
   в master-плейлисте заставлял hls.js навсегда остаться на 144p —
   видео было мыльным, и это считалось «багом плеера», хотя виноват был
   наш master-плейлист.

3. Ссылки `rutube.ru/play/embed/<hash>` и `/video/<hash>/` должны попадать
   сюда, а не в «универсальный поиск потока».
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..core.errors import RightsBlockedError
from ..core.models import Quality, SourceState, Stream, StreamKind
from .base import YtDlpSource, extract_info, pick_hls_qualities

RU_COUNTRIES = {"RU", "BY", "KZ", "AM", "KG", "MD", "AZ", "GE", "UZ", "TJ"}
"""Страны, для которых Rutube ограничивает показ реже всего."""

log = logging.getLogger(__name__)

_HASH_RE = re.compile(r"(?:video|embed)/([0-9a-f]{32})", re.IGNORECASE)
EMBED_URL = "https://rutube.ru/play/embed/{video_id}"


def video_id(url: str) -> str:
    """Вытащить 32-символьный hash видео из любого вида ссылки Rutube."""
    match = _HASH_RE.search(url or "")
    return match.group(1).lower() if match else ""


class RutubeSource(YtDlpSource):
    name = "Rutube"
    hosts = (
        "rutube.ru/video",
        "rutube.ru/play/embed",
        "rutube.ru/embed",
        "rutube.ru/metainfo",
    )
    state = SourceState.OK
    note = "до 1080p, без входа"
    kind = StreamKind.HLS

    def build_qualities(self, info: dict[str, Any]) -> tuple[Quality, ...]:
        """Все качества сразу, а не только лучшее.

        v1 брал только 1080p → на слабом канале сегменты не успевали
        скачаться, и hls.js падал с manifestLoadError. Теперь отдаём весь
        набор, и ABR сам выбирает по скорости.
        """
        qualities = pick_hls_qualities(info.get("formats") or [])
        if qualities:
            return qualities
        # Запасной путь: одна мультиплексная дорожка без разбивки по высотам.
        mux = str(info.get("hls_manifest_url") or "")
        if mux:
            height = int(info.get("height") or 720)
            width = int(info.get("width") or round(height * 16 / 9))
            tbr = int(info.get("tbr") or 0) or round(width * height * 30 / 10_000)
            return (Quality(height=height, width=width, bitrate_kbps=tbr, url=mux),)
        return ()

    def resolve(self, url: str) -> Stream:
        """Ссылка → поток с запасным путём через embed.

        Бывает, что страница видео отдаёт пустую разметку (A/B-тест,
        требование реферера). Тогда пробуем embed — там плеер тот же.
        """
        try:
            return super().resolve(url)
        except RightsBlockedError as exc:
            # Правообладатель назвал причину — embed её не обойдёт.
            log.info("Rutube: показ ограничен для этой сети (%s)", exc.why)
            raise
        except Exception:
            vid = video_id(url)
            if not vid or "play/embed" in url:
                raise
            log.info("Rutube: повтор через embed для %s", vid)
            info = extract_info(EMBED_URL.format(video_id=vid))
            qualities = self.build_qualities(info)
            if not qualities:
                raise
            return Stream(
                source=self.name,
                title=str(info.get("title") or "").strip() or "Rutube",
                kind=self.kind,
                qualities=qualities,
                duration_s=int(info.get("duration") or 0),
                state=self.state,
            )
