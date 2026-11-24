"""История просмотров: что открывали за эту сессию работы.

Зачем в памяти, а не на диске: заказчик просил «в рамках одной сессии».
Плюс к тому ссылки на видео живут часами — сохранённая вчера история
сегодня открыла бы мёртвый поток и ошибку вместо видео. Честнее забыть.

Пять записей, самые свежие сверху. Пятая при добавлении шестой уходит —
это не кольцевой буфер по кругу, а именно «пять последних».

Что запоминается: ссылка (её вставил человек), название, имя источника,
и выбранная озвучка. Серия и качество не запоминаются: переключение
озвучки у нас — повторный разбор, и достаточно восстановить индекс.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

MINUTES_IN_HOUR = 60

LIMIT = 5
"""Сколько последних видео помним. Больше в пульте не помещается —
список и так стоит под блоком открытия ссылки."""


@dataclass
class WatchRecord:
    """Одно просмотренное видео."""

    url: str
    title: str = ""
    source: str = ""
    translation_index: int = 0
    translation: str = ""
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, object]:
        """Для пульта: без служебного времени, но с подписью «когда»."""
        return {
            "url": self.url,
            "title": self.title or self.url,
            "source": self.source,
            "translationIndex": self.translation_index,
            "translation": self.translation,
            "when": _ago(self.at),
        }


class WatchHistory:
    """Последние просмотры. Потокобезопасно: пульт и прокси — разные потоки."""

    def __init__(self, limit: int = LIMIT) -> None:
        self.limit = limit
        self._items: list[WatchRecord] = []
        self._lock = threading.Lock()

    def remember(
        self,
        url: str,
        title: str = "",
        source: str = "",
        translation_index: int = 0,
        translation: str = "",
    ) -> None:
        """Запомнить просмотр.

        Повторное открытие ТОГО ЖЕ видео не создаёт вторую запись, а
        поднимает существующую наверх: иначе пять слотов съест одно видео,
        открытое пять раз, и вернуться к предыдущему будет нечем.
        Ключ — ссылка плюс озвучка: «Ван-Пис» в дубляже и в субтитрах это
        два разных просмотра, и вернуться хочется к каждому.
        """
        if not url:
            return
        with self._lock:
            self._items = [
                item
                for item in self._items
                if not (item.url == url and item.translation_index == translation_index)
            ]
            self._items.insert(
                0,
                WatchRecord(
                    url=url,
                    title=title or url,
                    source=source,
                    translation_index=translation_index,
                    translation=translation,
                ),
            )
            del self._items[self.limit :]

    def forget(self, url: str) -> bool:
        """Убрать видео из истории (кнопка «забыть»). True — если было."""
        with self._lock:
            before = len(self._items)
            self._items = [item for item in self._items if item.url != url]
            return len(self._items) != before

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def all(self) -> list[WatchRecord]:
        """Копия списка: наружу не отдаём внутренний список под замком."""
        with self._lock:
            return list(self._items)

    def as_list(self) -> list[dict[str, object]]:
        return [item.as_dict() for item in self.all()]

    def get(self, url: str) -> WatchRecord | None:
        """Запись по ссылке — нужна, чтобы вернуть и озвучку."""
        with self._lock:
            for item in self._items:
                if item.url == url:
                    return item
        return None


def _ago(at: float) -> str:
    """Подпись «3 минуты назад». Без дат: сессия не живёт сутками."""
    minutes = max(0, int((time.time() - at) / 60))
    if minutes < 1:
        return "только что"
    if minutes == 1:
        return "минуту назад"
    if minutes < MINUTES_IN_HOUR:
        return f"{minutes} мин назад"
    hours = minutes // MINUTES_IN_HOUR
    return f"{hours} ч назад"


HISTORY = WatchHistory()
"""Один экземпляр на процесс, как и STORE сессий."""
