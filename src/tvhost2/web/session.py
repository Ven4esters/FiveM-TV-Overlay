"""Сессии просмотра — состояние одного открытого видео.

Зачем отдельный модуль: в v1 сессия была набором полей прямо в обработчике
HTTP, и любой новый источник требовал правки этого обработчика. Здесь
сессия — самостоятельная структура, а прокси только раскладывает по ней
готовый `Stream` из источника.

Сессия обязана пережить нажатие «Повторить» и переключение качества, поэтому
в ней хранится всё, что нужно для повторного запроса к источнику:
  - исходная ссылка (страница), не поток;
  - выбранная озвучка и качество;
  - разрешённый `Stream` с временными ссылками CDN.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .models import Stream

SESSION_TTL_S = 6 * 3600
"""Сессия живёт 6 часов: дольше — не смотрят один выпуск, а ссылки
CDN всё равно протухают раньше."""


@dataclass
class Session:
    """Одна открытая ссылка и всё, что про неё известно."""

    sid: str
    url: str
    """Ссылка, которую вставил человек. Не поток — именно страница."""

    title: str = ""

    stream: Stream | None = None
    """Разрешённый поток. None — ещё не разбирали или разбор упал."""

    source_name: str = ""
    """Имя источника, который взялся за ссылку («Kodik», «Rutube»)."""

    translation_index: int = 0
    """Выбранная озвучка (индекс в stream.audio_tracks)."""

    quality_height: int = 0
    """Выбранное качество. 0 — «Авто»: плеер решает сам по битрейту."""

    volume: float = 1.0
    muted: bool = False
    normalize: bool = False
    """Выравнивание громкости. Усиление считает модуль audio.loudness."""

    error: str = ""
    error_hint: str = ""

    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_seen = time.time()

    @property
    def expired(self) -> bool:
        return time.time() - self.last_seen > SESSION_TTL_S

    def state(self) -> dict[str, Any]:
        """Состояние для пульта в оверлее.

        Ключи совпадают с тем, что ждёт интерфейс: connected, playing и так
        далее. Играет ли видео на самом деле, знает только плеер в игре —
        поэтому здесь connected=True это «плеер открыл сессию», а не «звук
        идёт».
        """
        return {
            "sid": self.sid,
            "title": self.title,
            "source": self.source_name,
            "connected": True,
            "error": self.error,
            "errorHint": self.error_hint,
            "volume": self.volume,
            "muted": self.muted,
            "normalize": self.normalize,
            "translation": self.translation_title,
            "qualities": [q.label for q in self.stream.qualities] if self.stream else [],
            "translations": [
                t.get("title", "") for t in (self.stream.audio_tracks if self.stream else ())
            ],
            "duration": self.stream.duration_s if self.stream else 0,
            # Прямой эфир: у пульта пропадает перемотка и общий таймер —
            # у live нет ни длительности, ни позиции в привычном смысле.
            "isLive": bool(self.stream.is_live) if self.stream else False,
            # Субтитры: пульт рисует кнопку «Субтитры» активной, только если
            # дорожки есть. Пустой список — кнопка честно скажет «нет».
            "subtitles": [
                str(t.get("title") or t.get("lang") or "")
                for t in (self.stream.subtitles if self.stream else ())
            ],
        }

    @property
    def translation_title(self) -> str:
        if not self.stream or not self.stream.audio_tracks:
            return ""
        tracks = self.stream.audio_tracks
        if 0 <= self.translation_index < len(tracks):
            return str(tracks[self.translation_index].get("title", ""))
        return ""


class SessionStore:
    """Хранилище сессий. Потокобезопасно: прокси многопоточный.

    Почему dict под замком, а не база: сессий единицы, живут часы, при
    перезапуске приложения они бесполезны (ссылки CDN протухли).
    """

    def __init__(self) -> None:
        self._items: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, url: str, title: str = "") -> Session:
        """Новая сессия. sid короткий: он попадает в ссылку для игры."""
        sid = secrets.token_urlsafe(6)
        session = Session(sid=sid, url=url, title=title or url[:80])
        with self._lock:
            self._purge_locked()
            self._items[sid] = session
        return session

    def get(self, sid: str) -> Session | None:
        with self._lock:
            session = self._items.get(sid)
            if session is None:
                return None
            if session.expired:
                del self._items[sid]
                return None
            session.touch()
            return session

    def all(self) -> list[Session]:
        with self._lock:
            self._purge_locked()
            return sorted(self._items.values(), key=lambda s: s.last_seen, reverse=True)

    def drop(self, sid: str) -> bool:
        with self._lock:
            return self._items.pop(sid, None) is not None

    def _purge_locked(self) -> None:
        dead = [sid for sid, s in self._items.items() if s.expired]
        for sid in dead:
            del self._items[sid]


STORE = SessionStore()
"""Один экземпляр на процесс: прокси и оверлей живут в разных потоках,
но в одном процессе."""
