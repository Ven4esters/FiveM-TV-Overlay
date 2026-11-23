"""Проверка одной ссылки на ВАШЕМ компьютере.

Зачем этот файл: часть видео я проверить не могу — правообладатель пускает
только из России, а я работаю из-за границы. Этот скрипт запускаете вы,
на своей машине, и он скажет правду: откроется у вас видео или нет.

Запуск (Windows, в папке проекта):

    py -m tests.manual.check_link https://rutube.ru/video/xxxx/

Или просто двойным кликом по `check_link.bat`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Чтобы скрипт работал и как `py tests/manual/check_link.py`, и через -m.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tvhost2.core import OverlayError, registry  # noqa: E402
from tvhost2.sources import register_all  # noqa: E402


def check(url: str) -> int:
    """Проверить ссылку. Возвращает код выхода: 0 — работает, 1 — нет."""
    register_all()
    source = registry.find(url)
    print(f"Ссылка:  {url}")
    if source is None:
        print("Источник: не найден")
        print()
        print("Ни один известный источник не умеет открывать эту ссылку.")
        print("Если в браузере она открывается — пришлите её разработчику.")
        return 1

    print(f"Источник: {source.name} ({source.state.value})")
    print("Проверяю, отдаёт ли сайт видео с вашего адреса…")
    print()

    try:
        stream = source.resolve(url)
    except OverlayError as exc:
        print("РЕЗУЛЬТАТ: видео не открылось")
        print()
        print(f"  Что случилось: {exc.why}")
        if exc.fix:
            print(f"  Что делать:    {exc.fix}")
        print()
        print("Если в браузере эта ссылка играет, а тут — нет,")
        print("пришлите этот вывод разработчику.")
        return 1

    print("РЕЗУЛЬТАТ: видео открылось")
    print()
    print(f"  Название:      {stream.title}")
    if stream.duration_s:
        print(f"  Длительность:  {stream.duration_s // 60} мин {stream.duration_s % 60} с")
    if stream.is_live:
        print("  Прямой эфир:   да")
    print(f"  Качеств:       {len(stream.qualities)}")
    for quality in stream.qualities:
        print(
            f"     {quality.label:>10}  {quality.width}x{quality.height}"
            f"  {quality.bitrate_kbps} kbps"
        )
    if stream.best:
        print(f"\n  Лучшее качество: {stream.best.label}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Как пользоваться:")
        print("  py -m tests.manual.check_link https://ссылка-на-видео")
        return 2
    return check(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
