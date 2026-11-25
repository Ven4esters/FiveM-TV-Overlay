"""Подготовка процесса к запуску: потоки, перехват падений, отметка старта.

Зачем отдельный модуль без Qt: это самый ранний этап жизни программы. Он
обязан работать, даже если Qt не загрузился, — иначе диагностика исчезает
именно тогда, когда нужна. Здесь нет ни одного импорта PySide6, поэтому
модуль можно проверить тестом на головой машине.

Разбираемый случай (реальный отчёт с собранного .exe):

    send_response() → log_request() → log_message() → sys.stderr.write(...)

В сборке PyInstaller с `console=False` консоли нет, `sys.stderr` равен
`None`, и `sys.stderr.write` даёт AttributeError ВНУТРИ `send_response` —
до отправки заголовков. Пульт видел «порт слушает, ответа нет» и писал
«прокси: выключен» при живом прокси.
"""

from __future__ import annotations

import io
import sys
import traceback
from types import TracebackType


def ensure_streams() -> list[str]:
    """Подставить заглушки вместо отсутствующих stdout/stderr.

    Возвращает имена потоков, которые пришлось подменить (для журнала).

    Заглушка не притворяется консолью: она копит строки и никуда их не
    выводит. Настоящий журнал ведёт `diagnostics`, и он пишет в файл, а не
    в поток, поэтому ничего не теряется.
    """
    replaced: list[str] = []
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, io.StringIO())
            replaced.append(name)
    return replaced


def install_crash_log() -> None:
    """Писать необработанные исключения в журнал перед обычной обработкой.

    В сборке `console=False` трейсбек печатать некуда: PyInstaller иногда
    показывает модальное окно, которое человек закрывает не читая. Итог —
    «программа просто закрылась». Обработчик сохраняет последние строки
    трейсбека в файл журнала и только затем отдаёт управление стандартному,
    поэтому поведение не меняется — появляется лишь след.
    """

    def hook(kind: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
        try:
            from . import diagnostics  # noqa: PLC0415 — журнал не должен ломать старт

            text = "".join(traceback.format_exception(kind, value, tb))
            for line in text.strip().splitlines()[-15:]:
                diagnostics.log("err", f"падение: {line}")
            diagnostics.log("err", f"журнал: {diagnostics.JOURNAL.path}")
        except Exception:
            sys.__excepthook__(kind, value, tb)
            return
        sys.__excepthook__(kind, value, tb)

    sys.excepthook = hook


def mark_startup(description: str) -> None:
    """Отметка запуска и строка о сборке — до создания окна.

    Без неё при падении на старте в журнале не остаётся даже факта запуска:
    человек видит пустой файл и не знает, доходило ли дело до программы.
    """
    try:
        from . import diagnostics  # noqa: PLC0415

        diagnostics.mark_start()
        diagnostics.log("boot", description)
    except Exception:
        return


__all__ = ["ensure_streams", "install_crash_log", "mark_startup"]
