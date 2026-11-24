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
from typing import Any

from .sources import alloha, alloha_bnsi

# src/tvhost2/ → src/ → корень проекта
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "alloha_check"

Samples = list[str]
"""Строки отчёта. Собираются по ходу, чтобы в конце сохранить summary.txt."""


def pick_embed(info: dict[str, Any], tracks: list[dict[str, str]], page_url: str) -> str:
    """Встройка, с которой имеет смысл работать.

    Тонкость, из-за которой путал сам: на странице бывает ДВЕ встройки
    Alloha — одна с `kp=`, другая с `token=`. Открытый API озвучек понимает
    только `token`, поэтому «первая с kp» может дать пустой список, хотя
    озвучки есть. Сначала берём ту, где есть токен; иначе — что дали.
    """
    candidates: list[str] = []
    iframe = str((tracks[0] if tracks else {}).get("iframe") or "")
    if iframe:
        candidates.append(iframe)
    candidates.append(str(info.get("embed") or ""))
    try:
        html = alloha.fetch_text(page_url) if hasattr(alloha, "fetch_text") else ""
    except Exception:  # сеть могла отвалиться — не повод падать из-за диагностики
        html = ""
    if html:
        for url in alloha.embeds_of(html):
            candidates.append(url)
    for url in candidates:
        if url and alloha.site_token(url):
            return url
    return next((url for url in candidates if url), "")


BUILD = "2026-11-25 · id_file+Borth из curl"
"""Метка сборки: первая строка отчёта. По ней видно, обновился ли код.

Зачем: прогон 25.11 00:0x шёл ещё на старом коде (перебор начинался с
`1847894`, хотя браузер просит `1836464`), и по отчёту это было незаметно.
"""


def _line(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


class Report:
    """Печатает строку и тут же кладёт её в файл отчёта."""

    def __init__(self, stamp: str) -> None:
        self.stamp = stamp
        self.lines: list[str] = []

    def say(self, text: str) -> None:
        print(text)
        self.lines.append(text)

    def save(self) -> None:
        path = OUT / f"{self.stamp}_summary.txt"
        path.write_text("\n".join(self.lines), encoding="utf-8")
        print(f"\nОтчёт: {path}")
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            input("Нажмите Enter, чтобы закрыть окно… ")


def _setup_block(report: Report, page_url: str) -> tuple[bool, str, list[dict[str, str]]]:
    """Шаг 1: найти встройку так же, как это делает приложение."""
    _line("1. Настройка источника (как в приложении)")
    source = alloha.AllohaSource() if hasattr(alloha, "AllohaSource") else None
    if source is None:  # pragma: no cover - на случай переименования класса
        report.say("НЕ НАШЁЛ класс источника Alloha — проверьте версию приложения")
        return False, "", []

    # Приватный метод осознанно: диагностика должна идти тем же путём,
    # что и обычное открытие ссылки, включая кэш.
    info = source._info_of(page_url)
    if not info:
        report.say("На странице нет встройки Alloha — ссылка не та либо плеер грузится скриптом")
        return False, "", []

    tracks: list[dict[str, str]] = list(info.get("tracks") or [])
    embed = pick_embed(info, tracks, page_url)
    report.say(f"встройка: {embed[:110]}")
    token = alloha.site_token(embed)
    report.say(f"токен сайта: {token or 'НЕ НАЙДЕН — открытый API будет молчать'}")
    report.say(f"озвучек: {len(tracks)}" + ("" if tracks else " (API не ответил)"))
    for position, track in enumerate(tracks, 1):
        report.say(f"   {position}. {track.get('title', '?')}  (id {track.get('id', '?')})")
    return True, embed, tracks


def _player_block(report: Report, embed: str, page_url: str) -> tuple[str, list[str]]:
    """Шаг 2: скачать страницу плеера и достать из неё номера файлов."""
    _line("2. Страница плеера")
    player = alloha_bnsi.page_text(embed, page_url)
    if not player:
        report.say("Плеер не отдал страницу. Если это регион — см. пункт 4.")
        return "", []
    report.say(f"получено {len(player)} байт")
    blocked = alloha.blocked_by_region(player)
    report.say(f"признак отказа по региону: {'ДА' if blocked else 'нет'}")
    meta = alloha_bnsi.meta_of(player)
    report.say(f"мета viewporti: {'есть, ' + str(len(meta)) + ' символов' if meta else 'НЕТ'}")
    ids = alloha_bnsi.file_ids(player)
    report.say(f"файлов в fileList: {len(ids)} → {', '.join(ids[:6]) or '—'}")
    report.say(f"тип: {alloha_bnsi.kind_of(player)}")
    learnt = alloha_bnsi.learnt_borths()
    if learnt:
        report.say(f"Borth: ВЗЯТ ИЗ curl.txt — {len(learnt[0])} символов (настоящий)")
    else:
        size = len(alloha_bnsi.borth_for(player))
        report.say(f"Borth собран: {size} символов (из UA — сервер может не принять)")
    report.say(f"пойдут номера: {', '.join(ids[:6])}")
    first = ids[0] if ids else ""
    report.say(
        "первым — id_file активного файла (его просит браузер): "
        + (first if first else "НЕ НАЙДЕН")
    )
    return player, ids


def _plain_block(report: Report, stamp: str, embed: str, player: str) -> bool:
    """Шаг 3: обычный запрос — по одной попытке на номер файла."""
    _line("3. Запрос адресов (POST /bnsi)")
    tries = alloha_bnsi.attempts(embed, player)
    report.say(f"попыток: {len(tries)} (по одной на номер файла)")
    for attempt in tries:
        verdict = "ok" if "hlsSource" in str(attempt.get("body")) else "нет"
        report.say(f"   id {attempt.get('id')} → {attempt.get('status')} (hlsSource: {verdict})")
    body = str((tries[-1] if tries else {}).get("body") or "")
    sources = alloha_bnsi.sources_of(body)
    if not sources:
        report.say(f"пусто. Тело ответа ({len(body)} байт): {body[:200]}")
        if alloha_bnsi.asked_region(body):
            report.say("ответ — «контент не найден»: Alloha не отдаёт адреса этому запросу.")
        (OUT / f"{stamp}_bnsi_response.txt").write_text(body or "", encoding="utf-8")
        return False

    report.say(f"hlsSource: {len(sources)} озвучек — ПОЛУЧЕНО")
    qualities = alloha.qualities_of_sources(sources, 0)
    report.say(f"качеств в первой озвучке: {len(qualities)}")
    for quality in qualities[:5]:
        report.say(f"   {quality.height}p — {quality.url[:90]}")
    name = f"{stamp}_hlsSource.json"
    (OUT / name).write_text(body, encoding="utf-8")
    report.say(f"тело ответа сохранено: {name}")
    return True


def _variant_block(report: Report, embed: str, player: str, ids: list[str]) -> str:
    """Шаг 3б: перебрать варианты тела и заголовков. Пусто — если не вышло."""
    _line("3б. Перебор вариантов запроса")
    report.say("Обычный вариант не дал адресов — проверяю, не в запросе ли дело.")
    rows = alloha_bnsi.try_variants(embed, player, ids)
    for row in rows:
        report.say(
            f"   {row['name'][:38]:38s} id {row['id']} → {row['status']}"
            + ("   ← ЕСТЬ hlsSource!" if row["ok"] else "")
        )
        if row["ok"]:
            return str(row["body"])
    report.say("ни один вариант не подошёл: дело не в теле и не в заголовках.")
    return ""


def _borth_block(report: Report, embed: str, player: str, ids: list[str]) -> str:
    """Шаг 3в: `Borth` с отпечатком, который считает сам сервер (`/sarn`)."""
    _line("3в. Отпечаток Borth из ответа сервера")
    realm = alloha_bnsi._origin_of(embed).split("//")[-1]
    uap = alloha_bnsi._uap_of(realm)
    if uap:
        report.say(f"сервер считает отпечаток клиента: {uap[:24]}… ({len(uap)} символов)")
    else:
        report.say("/sarn не ответил — отпечаток сервера получить не удалось")
    rows = alloha_bnsi.try_real_borth(embed, player, ids)
    report.say(f"вариантов: {len(rows)}")
    for row in rows:
        report.say(
            f"   {row['name'][:44]:44s} id {row['id']} → {row['status']}"
            + ("   ← ЕСТЬ hlsSource!" if row["ok"] else "")
        )
        if row["ok"]:
            return str(row["body"])
    if not rows:
        report.say("проверить не удалось: /sarn не дал отпечаток.")
    return ""


def _final(report: Report) -> None:
    """Шаг 4: что это значит и что делать дальше."""
    _line("4. Что это значит")
    report.say(
        "Если на шаге 2 страница плеера не пришла — Alloha закрывает по региону "
        "и это не лечится на нашей стороне."
    )
    report.say(
        "Если страница пришла, а шаг 3 пуст — остаётся снять запрос в браузере: "
        "F12 → Network → фильтр bnsi → правой кнопкой → Copy as cURL."
    )
    report.say(
        "Вставьте это в alloha_check\\curl.txt и запустите replay_alloha.bat — "
        "он повторит запрос плеера один в один и покажет отличие от нашего."
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or not args[0].startswith("http"):
        print(__doc__)
        print("Пример (Windows — python, без цифры 3):")
        print('  python -m tvhost2.alloha_check "https://zlord.org/17222-....html"')
        return 0

    page_url = args[0].strip()
    OUT.mkdir(exist_ok=True)
    print(f"Отчёты складываю в: {OUT}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = Report(stamp)
    report.say(f"сборка: {BUILD}")
    report.say(f"страница: {page_url}")
    report.say("")

    ok, embed, _tracks = _setup_block(report, page_url)
    if not ok:
        report.save()
        return 1

    player, ids = _player_block(report, embed, page_url)
    if not player:
        _final(report)
        report.save()
        return 1

    if _plain_block(report, stamp, embed, player):
        report.save()
        _line("ИТОГ")
        report.say("поток получен. Пришлите summary.txt и json — сверю разбор.")
        return 0

    body = _variant_block(report, embed, player, ids) or _borth_block(report, embed, player, ids)
    if body:
        name = f"{stamp}_hlsSource.json"
        (OUT / name).write_text(body, encoding="utf-8")
        report.say(f"рабочий ответ сохранён: {name}")
        report.save()
        _line("ИТОГ")
        report.say("поток получен перебором — пришлите summary.txt и json.")
        return 0

    _final(report)
    report.save()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
