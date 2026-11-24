"""Резолвер: ссылка от человека → готовый `Stream`.

Один вход для всей логики «какой источник возьмётся и что вернёт». В v1 это
было размазано по `_prepare_kodik`, `_prepare_vk`, `_prepare_twitch` — по
методу на источник, и добавление сайта требовало правки прокси.

Здесь прокси вообще не знает про источники: он спрашивает у резолвера.
Новый сайт = новый модуль в `sources/` + регистрация, прокси не меняется.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass

from ..core import registry
from ..core.errors import OverlayError
from ..core.models import Stream, StreamKind
from ..sources import register_all
from .fetch import FetchError

log = logging.getLogger(__name__)


@dataclass
class Resolved:
    """Результат разбора: поток и имя источника."""

    stream: Stream
    source_name: str

    @property
    def needs_browser(self) -> bool:
        """Нужен ли браузер для воспроизведения (PAGE-поток)."""
        return self.stream.kind is StreamKind.PAGE

    @property
    def has_own_master(self) -> bool:
        """Источник отдаёт один плейлист со всеми качествами внутри.

        Тогда мост просто переписывает ссылки. Если False — мост собирает
        master сам из отдельных качеств (Kodik, Rutube, VK).
        """
        return len(self.stream.qualities) <= 1 and not self.stream.audio_url


class ResolveError(Exception):
    """Разбор не удался. Текст — для человека, не для логов."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def ensure_registered() -> None:
    """Зарегистрировать источники один раз. Повторный вызов безопасен."""
    register_all()


def find_source(url: str):
    """Кто возьмётся за ссылку. None — ни один источник не узнал адрес."""
    ensure_registered()
    return registry.find(url)


def resolve(url: str, translation_index: int | None = None) -> Resolved:
    """Разобрать ссылку в поток.

    translation_index — какая озвучка нужна (Kodik и подобные сайты, где
    каждая озвучка это отдельный поток). None — пусть источник выберет сам.

    Ошибки источников (`OverlayError`) и сети (`FetchError`) переводятся в
    `ResolveError` с понятным текстом: интерфейс показывает его человеку,
    а не трейсбек.
    """
    ensure_registered()
    source = registry.find(url)
    if source is None:
        supported = ", ".join(registry.names())
        raise ResolveError(
            f"сайт не поддерживается: {urllib.parse.urlsplit(url).netloc or url[:40]}",
            f"Поддерживаются: {supported}.\n"
            "Если это прямая ссылка на .m3u8 или .mp4 — вставьте её целиком, "
            "с параметрами после знака вопроса.",
        )
    try:
        if translation_index is None:
            stream = source.resolve(url)
        else:
            stream = source.resolve(url, translation_index=translation_index)
    except TypeError:
        # источник не умеет выбор озвучки — это нормально, у большинства
        # сайтов дорожка одна
        stream = source.resolve(url)
    except OverlayError as exc:
        raise ResolveError(str(exc), _hint_for(exc)) from exc
    except FetchError as exc:
        raise ResolveError(
            exc.reason, "Источник не ответил. Проверьте ссылку и попробуйте ещё раз."
        ) from exc
    except Exception as exc:  # источник не должен ронять прокси
        log.exception("источник %s упал на %s", source.name, url[:80])
        raise ResolveError(
            f"источник не смог разобрать ссылку ({type(exc).__name__})",
            "Попробуйте другую ссылку или откройте видео на сайте, "
            "чтобы проверить, работает ли оно там.",
        ) from exc
    return Resolved(stream=stream, source_name=source.name)


def _hint_for(error: OverlayError) -> str:
    """Подсказка под тип ошибки источника.

    Показывается в панели сообщений: человек должен понимать, что делать,
    а не читать код.
    """
    name = type(error).__name__
    if name == "RightsBlockedError":
        return "Правообладатель закрыл видео. Выберите другое."
    if name == "GeoblockedError":
        return "Видео недоступно в вашем регионе."
    if name == "NeedsCookiesError":
        return "Сайт требует входа в аккаунт — без cookies браузера поток не отдаётся."
    return "Источник отказал. Попробуйте другую ссылку."
