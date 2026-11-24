"""Проверка цепочки Alloha прямо в приложении: `python -m tvhost2.alloha_check`.

ЗАЧЕМ. Пробник `tools/alloha/probe_bnsi.py` — отдельный файл, его надо
скачать и он не видит настроек приложения. А раз приложение уже работает на
этом компьютере (а компьютер в России), проще спросить у него самого: он
пройдёт той же дорогой, что и при открытии ссылки, и покажет, на каком шаге
что ответил сервер.

ЗАПУСК (из папки проекта, окно приложения можно не закрывать):

    python -m tvhost2.alloha_check "https://zlord.org/17222-....html"

СКРИПТ НИЧЕГО НЕ МЕНЯЕТ: только читает. Ответы кладёт в `alloha_check/`
рядом с текущей папкой — `*.json` и `summary.txt`.

Если всё хорошо, в конце будет строка «ИТОГ: поток получен» и список качеств
с адресами. Если нет — будет видно, на каком шаге и с каким кодом.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

from .sources import alloha, alloha_bnsi

OUT = Path.cwd() / "alloha_check"


def _line(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or not args[0].startswith("http"):
        print(__doc__)
        print("Пример (Windows — python, без цифры 3):")
        print('  python -m tvhost2.alloha_check "https://zlord.org/17222-....html"')
        return 0

    page_url = args[0].strip()
    OUT.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report: list[str] = [f"страница: {page_url}", ""]

    def say(text: str) -> None:
        print(text)
        report.append(text)

    _line("1. Настройка источника (как в приложении)")

    source = alloha.AllohaSource() if hasattr(alloha, "AllohaSource") else None
    if source is None:  # pragma: no cover - на случай переименования класса
        say("НЕ НАШЁЛ класс источника Alloha — проверьте версию приложения")
        return 1

    # Приватный метод осознанно: диагностика должна идти тем же путём,
    # что и обычное открытие ссылки, включая кэш.
    info = source._info_of(page_url)
    if not info:
        say("На странице нет встройки Alloha — ссылка не та либо плеер грузится скриптом")
        _save(stamp, report)
        return 1

    tracks: list[dict[str, str]] = list(info.get("tracks") or [])
    embed = str((tracks[0] if tracks else {}).get("iframe") or info.get("embed") or "")
    say(f"встройка: {embed[:100]}")
    say(f"озвучек: {len(tracks)}" + ("" if tracks else " (API не ответил)"))
    for position, track in enumerate(tracks, 1):
        say(f"   {position}. {track.get('title', '?')}  (id {track.get('id', '?')})")

    _line("2. Страница плеера")
    player = alloha_bnsi.page_text(embed, page_url)
    if not player:
        say("Плеер не отдал страницу. Если это регион — см. пункт 5.")
    else:
        say(f"получено {len(player)} байт")
        blocked = alloha.blocked_by_region(player)
        say(f"признак отказа по региону: {'ДА' if blocked else 'нет'}")
        meta = alloha_bnsi.meta_of(player)
        say(f"мета viewporti: {'есть, ' + str(len(meta)) + ' символов' if meta else 'НЕТ'}")
        ids = alloha_bnsi.file_ids(player)
        say(f"файлов в fileList: {len(ids)} → {', '.join(ids[:6]) or '—'}")
        say(f"тип: {alloha_bnsi.kind_of(player)}")
        borth = alloha_bnsi.borth_for(player)
        say(f"Borth собран: {len(borth)} символов")

        _line("3. Запрос адресов (POST /bnsi)")
        body, sources = alloha_bnsi.fetch_sources(embed, player)
        if sources:
            say(f"hlsSource: {len(sources)} озвучек — ПОЛУЧЕНО")
            qualities = alloha.qualities_of_sources(sources, 0)
            say(f"качеств в первой озвучке: {len(qualities)}")
            for quality in qualities[:5]:
                say(f"   {quality.height}p — {quality.url[:90]}")
            name = f"{stamp}_hlsSource.json"
            (OUT / name).write_text(body, encoding="utf-8")
            say(f"тело ответа сохранено: {name}")
            _save(stamp, report)
            _line("ИТОГ")
            say("поток получен. Пришлите summary.txt и json — сверю разбор с живым ответом.")
            return 0

        say(f"пусто. Тело ответа ({len(body)} байт): {body[:200]}")
        if alloha_bnsi.asked_region(body):
            say("ответ — «контент не найден»: это региональный отказ Alloha.")
        (OUT / f"{stamp}_bnsi_response.txt").write_text(body or "", encoding="utf-8")

    _line("4. Что это значит")
    say(
        "Если на шаге 2 страница плеера не пришла — Alloha закрывает по региону, "
        "и это не лечится на нашей стороне."
    )
    say(
        "Если пришла, но шаг 3 пуст — пришлите summary.txt: по коду ответа будет "
        "видно, сверяет ли сервер отпечаток браузера строже, чем мы думали."
    )
    _save(stamp, report)
    return 1


def _save(stamp: str, report: list[str]) -> None:
    path = OUT / f"{stamp}_summary.txt"
    path.write_text("\n".join(report), encoding="utf-8")
    print(f"\nОтчёт: {path}")
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        input("Нажмите Enter, чтобы закрыть окно… ")


if __name__ == "__main__":
    raise SystemExit(main())
