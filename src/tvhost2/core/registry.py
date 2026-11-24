"""Реестр источников — единственное место, где источники перечислены.

Зачем: в v1 список источников был размазан по `if "rutube" in url` в трёх
файлах. Добавить источник = вспомнить все места. Здесь:

    registry.register(RutubeSource())

и всё. Прокси, интерфейс и вкладка «Источники» узнают о новом модуле сами.

Порядок регистрации = порядок проверки. Специфичные источники регистрируем
раньше общих: ссылка на `vkvideo.ru` не должна уходить в «прямой HLS».
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from .models import SourceState, Stream

log = logging.getLogger(__name__)


@runtime_checkable
class Source(Protocol):
    """Контракт источника. Один класс = один сайт."""

    name: str
    """Человеческое имя: «Rutube», «Kodik»."""

    state: SourceState
    """Честное состояние — показывается во вкладке «Источники»."""

    note: str
    """Короткая оговорка для интерфейса: «до 720p без входа»."""

    def matches(self, url: str) -> bool:
        """Умеет ли этот источник открывать такую ссылку."""

    def resolve(self, url: str, translation_index: int = 0) -> Stream:
        """Ссылка → поток. Бросает OverlayError с понятным текстом.

        `translation_index` — какая озвучка нужна. У большинства сайтов
        дорожка одна, и параметр просто игнорируется; реально его понимает
        только Kodik (там каждая озвучка — отдельный поток). В контракте
        он есть, потому что вызывающий код (resolver) передаёт его всем:
        иначе mypy справедливо ругался на неожиданный аргумент.
        """


class Registry:
    """Список источников с поиском по ссылке."""

    def __init__(self) -> None:
        self._sources: list[Source] = []

    def register(self, source: Source) -> None:
        """Добавить источник в конец цепочки."""
        if not getattr(source, "name", ""):
            raise ValueError("у источника должно быть имя")
        if any(s.name == source.name for s in self._sources):
            raise ValueError(f"источник {source.name!r} уже зарегистрирован")
        self._sources.append(source)

    def find(self, url: str) -> Source | None:
        """Первый источник, который узнал ссылку."""
        for source in self._sources:
            try:
                if source.matches(url):
                    return source
            except Exception:
                # Кривой matches не должен ронять поиск: пропускаем источник.
                log.warning("источник %s упал в matches()", getattr(source, "name", "?"))
                continue
        return None

    def __iter__(self) -> Iterator[Source]:
        return iter(self._sources)

    def __len__(self) -> int:
        return len(self._sources)

    def names(self) -> list[str]:
        return [s.name for s in self._sources]

    def status_table(self) -> list[dict[str, str]]:
        """Данные для вкладки «Источники»: имя, состояние, оговорка."""
        return [{"name": s.name, "state": s.state.value, "note": s.note} for s in self._sources]


registry = Registry()
"""Глобальный реестр. Модули источников регистрируются в `sources/__init__.py`."""
