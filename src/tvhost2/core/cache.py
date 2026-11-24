"""Ограниченные кэши — один помощник вместо четырёх копий.

Зачем отдельный модуль: к 24.11.2026 в проекте было четыре самодельных
кэша на обычном `dict`, и у трёх не было верхнего предела. Замерено:
25 000 сессий — это +10.6 МБ, и они живут все шесть часов, потому что TTL
проверялся только при чтении ключа, а к ключам, к которым не вернулись,
никто не читает. То же самое ждало плейлисты CVH (по 615 КБ каждый) и
таблицы эмбедов встройки.

Правило простое: у любого словаря, который живёт дольше одного запроса,
обязан быть предел. Здесь он один на всех, и поведение одинаковое:
вытесняется то, что дольше всего не трогали.

Почему не `functools.lru_cache`: его нельзя очистить по времени и он не
умеет потокобезопасного обновления значения на месте. Почему не
`OrderedDict`: обычного dict достаточно, порядок ключей и так по вставке,
а перестановка при чтении — лишняя работа для нашего объёма.
"""

from __future__ import annotations

import threading
import time
from typing import Generic, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    """Словарь с пределом по числу и сроком жизни записи.

    Оба предела нужны: TTL ловит «запись старая», а предел числа — «записей
    слишком много и они свежие». Без второго счётчик растёт безгранично,
    потому что новая запись обновляет время только у себя.

    Потокобезопасен: прокси многопоточный, кэши общие на процесс.
    """

    def __init__(self, limit: int, ttl_s: float) -> None:
        if limit <= 0:
            raise ValueError("предел должен быть больше нуля")
        self._items: dict[str, tuple[float, V]] = {}
        self._lock = threading.Lock()
        self._limit = limit
        self._ttl_s = ttl_s

    def get(self, key: str) -> V | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            at, value = item
            if self._ttl_s > 0 and time.time() - at > self._ttl_s:
                del self._items[key]
                return None
            return value

    def put(self, key: str, value: V) -> None:
        with self._lock:
            self._items[key] = (time.time(), value)
            self._trim_locked()

    def _trim_locked(self) -> None:
        extra = len(self._items) - self._limit
        if extra <= 0:
            return
        oldest = sorted(self._items.items(), key=lambda kv: kv[1][0])
        for stale, _value in oldest[:extra]:
            del self._items[stale]

    def clear(self) -> None:
        """Только для тестов и диагностики."""
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class SizeCache:
    """Размеры кадра по файлам: имя поля + id файла из адреса.

    Отдельный класс, потому что у этих записей нет срока жизни: разрешение
    файла не меняется, и перечитывать его через десять минут незачем.
    Предел по числу — потому что сезонов и озвучек бесконечно много.

    Ключ — id файла из адреса, а не полный адрес: подпись в нём меняется
    каждые несколько минут, а id нет.
    """

    def __init__(self, limit: int = 256) -> None:
        self._items: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._limit = limit

    def get(self, key: str) -> tuple[int, int] | None:
        with self._lock:
            return self._items.get(key)

    def put(self, key: str, size: tuple[int, int]) -> None:
        with self._lock:
            self._items[key] = size
            extra = len(self._items) - self._limit
            if extra > 0:
                for stale in list(self._items)[:extra]:
                    del self._items[stale]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def file_key(prefix: str, url: str) -> str:
    """Ключ файла из адреса: `/id/12345/` важнее подписи вокруг него."""
    import re  # noqa: PLC0415 — нужен только здесь

    match = re.search(r"/id/(\d+)/", url or "")
    file_id = match.group(1) if match else (url or "").split("?", maxsplit=1)[0]
    return f"{prefix}:{file_id}"


__all__ = ["SizeCache", "TTLCache", "file_key"]
