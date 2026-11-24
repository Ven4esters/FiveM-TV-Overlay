"""YouTube — самый сложный источник, и вот почему.

Что отдаёт YouTube (замеры 23.11.2026):

* видео — HLS-плейлисты (itag 269/229/230/229…270), **без звука**;
* аудио — HLS-плейлист (itag 233/234), но сегменты внутри — **сырые AAC
  файлы** (`ID3` в начале, не MPEG-TS). hls.js такой поток в группе
  `EXT-X-MEDIA` не проиграет;
* мультиплекса (видео+звук сразу) нет ни в VOD, ни в live.

**Важно (проверено экспериментом 23.11.2026).** Первый вывод был неверным:
казалось, что hls.js не проиграет сырой AAC в аудиогруппе. Замер в реальном
Chromium показал обратное — звук декодируется, работает перемотка и ручная
смена качества:

    старт+10с   rs=4  аудио=56 508 байт   видео=2 257 392   1920x1080
    +8с         rs=4  аудио=105 603       видео=8 917 911
    seek 2:05   rs=4  аудио=140 718       видео=12 610 178
    смена 480p  rs=4  аудио=183 436       видео=12 710 454   640x360

Поэтому штатный режим — обычный HLS-мастер с аудиогруппой: без скачивания,
без ожидания, без места на диске. Склейка через ffmpeg остаётся аварийным
путём для видео, где HLS-аудио нет вовсе.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..core import mux
from ..core.errors import SourceFailedError
from ..core.models import Quality, SourceState, Stream, StreamKind, sort_qualities
from .base import YtDlpSource, extract_info

log = logging.getLogger(__name__)

# itag → человеческое понятие. Берём только H.264 (avc1): браузер игры
# (CEF в GTA 5) не умеет VP9/AV1 в аппаратном режиме и часто вообще не умеет.
H264_ITAGS: dict[str, tuple[int, int, int]] = {
    "269": (256, 144, 290),
    "229": (426, 240, 546),
    "230": (640, 360, 1210),
    "231": (854, 480, 1569),
    "232": (1280, 720, 2969),
    "270": (1920, 1080, 5421),
}
"""itag → (ширина, высота, ориентировочный битрейт кбит/с)."""

# Аудио-дорожки YouTube в HLS. 233/234 — единственные, что отдаются
# плейлистом; остальные (140, 251) — прямые файлы, их в мастер не вставить.
AUDIO_HLS_ITAGS = ("233", "234")

_LIVE_MARKERS = ("/live", "live_stream", "is_live")


class YouTubeSource(YtDlpSource):
    name = "YouTube"
    hosts = (
        "youtube.com/watch",
        "youtube.com/live",
        "youtube.com/shorts",
        "youtu.be/",
        "m.youtube.com/watch",
        "youtube-nocookie.com",
    )
    state = SourceState.OK
    note = "до 1080p (H.264), видео и эфиры, без входа"
    kind = StreamKind.HLS

    def extractor_opts(self) -> dict[str, Any]:
        return {"format": "all"}

    # ------------------------------------------------------------------
    # выбор дорожек
    # ------------------------------------------------------------------

    @staticmethod
    def _video_hls(info: dict[str, Any]) -> dict[int, dict[str, Any]]:
        """HLS-дорожки с видео, по одной на высоту. Только H.264."""
        best: dict[int, dict[str, Any]] = {}
        for fmt in info.get("formats") or []:
            fmt_id = str(fmt.get("format_id") or "")
            if "m3u8" not in str(fmt.get("protocol") or ""):
                continue
            height = int(fmt.get("height") or 0)
            url = str(fmt.get("url") or "")
            if not height or not url:
                continue
            vcodec = str(fmt.get("vcodec") or "")
            if not vcodec.startswith("avc1"):
                continue  # VP9/AV1 браузер игры не тянет
            prev = best.get(height)
            if prev is None or float(fmt.get("tbr") or 0) > float(prev.get("tbr") or 0):
                best[height] = {**fmt, "format_id": fmt_id}
        return best

    @staticmethod
    def _audio_hls(info: dict[str, Any]) -> dict[str, Any] | None:
        """Аудио-плейлист (itag 233/234). yt-dlp зовёт их `vcodec=none`."""
        for fmt in info.get("formats") or []:
            if str(fmt.get("format_id") or "") in AUDIO_HLS_ITAGS and "m3u8" in str(
                fmt.get("protocol") or ""
            ):
                return fmt
        # Запасной: любая HLS-дорожка без высоты и без видеокодека.
        for fmt in info.get("formats") or []:
            if (
                "m3u8" in str(fmt.get("protocol") or "")
                and not fmt.get("height")
                and (fmt.get("vcodec") or "none") == "none"
            ):
                return fmt
        return None

    @staticmethod
    def _audio_file(info: dict[str, Any]) -> str:
        """Прямой аудиофайл — для аварийной склейки, когда HLS-аудио нет.

        Берём m4a (`140`), на крайний случай любой `acodec != none` без видео.
        Такие дорожки нельзя вставить в HLS-мастер, но ffmpeg их прочитает.
        """
        candidates = []
        for fmt in info.get("formats") or []:
            if fmt.get("height") or (fmt.get("vcodec") or "none") != "none":
                continue
            if (fmt.get("acodec") or "none") == "none":
                continue
            if "m3u8" in str(fmt.get("protocol") or ""):
                continue  # HLS-аудио разбирается отдельно
            url = str(fmt.get("url") or "")
            if url:
                candidates.append((str(fmt.get("format_id") or ""), url))
        # 140 — AAC 128k, самый совместимый; дальше по порядку.
        for fmt_id, url in candidates:
            if fmt_id == "140":
                return url
        return candidates[0][1] if candidates else ""

    @staticmethod
    def _subtitles(info: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        """Дорожки субтитров в виде, понятном плееру.

        yt-dlp отдаёт словарь язык → список вариантов, где каждый вариант —
        либо WEBVTT, либо SRT/TTML. Берём только VTT: браузер игры понимает
        его через `<track>` нативно, а конвертировать SRT на лету — лишний
        код и лишний повод для ошибки на пустом месте.

        Русский ставим первым: заказчик смотрит русскоязычные видео, и
        именно эту дорожку он включит с вероятностью 90%.
        """
        found: list[dict[str, Any]] = []
        subs = info.get("subtitles") or {}
        if not isinstance(subs, dict):
            return ()
        for lang, variants in subs.items():
            if not isinstance(variants, list):
                continue
            for variant in variants:
                if str(variant.get("ext") or "").lower() != "vtt":
                    continue
                url = str(variant.get("url") or "")
                if not url:
                    continue
                found.append(
                    {
                        "lang": str(lang),
                        "title": str(variant.get("name") or lang),
                        "url": url,
                    }
                )
                break  # один вариант на язык: дубли в списке только мешают

        def order(item: dict[str, Any]) -> tuple[int, str]:
            lang = item["lang"]
            if lang.startswith("ru"):
                return (0, lang)
            if lang.startswith("en"):
                return (1, lang)
            return (2, lang)

        return tuple(sorted(found, key=order))

    def build_qualities(self, info: dict[str, Any]) -> tuple[Quality, ...]:
        found: list[Quality] = []
        for height, fmt in self._video_hls(info).items():
            width = int(fmt.get("width") or 0) or round(height * 16 / 9)
            bitrate = int(fmt.get("tbr") or 0)
            if bitrate <= 0:
                # Ориентир из таблицы itag — он точнее, чем «площадь кадра».
                itag = str(fmt.get("format_id") or "")
                bitrate = H264_ITAGS.get(itag, (0, 0, 0))[2] or round(width * height * 30 / 10_000)
            found.append(
                Quality(
                    height=height,
                    width=width,
                    bitrate_kbps=bitrate,
                    url=str(fmt["url"]),
                )
            )
        return sort_qualities(found)

    def resolve(self, url: str) -> Stream:
        info = extract_info(url, **self.extractor_opts())
        is_live = bool(info.get("is_live"))

        qualities = self.build_qualities(info)
        audio = self._audio_hls(info)
        if not qualities:
            raise SourceFailedError(
                self.name,
                "YouTube не отдал ни одного H.264-потока",
                fix="Пришлите ссылку разработчику — возможно, у видео только VP9/AV1",
                state=SourceState.PARTIAL,
            )
        if audio is None:
            # Аварийный путь: HLS-аудио нет, но есть прямой файл. Склеиваем
            # ffmpeg-ом в локальный HLS. Без ffmpeg — честно говорим, что не
            # выйдет, вместо «видео играет без звука».
            audio_file = self._audio_file(info)
            if audio_file and mux.available():
                return self._via_mux(info, qualities, audio_file, is_live)
            raise SourceFailedError(
                self.name,
                "YouTube не отдал аудио-дорожку в HLS",
                fix=(
                    "Нужен ffmpeg для склейки звука: положите ffmpeg.exe рядом с приложением"
                    if audio_file
                    else "Попробуйте другую ссылку: у этого видео звук отдаётся только в браузере"
                ),
                state=SourceState.PARTIAL,
            )

        return Stream(
            source=self.name,
            title=str(info.get("title") or "").strip()
            or ("YouTube — эфир" if is_live else "YouTube"),
            kind=StreamKind.HLS,
            qualities=qualities,
            audio_url=str(audio.get("url") or ""),
            # У эфира длительности нет: yt-dlp отдаёт для live либо 0, либо
            # момент начала трансляции. Ноль правильнее — пульт не рисует
            # таймер и не пытается считать проценты.
            duration_s=0 if is_live else int(info.get("duration") or 0),
            is_live=is_live,
            subtitles=self._subtitles(info),
            state=self.state,
        )

    def _via_mux(
        self,
        info: dict[str, Any],
        qualities: tuple[Quality, ...],
        audio_file: str,
        is_live: bool,
    ) -> Stream:
        """Поток, склеенный из видео-плейлиста и отдельного аудиофайла.

        Видео берём в лучшем качестве: при склейке переключать качество
        нельзя — ffmpeg уже в работе. Это цена аварийного пути, и она
        приемлема: таких видео мало.
        """
        best = qualities[0]
        muxed = mux.muxed_for(best.url, audio_file)
        if muxed is None:  # ffmpeg исчез между проверкой и вызовом
            raise SourceFailedError(
                self.name,
                "не удалось запустить склейку звука",
                fix="Положите ffmpeg.exe рядом с приложением",
                state=SourceState.PARTIAL,
            )
        return Stream(
            source=self.name,
            title=(str(info.get("title") or "").strip() or "YouTube") + " (склейка)",
            # Один плейлист: его пишет ffmpeg, звук уже внутри.
            kind=StreamKind.HLS,
            qualities=(best,),
            duration_s=0 if is_live else int(info.get("duration") or 0),
            is_live=is_live,
            state=SourceState.PARTIAL,
        )


def is_live_link(url: str) -> bool:
    """Похоже ли на прямую трансляцию — до сети, по виду ссылки."""
    low = (url or "").lower()
    return any(marker in low for marker in _LIVE_MARKERS)


def video_id(url: str) -> str:
    """11-символьный id видео из любого вида ссылки YouTube."""
    match = re.search(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})", url or "")
    return match.group(1) if match else ""
