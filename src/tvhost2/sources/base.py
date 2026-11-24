"""Общая база для источников: разбор через yt-dlp.

Почему одна база, а не копипаста в каждом источнике: 12 сайтов разбираются
одной библиотекой yt-dlp. Отличаются только:
  - как узнать ссылку (`matches`);
  - как собрать качества из форматов (`build_qualities`);
  - что делать, если ничего не вышло (`explain`).

Всё остальное — общее. Один баг чинится в одном месте.
"""

from __future__ import annotations

import logging
from typing import Any

import yt_dlp

from ..core.errors import (
    GeoblockedError,
    NeedsCookiesError,
    OverlayError,
    RightsBlockedError,
    SourceFailedError,
)
from ..core.models import Quality, SourceState, Stream, StreamKind, sort_qualities

log = logging.getLogger(__name__)

_BASE_OPTS: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "socket_timeout": 25,
    "retries": 2,
}


def extract_info(url: str, **extra: Any) -> dict[str, Any]:
    """Разобрать ссылку через yt-dlp.

    Сеть здесь — единственное место в приложении, где мы спрашиваем
    «что лежит по этой ссылке». Ошибки переводим в человеческие сразу,
    чтобы интерфейс не показывал трейсбек.
    """
    opts = {**_BASE_OPTS, **extra}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise _translate_download_error(str(exc)) from exc
    if not info:
        raise SourceFailedError("yt-dlp", "сайт вернул пустой ответ")
    return info


def _content_error(low: str) -> OverlayError | None:
    """Ошибки про само видео: удалено, приватное, гео."""
    if "not available in your country" in low or ("geo" in low and "restrict" in low):
        return GeoblockedError("сайт")
    if "private" in low or "removed" in low or "unavailable" in low:
        return SourceFailedError(
            "сайт",
            "видео удалено, приватное или недоступно",
            fix="Проверьте ссылку в браузере: если видео нет там, его нет и здесь",
            state=SourceState.PARTIAL,
        )
    return None


def _translate_download_error(text: str) -> OverlayError:
    """Текст yt-dlp → понятная ошибка.

    yt-dlp пишет «The web client only works when logged-in» — пользователю
    это ни о чём не говорит. Переводим на человеческий.
    """
    low = text.lower()
    early = _content_error(low)
    if early is not None:
        return early
    if "blocking_rule" in low or "blocking rule" in low:
        # Rutube: правообладатель ограничил показ за пределами своей страны.
        return RightsBlockedError("Rutube", "обычно ограничено РФ")
    if "logged-in" in low or "log in" in low or "cookies" in low or "sign in" in low:
        return NeedsCookiesError("сайт")
    if "404" in low and "rutube" in low:
        return RightsBlockedError("Rutube", "чаще всего ограничено РФ")
    if "unsupported url" in low:
        return SourceFailedError(
            "сайт",
            "этот сайт пока не поддерживается",
            fix="Пришлите ссылку разработчику — добавлю источник",
        )
    return SourceFailedError("сайт", text.split("\n", 1)[0][:200])


def pick_hls_qualities(formats: list[dict[str, Any]]) -> tuple[Quality, ...]:
    """Собрать качества из форматов yt-dlp, где каждое качество — HLS.

    Берём только дорожки с видео и реальным height. Битрейт — настоящий
    `tbr`. Если tbr нет, оцениваем по площади кадра, но помечаем честно:
    такая оценка — догадка, и она может стоить нам ABR (ERR-067).
    """
    found: list[Quality] = []
    for fmt in formats:
        protocol = fmt.get("protocol") or ""
        if "m3u8" not in protocol:
            continue
        if (fmt.get("vcodec") or "none") == "none":
            continue  # аудио-дорожка
        height = int(fmt.get("height") or 0)
        url = fmt.get("url") or ""
        if not height or not url:
            continue
        width = int(fmt.get("width") or 0) or round(height * 16 / 9)
        bitrate = int(fmt.get("tbr") or 0)
        if bitrate <= 0:
            # Оценка: ~0.1 бит/пиксель на кадр — нижняя граница для H.264.
            bitrate = max(1, round(width * height * 30 / 10_000))
        found.append(Quality(height=height, width=width, bitrate_kbps=bitrate, url=url))
    return sort_qualities(found)


def pick_direct_formats(formats: list[dict[str, Any]]) -> tuple[Quality, ...]:
    """Качества из прямых mp4/вебмуксов (OK.ru, Archive и т.п.)."""
    found: list[Quality] = []
    for fmt in formats:
        if (fmt.get("vcodec") or "none") == "none":
            continue
        if (fmt.get("acodec") or "none") == "none":
            continue  # нужен файл со звуком внутри
        height = int(fmt.get("height") or 0)
        url = fmt.get("url") or ""
        if not height or not url:
            continue
        width = int(fmt.get("width") or 0) or round(height * 16 / 9)
        bitrate = int(fmt.get("tbr") or 0) or max(1, round(width * height * 30 / 10_000))
        found.append(Quality(height=height, width=width, bitrate_kbps=bitrate, url=url))
    return sort_qualities(found)


class YtDlpSource:
    """Базовый источник поверх yt-dlp.

    Наследник задаёт только `name`, `hosts`, `state`, `note` — и при
    необходимости переопределяет `build_qualities` или `kind`.
    """

    name: str = "источник"
    hosts: tuple[str, ...] = ()
    state: SourceState = SourceState.OK
    note: str = ""
    kind: StreamKind = StreamKind.HLS

    def matches(self, url: str) -> bool:
        low = (url or "").lower()
        return any(host in low for host in self.hosts)

    def extractor_opts(self) -> dict[str, Any]:
        """Особые настройки yt-dlp для этого сайта."""
        return {}

    def build_qualities(self, info: dict[str, Any]) -> tuple[Quality, ...]:
        return pick_hls_qualities(info.get("formats") or [])

    def resolve(self, url: str, translation_index: int = 0) -> Stream:  # noqa: ARG002
        """Ссылка → поток. Ошибки — уже человеческие (см. base.extract_info).

        `translation_index` в контракте ради Kodik: у него каждая озвучка —
        отдельный поток. Остальным сайтам выбирать не из чего, параметр
        молча игнорируется — но сигнатура у всех одна, и mypy может
        проверить, что источник выполняет контракт.
        """
        info = extract_info(url, **self.extractor_opts())
        qualities = self.build_qualities(info)
        title = str(info.get("title") or "").strip() or "Без названия"
        duration = int(info.get("duration") or 0)
        is_live = bool(info.get("is_live"))

        if not qualities:
            raise SourceFailedError(
                self.name,
                "видео найдено, но ни одного качества с видео",
                fix=(
                    "Скорее всего сайт отдаёт поток только в браузере — "
                    "пришлите ссылку разработчику"
                ),
                state=SourceState.BROWSER_ONLY,
            )

        return Stream(
            source=self.name,
            title=title,
            kind=self.kind,
            qualities=qualities,
            duration_s=duration,
            is_live=is_live,
            state=self.state,
        )
