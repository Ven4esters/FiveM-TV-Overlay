"""
Уборка мусора: кэш скачанных видео и временные профили браузера.

Зачем: при раздаче видео через скачивание в videos/cache/ оставались
готовые файлы (фильм — это 4 ГБ), и папка раздувалась незаметно.

Запуск:
    python cleanup.py            — почистить и показать, сколько освободилось
    python cleanup.py --dry-run  — только показать, что будет удалено
    python cleanup.py --days 3   — удалять файлы старше 3 дней (по умолчанию 1)
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIRS = [
    os.path.join(BASE_DIR, "videos", "cache"),  # скачанные видео
    os.path.join(BASE_DIR, "videos", "tmp"),  # временные вырезки
]


def _human(size: int) -> str:
    """Размер в человекочитаемом виде: 512 Б, 2 КБ, 5 МБ, 3 ГБ."""
    value: float = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value = value / 1024
    return f"{value:.1f} ТБ"


def _cache_files(max_age_days: float) -> list[tuple[str, int, float]]:
    """Файлы кэша старше указанного возраста: [(путь, байт, возраст_дней)]."""
    limit = time.time() - max_age_days * 86400
    out: list[tuple[str, int, float]] = []
    for d in CACHE_DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime <= limit:
                out.append((path, st.st_size, (time.time() - st.st_mtime) / 86400))
    return out


def _temp_profiles() -> list[str]:
    """Оставшиеся временные профили браузера после поиска потоков."""
    out: list[str] = []
    root = tempfile.gettempdir()
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        if name.startswith("gta-sniff-"):
            out.append(os.path.join(root, name))
    return out


def cleanup(max_age_days: float = 1.0, dry_run: bool = False) -> int:
    """Почистить кэш и профили. Возвращает освобождённые байты."""
    freed = 0

    files = _cache_files(max_age_days)
    for path, size, age in files:
        print(
            f"  {'[ ]' if dry_run else '[-]'} {_human(size):>10}  "
            f"{age:5.1f} дн.  {os.path.basename(path)}"
        )
        if not dry_run:
            try:
                os.remove(path)
            except OSError as e:
                print(f"      не удалось: {e}")
                continue
        freed += size

    for path in _temp_profiles():
        size = 0
        for dirpath, _dirs, names in os.walk(path):
            for n in names:
                try:
                    size += os.path.getsize(os.path.join(dirpath, n))
                except OSError:
                    pass
        print(
            f"  {'[ ]' if dry_run else '[-]'} {_human(size):>10}  "
            f"профиль      {os.path.basename(path)}"
        )
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)
        freed += size

    return freed


def auto_clean(max_age_days: float = 1.0) -> int:
    """Тихая уборка при запуске приложения (без вывода, если нечего чистить)."""
    try:
        freed = cleanup(max_age_days=max_age_days)
    except Exception:
        return 0
    if freed:
        print(f"🧹 Очистка кэша: освобождено {_human(freed)}")
    return freed


def main() -> None:
    ap = argparse.ArgumentParser(description="Уборка кэша видео и профилей браузера")
    ap.add_argument(
        "--days", type=float, default=1.0, help="удалять файлы старше N дней (по умолчанию 1)"
    )
    ap.add_argument("--dry-run", action="store_true", help="только показать, ничего не удалять")
    args = ap.parse_args()

    print(f"Кэш: {', '.join(CACHE_DIRS)}")
    freed = cleanup(max_age_days=args.days, dry_run=args.dry_run)
    if freed:
        verb = "будет освобождено" if args.dry_run else "освобождено"
        print(f"\n✅ {verb}: {_human(freed)}")
    else:
        print("\n✅ Чисто — удалять нечего.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
