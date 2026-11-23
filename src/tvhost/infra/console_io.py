"""
console_io.py — защита от UnicodeEncodeError на Windows.

Проблема: на русской Windows консоль и перенаправленный вывод работают в cp1251,
где нет эмодзи (🎬 ✅ ⚠️) и некоторых символов. Любой print() с ними падает:

    UnicodeEncodeError: 'charmap' codec can't encode character '\\U0001f3ac'

Решение:
1. переключить stdout/stderr в UTF-8 там, где это возможно;
2. при невозможности — заменить неподдерживаемые символы на '?', чтобы
   программа работала, а не падала (важнее вывести лог без эмодзи, чем упасть).

Использование:
    from tvhost.infra import console_io  # достаточно один раз
"""

from __future__ import annotations

import io
import sys


def _reconfigure_utf8(stream) -> None:
    """Попытаться перевести поток в UTF-8 (Python 3.7+ умеет reconfigure)."""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        return
    except (AttributeError, ValueError, OSError):
        pass
    # поток может быть уже обёрнут с errors='strict' — пересоздаём с errors='replace'
    try:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            return
    except (AttributeError, ValueError, OSError):
        pass
    # fallback для случаев, когда reconfigure недоступен
    try:
        if hasattr(stream, "buffer"):
            wrapper = io.TextIOWrapper(
                stream.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
            if stream is sys.stdout:
                sys.stdout = wrapper
            elif stream is sys.stderr:
                sys.stderr = wrapper
    except (AttributeError, ValueError, OSError):
        pass


def make_safe() -> None:
    """Сделать stdout/stderr безопасными для любых символов."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            _reconfigure_utf8(stream)
    _fix_argv()


def _fix_argv() -> None:
    """
    Русские буквы в аргументах командной строки.

    На Windows аргументы приходят через ANSI-кодировку системы (cp1251),
    и путь с русскими буквами может превратиться в кракозябры. Если
    получается восстановить — восстанавливаем.
    """
    if sys.platform != "win32":
        return
    try:
        fixed = []
        for arg in sys.argv:
            if not isinstance(arg, str):
                fixed.append(arg)
                continue
            try:
                # если в строке есть «битые» символы замены или она не читается —
                # пробуем перечитать исходные байты как cp1251 -> utf-8
                if "\ufffd" in arg:
                    raise ValueError
                fixed.append(arg)
            except (ValueError, UnicodeError):
                try:
                    fixed.append(arg.encode("cp1251", "replace").decode("utf-8", "replace"))
                except (UnicodeError, LookupError):
                    fixed.append(arg)
        if fixed != sys.argv:
            sys.argv = fixed
    except Exception:
        pass


def safe_print(*args, **kwargs) -> None:
    """print(), который никогда не падает из-за кодировки."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        cleaned = text.encode(enc, errors="replace").decode(enc, errors="replace")
        print(cleaned, **kwargs)


# применяем сразу при импорте — это самый надёжный вариант
make_safe()


if __name__ == "__main__":
    # самопроверка: печатаем эмодзи и кириллицу
    print("🎬 ✅ ⚠️ ❌ 📦 ⓘ 💡 — тест эмодзи")
    print("Кириллица: проверка связи")
    print(f"stdout encoding: {getattr(sys.stdout, 'encoding', '?')}")
    print("Если вы это видите и код возврата 0 — всё в порядке.")
