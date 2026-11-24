"""Веб-слой: сессии, HLS-мост, страница плеера, HTTP-сервер.

Порядок зависимостей: session (состояние) → hls (переписывание плейлистов)
→ player (страница для игры) → server (маршруты). Каждый модуль знает
только предыдущие.
"""

from .session import STORE, Session, SessionStore

__all__ = ["STORE", "Session", "SessionStore"]
