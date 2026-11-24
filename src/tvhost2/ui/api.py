"""Клиент прокси для интерфейса.

Почему отдельный слой: интерфейс не должен знать, что прокси — это HTTP.
Все запросы идут через один класс, поэтому:
  - адрес прокси меняется в одном месте;
  - ошибки сети превращаются в человеческий текст один раз;
  - тесты интерфейса подменяют клиента, а не сеть.

Важно: НИКАКОЙ логики разбора ссылок здесь нет — только транспорт.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class ApiResult:
    """Ответ прокси: код, данные и текст ошибки, если она была."""

    ok: bool
    status: int = 0
    data: dict[str, Any] | None = None
    error: str = ""

    @property
    def hint(self) -> str:
        """Подсказка от прокси — готова для показа человеку."""
        return str((self.data or {}).get("hint") or "")

    @property
    def reason(self) -> str:
        """Причина отказа — без технического шума."""
        data = self.data or {}
        return str(data.get("reason") or data.get("error") or self.error or "неизвестная ошибка")


class ApiError(Exception):
    """Прокси не ответил совсем (не запущен, порт занят, таймаут)."""


class Api:
    """Клиент прокси. Один экземпляр на окно."""

    def __init__(self, base: str = "http://127.0.0.1:9001", timeout: int = 60) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    # ---------------- транспорт ----------------
    def _request(
        self,
        path: str,
        params: dict[str, str] | None = None,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> ApiResult:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                return ApiResult(ok=True, status=response.status, data=_json_or_none(raw))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return ApiResult(
                ok=False,
                status=exc.code,
                data=_json_or_none(raw),
                error=f"HTTP {exc.code}",
            )
        except urllib.error.URLError as exc:
            raise ApiError(f"прокси не отвечает: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ApiError("прокси не ответил вовремя") from exc
        except OSError as exc:
            raise ApiError(f"сеть недоступна: {exc.strerror or exc}") from exc

    # ---------------- методы ----------------
    def alive(self) -> bool:
        """Прокси запущен? Проверка дешёвая, вызывается при старте окна."""
        try:
            return self._request("/", timeout=3).ok
        except ApiError:
            return False

    def create_session(self, url: str, title: str = "") -> ApiResult:
        """Открыть ссылку: прокси разберёт её и вернёт адрес страницы."""
        return self._request("/new", {"url": url, "title": title}, timeout=120)

    def sessions(self) -> list[dict[str, Any]]:
        """Список активных сессий для блока «Сейчас запущено»."""
        result = self._request("/sessions", timeout=6)
        return list((result.data or {}).get("sessions") or [])

    def state(self, sid: str) -> dict[str, Any]:
        """Состояние одной сессии: позиция, пауза, громкость, ошибка."""
        result = self._request(f"/state/{sid}", timeout=6)
        return dict(result.data or {})

    def control(self, sid: str, cmd: str, value: Any = None) -> ApiResult:
        """Команда пульта: play, pause, seek, volume, quality, trans…"""
        params = {"cmd": cmd}
        if value is not None:
            params["value"] = str(value).lower() if isinstance(value, bool) else str(value)
        # POST: сервер принимает команды пульта только POST-запросом,
        # на GET отвечает 404 «нет маршрута».
        return self._request(f"/control/{sid}", params, method="POST", timeout=8)

    def loudness(self, sid: str) -> dict[str, Any]:
        """Усиление громкости для текущего потока (замер кешируется)."""
        result = self._request(f"/loudness/{sid}", timeout=120)
        return dict(result.data or {})

    def translation(self, sid: str, index: int) -> ApiResult:
        """Сменить озвучку (Kodik и подобные сайты)."""
        return self._request(f"/trans/{sid}", {"index": str(index)}, method="POST", timeout=90)

    def sources(self) -> list[dict[str, Any]]:
        """Данные вкладки «Источники»: имя, состояние, оговорка."""
        result = self._request("/sources", timeout=6)
        return list((result.data or {}).get("sources") or [])

    def journal(self, limit: int = 300) -> str:
        """Журнал с сервера, если интерфейс работает с отдельным процессом."""
        result = self._request("/diag", {"limit": str(limit)}, timeout=8)
        return str((result.data or {}).get("text") or "")


def _json_or_none(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else {"items": value}
