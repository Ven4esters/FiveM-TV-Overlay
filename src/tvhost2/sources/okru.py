"""OK.ru (Одноклассники) — видео с публичных страниц.

Что важно про этот источник и почему он не похож на остальные:

1. **yt-dlp отдаёт `vcodec=None` у HLS-дорожек.** Не `"none"`, не `"avc1"`,
   а именно `None` — то есть «не знаю». Общая функция `pick_hls_qualities()`
   считает `None` за «дорожка без видео» и выбрасывает ВСЕ качества: на
   выходе пустой список, источник выглядел бы сломанным. Поэтому сборщик
   здесь свой: для OK.ru `height > 0` уже значит «это видео».

2. **HLS-дорожки OK.ru идут со звуком внутри.** Проверено: в плейлисте
   `hls-1920` сегменты MPEG-TS, звук внутри, отдельная аудиодорожка не
   нужна. Это лучше, чем у YouTube: не требуется аудиогруппа.

3. **VP9 и Opus браузер игры не тянет.** Прямые файлы `webm-*` (VP9) и
   аудиодорожки Opus (`a=opus`) в CEF воспроизводятся через раз: аппаратного
   декодера нет, а программный на 8 ГБ рядом с игрой — плохая идея. Берём
   только HLS: там H.264.

4. **Ссылки живут недолго.** CDN OK.ru подписывает адреса, поэтому мост
   обязан работать: браузер игры не должен ходить к `okcdn.ru` напрямую —
   и из-за срока жизни ссылки, и из-за заголовков.

Что НЕ поддерживается намеренно: прямые эфиры OK.ru. yt-dlp отдаёт для них
неполные данные, а цельного потока до конца трансляции не существует.
Скажем об этом прямо, а не «неизвестная ошибка».
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..core.models import Quality, SourceState, Stream, StreamKind, sort_qualities
from .base import YtDlpSource, extract_info

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"(?:ok\.ru|odnoklassniki\.ru)/(?:video|web_video)/(\d+)", re.IGNORECASE)


class OkRuSource(YtDlpSource):
    name = "OK.ru"
    hosts = (
        "ok.ru/video/",
        "ok.ru/web_video/",
        "m.ok.ru/video/",
        "odnoklassniki.ru/video/",
    )
    state = SourceState.OK
    note = "до 1080p, без входа"
    kind = StreamKind.HLS

    def extractor_opts(self) -> dict[str, Any]:
        return {"format": "all"}

    @staticmethod
    def _hls_qualities(info: dict[str, Any]) -> tuple[Quality, ...]:
        """HLS-дорожки OK.ru, по одной на высоту.

        Почему не общая pick_hls_qualities(): у OK.ru `vcodec` не заполнен
        (None, не "none"), и общая функция считает такую дорожку «без видео» —
        выбрасывает все качества подряд, источник выглядел бы сломанным.
        Надёжный признак видео здесь — непустой height.

        Поля height/width у OK.ru обычные: 1080p — это height=1080,
        width=1920. Проверено на живой ссылке; в тестах данные должны быть
        такими же, иначе проверяется выдуманная проблема.
        """
        best: dict[int, Quality] = {}
        for fmt in info.get("formats") or []:
            url = str(fmt.get("url") or "")
            if not url or "m3u8" not in str(fmt.get("protocol") or ""):
                continue
            height = int(fmt.get("height") or 0)
            if height <= 0:
                continue
            width = int(fmt.get("width") or 0) or round(height * 16 / 9)
            bitrate = int(fmt.get("tbr") or 0)
            if bitrate <= 0:
                # Нижняя граница для H.264: 0.1 бит на пиксель в секунду.
                bitrate = max(1, round(width * height * 30 / 10_000))
            candidate = Quality(height=height, width=width, bitrate_kbps=bitrate, url=url)
            # На одну высоту — дорожка с большим битрейтом (ERR-067).
            prev = best.get(height)
            if prev is None or candidate.bitrate_kbps > prev.bitrate_kbps:
                best[height] = candidate
        return sort_qualities(list(best.values()))

    def build_qualities(self, info: dict[str, Any]) -> tuple[Quality, ...]:
        return self._hls_qualities(info)

    def _extract(self, url: str) -> dict[str, Any]:
        """Разбор с двумя повторами.

        yt-dlp на OK.ru нестабилен: та же самая ссылка разбирается через раз
        с ошибкой `Unable to extract player`, хотя видео живое. Проверено:
        один и тот же адрес дал «ошибка, ошибка, успех, успех» в четырёх
        запросах подряд. Это не наша ошибка и не удалённое видео — сервер
        OK.ru отдаёт разметку не всегда, а yt-dlp её не находит.

        Поэтому три попытки: обычная страница, она же с параметром
        (другой кеш CDN), и embed-версия. Мобильный хост yt-dlp не знает —
        проверено, там «Unsupported URL», поэтому его не пробуем.
        """
        vid = video_id(url)
        attempts = [url]
        if vid:
            attempts += [
                f"https://ok.ru/video/{vid}?fromTime=0",
                f"https://ok.ru/videoembed/{vid}",
            ]
        last: Exception | None = None
        for attempt in attempts[:3]:
            try:
                return extract_info(attempt, format="all")
            except Exception as exc:
                last = exc
                log.info("OK.ru: попытка не удалась для %s (%s)", attempt[-24:], str(exc)[:60])
        if last is not None:
            raise last
        return extract_info(url, format="all")

    def resolve(self, url: str, translation_index: int = 0) -> Stream:  # noqa: ARG002
        info = self._extract(url)

        if info.get("is_live"):
            from ..core.errors import SourceFailedError  # noqa: PLC0415

            raise SourceFailedError(
                self.name,
                "прямые эфиры OK.ru не поддерживаются",
                fix="Дождитесь окончания трансляции: запись откроется обычной ссылкой",
                state=SourceState.PARTIAL,
            )

        qualities = self._hls_qualities(info)
        if not qualities:
            from ..core.errors import SourceFailedError  # noqa: PLC0415

            raise SourceFailedError(
                self.name,
                "OK.ru не отдал ни одной HLS-дорожки",
                fix=(
                    "У этого ролика только прямые файлы в VP9 — их браузер игры "
                    "не воспроизводит. Пришлите ссылку разработчику"
                ),
                state=SourceState.PARTIAL,
            )

        return Stream(
            source=self.name,
            title=str(info.get("title") or "").strip() or "OK.ru",
            kind=StreamKind.HLS,
            qualities=qualities,
            duration_s=int(info.get("duration") or 0),
            is_live=False,
            state=self.state,
        )


def video_id(url: str) -> str:
    """Числовой id видео из ссылки OK.ru. Пустая строка, если не разобралась."""
    match = _ID_RE.search(url or "")
    return match.group(1) if match else ""


def is_okru(url: str) -> bool:
    """Ссылка ведёт на видео OK.ru (а не на профиль или группу)."""
    return bool(video_id(url))
