"""Ошибки — с человеческим лицом.

В v1 ошибка была строкой «Ошибка воспроизведения». Заказчик не мог понять,
что делать. Здесь у каждой ошибки есть:
  what — что случилось (для журнала);
  why  — почему (для человека);
  fix  — что сделать (конкретный шаг).

Правило: ни одно сообщение об ошибке не заканчивается ничего не значащим
«попробуйте позже».
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import SourceState


@dataclass
class OverlayError(Exception):
    """Базовая ошибка приложения. Всегда знает, что делать пользователю."""

    what: str
    """Техническое: что именно сломалось."""

    why: str = ""
    """Объяснение простыми словами."""

    fix: str = ""
    """Что сделать — по шагам."""

    state: SourceState = SourceState.BROKEN
    """Как это отразится на статусе источника во вкладке «Источники»."""

    detail: dict[str, object] = field(default_factory=dict)
    """Подробности для журнала."""

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.what}: {self.why}".strip(": "))

    def user_text(self) -> str:
        """Текст для показа в интерфейсе — без технического мусора."""
        parts = [self.what]
        if self.why:
            parts.append(self.why)
        if self.fix:
            parts.append(f"Что сделать: {self.fix}")
        return "\n".join(parts)


class LinkNotRecognisedError(OverlayError):
    """Ссылка не подошла ни одному источнику."""

    def __init__(self, url: str) -> None:
        super().__init__(
            what="Ссылка не распознана",
            why=f"Ни один источник не умеет открывать {url[:60]}",
            fix="Проверьте, что это ссылка на видео, а не на главную страницу сайта",
            state=SourceState.OK,
        )


class SourceFailedError(OverlayError):
    """Источник узнал ссылку, но не смог отдать поток."""

    def __init__(
        self,
        source: str,
        reason: str,
        fix: str = "",
        state: SourceState = SourceState.BROKEN,
    ) -> None:
        super().__init__(
            what=f"{source}: не удалось получить видео",
            why=reason,
            fix=fix or "Пришлите эту ссылку разработчику — разберусь",
            state=state,
        )


class NeedsCookiesError(SourceFailedError):
    """Сайт требует вход в аккаунт."""

    def __init__(self, source: str) -> None:
        super().__init__(
            source,
            reason="Сайт отдаёт видео только вошедшим в аккаунт",
            fix="Экспортируйте cookies из браузера и укажите файл в настройках",
            state=SourceState.NEEDS_COOKIES,
        )


class GeoblockedError(SourceFailedError):
    """Видео недоступно из вашей страны."""

    def __init__(self, source: str) -> None:
        super().__init__(
            source,
            reason="Видео недоступно из вашего региона",
            fix="Такую ссылку открыть нельзя — выберите другое видео",
            state=SourceState.PARTIAL,
        )


class LiveEndedError(SourceFailedError):
    """Стрим закончился."""

    def __init__(self, source: str) -> None:
        super().__init__(
            source,
            reason="Прямой эфир закончился",
            fix="Записи стрима обычно доступны на том же канале через несколько минут",
            state=SourceState.OK,
        )


class NoQualitiesError(SourceFailedError):
    """Источник отдал видео без единого качества."""

    def __init__(self, source: str) -> None:
        super().__init__(
            source,
            reason="Видео найдено, но доступных качеств нет",
            fix="Попробуйте другую ссылку или проверьте её в браузере",
            state=SourceState.PARTIAL,
        )


class RightsBlockedError(SourceFailedError):
    """Правообладатель запретил показ этого видео в некоторых странах.

    Реальные случаи (проверены 23.11.2026):
      - «Супермен (2025)»    — доступен только в РФ;
      - «Гарри Поттер (2001)» — доступен всего в 32 странах из ~250.

    Rutube отдаёт в метаданных точный список стран, поэтому мы не гадаем:
    если код возврата 2 (blocked) и известна страна пользователя —
    показываем конкретику, а не «попробуйте позже».

    Источник при этом НЕ помечается «сломан»: он жив, недоступно
    конкретное видео с конкретного IP.
    """

    def __init__(self, source: str, region_hint: str = "", detail_text: str = "") -> None:
        reason = "правообладатель ограничил показ этого видео"
        if detail_text:
            reason = detail_text
        elif region_hint:
            reason += f" ({region_hint})"
        super().__init__(
            source,
            reason=reason,
            fix=(
                "Если видео открывается у вас в браузере — оно откроется и здесь. "
                "Ограничение действует только для чужих стран и VPN"
            ),
            state=SourceState.OK,
        )
