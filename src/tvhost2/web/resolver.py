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
from ..core.models import Stream
from ..sources import register_all
from .fetch import FetchError

log = logging.getLogger(__name__)


@dataclass
class Resolved:
    """Результат разбора: поток и имя источника."""

    stream: Stream
    source_name: str


class ResolveError(Exception):
    """Разбор не удался. Текст — для человека, не для логов."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def _ask(source, url: str, translation_index: int | None, episode: int | None) -> Stream:
    """Спросить источник — по одному набору параметров, который он понимает.

    Почему ступеньки, а не один вызов: у источников разные подписи `resolve`.
    vk/youtube/rutube принимают только ссылку; kodik — ещё озвучку; anidub.org
    — ещё серию. Пробуем от самого полного набора к самому простому и
    останавливаемся на первом, который источник принял.

    Почему `TypeError` не пробрасываем: он возникает ровно в момент вызова,
    когда подпись не совпала, — до какой-либо работы источника. Настоящие
    ошибки разбора приходят как `OverlayError` и выводятся человеку.
    """
    variants: list[dict[str, int]] = []
    if episode is not None and translation_index is not None:
        variants.append({"translation_index": translation_index, "episode": episode})
    if translation_index is not None:
        variants.append({"translation_index": translation_index})
    if episode is not None:
        variants.append({"episode": episode})
    variants.append({})
    last: TypeError | None = None
    for kwargs in variants:
        try:
            return source.resolve(url, **kwargs)
        except TypeError as exc:
            last = exc
            log.debug("резолвер: %s не принял %s (%s)", source.name, kwargs, exc)
    # Ни один вариант не подошёл — это ошибка в подписи источника, а не в
    # ссылке. Сообщаем как есть, чтобы не прятать баг за «не удалось».
    raise last or TypeError("источник не принял ни одного набора параметров")


def ensure_registered() -> None:
    """Зарегистрировать источники один раз. Повторный вызов безопасен."""
    register_all()


def resolve(
    url: str,
    translation_index: int | None = None,
    episode: int | None = None,
) -> Resolved:
    """Разобрать ссылку в поток.

    translation_index — какая озвучка нужна (Kodik и подобные сайты, где
    каждая озвучка это отдельный поток). None — пусть источник выберет сам.

    episode — номер серии. None — пусть источник решит сам (у Kodik это
    первая серия, у anidub.org — первая доступная). Параметр ОБЩИЙ: если
    источник не умеет серии, он просто не принимает его, и разбор идёт как
    обычно — молчаливой подмены не происходит.

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
        stream = _ask(source, url, translation_index, episode)
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
    # Свой `fix` источника важнее общего текста: там сказано, ЧТО именно
    # закрыто и где заработает. «Источник отказал» стирало эти подробности,
    # и человек не понимал, что дело в регионе, а не в программе.
    own = getattr(error, "fix", "")
    if own and own != "Источник отказал. Попробуйте другую ссылку.":
        return str(own)
    return "Источник отказал. Попробуйте другую ссылку."
