"""Замер памяти: сколько на самом деле занимает tvhost2.

Зачем отдельный инструмент, а не «посмотреть глазами»: цифра из диспетчера
задач ничего не говорит о том, ЧТО её съело. Почта растёт от обычного
`import` (PySide6 тянет десятки мегабайт), от сессий, от сегментов видео в
буфере моста и от Chromium, который сам по себе занимает больше, чем наш
сервер. Поэтому память снимается по этапам, и на каждом этапе видно
прибавку к предыдущему.

Меряется RSS (resident set size) — сколько физической памяти процесс держит
в данный момент. Для Python это честная цифра: освобождённые объекты
возвращаются системе не сразу, но RSS показывает то, что игра реально
отнимает у системы.

Запуск:

    python tools/bench_memory.py              # полный прогон (~4 минуты)
    python tools/bench_memory.py --quick      # без проигрывания (быстро)
    python tools/bench_memory.py --json out.json

Скрипт сам поднимает сервер на свободном порту и сам его гасит. Ничего в
рабочем каталоге не меняется, кроме временного файла кэша громкости в
`videos/cache/`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# Ссылка для замера: живая страница серии. Если она недоступна (сайт лёг
# или сменил разметку), скрипт честно скажет об этом, а не выдумает цифры.
SAMPLE_URL = "https://anidub.com/12492-operacija-semejka-edzakura.html"

MB = 1024 * 1024


@dataclass
class Step:
    """Один этап замера: что сделали и сколько памяти стало."""

    name: str
    note: str
    rss_mb: float
    delta_mb: float
    extra: dict[str, float] = field(default_factory=dict)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def rss_mb(pid: int) -> float:
    """RSS процесса в мегабайтах. Ноль, если процесс уже умер."""
    try:
        return psutil.Process(pid).memory_info().rss / MB
    except psutil.Error:
        return 0.0


def tree_rss_mb(pid: int) -> float:
    """RSS процесса со всеми потомками (ffmpeg, дочерние python)."""
    try:
        process = psutil.Process(pid)
    except psutil.Error:
        return 0.0
    total = 0
    try:
        total += process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                pass
    except psutil.Error:
        return 0.0
    return total / MB


def wait_server(base: str, process: subprocess.Popen[bytes], timeout: float = 30.0) -> bool:
    """Ждём, пока сервер начнёт отвечать. Умер — сразу выходим."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(base + "/sources", timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def close_session(base: str, sid: str) -> None:
    """POST /close/<sid> — штатное закрытие сессии."""
    request = urllib.request.Request(f"{base}/close/{sid}", data=b"", method="POST")
    urllib.request.urlopen(request, timeout=5).read()


def get_json(url: str, timeout: float = 120.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class Bench:
    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.base = ""
        self.process: subprocess.Popen[bytes] | None = None
        self._last = 0.0

    def mark(self, name: str, note: str, extra: dict[str, float] | None = None) -> float:
        """Зафиксировать этап. Возвращает текущий RSS."""
        assert self.process is not None
        current = tree_rss_mb(self.process.pid)
        self.steps.append(
            Step(
                name=name,
                note=note,
                rss_mb=round(current, 1),
                delta_mb=round(current - self._last, 1),
                extra={k: round(v, 1) for k, v in (extra or {}).items()},
            )
        )
        self._last = current
        print(f"  {name:28} {current:7.1f} МБ  ({current - self.steps[-2].rss_mb:+.1f})")
        return current

    # ---------------- этапы ----------------

    def baseline(self) -> None:
        """Сколько стоит пустой интерпретатор без нашего кода."""
        code = "import sys, json; print(json.dumps({'rss': 0}))"
        output = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
        )
        assert output.stdout  # интерпретатор ответил
        pid = os.getpid()
        self._last = rss_mb(pid)
        self.steps.append(
            Step(
                name="Ноль: пустой Python",
                note="этот самый процесс, без импорта наших модулей",
                rss_mb=round(self._last, 1),
                delta_mb=0.0,
            )
        )
        print(f"  {'Ноль: пустой Python':28} {self._last:7.1f} МБ")

    def start_server(self, port: int) -> bool:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tvhost2", "--port", str(port), "--host", "127.0.0.1"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.base = f"http://127.0.0.1:{port}"
        return wait_server(self.base, self.process)

    def stop_server(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def open_sessions(self, count: int, url: str) -> list[str]:
        sids: list[str] = []
        for _ in range(count):
            try:
                data = get_json(f"{self.base}/new?url={urllib.parse.quote(url, safe='')}")
            except Exception:
                break
            if data.get("ok"):
                sids.append(data["sid"])
        return sids


def run(quick: bool, sample_url: str) -> int:
    bench = Bench()
    port = free_port()

    print("Замер памяти tvhost2")
    print("=" * 62)
    bench.baseline()

    if not bench.start_server(port):
        print("  сервер не поднялся — замер невозможен")
        bench.stop_server()
        return 1
    time.sleep(2)
    bench.mark("Сервер запущен", "uvicorn-подобный ThreadingHTTPServer + все источники")

    # 1) одна сессия: страница → разбор → поток
    sids: list[str] = []
    try:
        data = get_json(f"{bench.base}/new?url={urllib.parse.quote(sample_url, safe='')}")
        if data.get("ok"):
            sids.append(data["sid"])
            bench.mark(
                "Одна сессия разобрана", f"{data.get('source')}: {data.get('title', '')[:40]}"
            )
        else:
            print(f"  источник не отдал поток: {data.get('reason', '')[:70]}")
            bench.mark("Одна сессия (неудачно)", str(data.get("reason", ""))[:40])
    except Exception as exc:  # источник мог лечь
        print(f"  разбор не удался: {exc}")
        bench.mark("Одна сессия (ошибка)", type(exc).__name__)

    # 2) плейлист через мост — первый настоящий трафик видео
    if sids:
        try:
            with urllib.request.urlopen(f"{bench.base}/stream/{sids[0]}", timeout=60) as response:
                body = response.read()
            bench.mark("Master-плейлист отдан", f"{len(body)} байт через мост")
        except Exception as exc:
            print(f"  плейлист не отдался: {exc}")

    # 3) много сессий: как растёт память при переключении каналов
    more = bench.open_sessions(9, sample_url)
    sids.extend(more)
    bench.mark("10 сессий открыто", f"+{len(more)} к первой", {"sessions": float(len(sids))})

    # 4) проигрывание в Chromium — настоящая нагрузка на мост
    browser_mb = 0.0
    if not quick and sids:
        browser_mb = play_in_browser(bench.base, sids[0])
        bench.mark(
            "Проигрывание 60 с",
            "Chromium тянет сегменты через мост",
            {"browser_mb": browser_mb},
        )
        # ловим утечку: смотрим, растёт ли память на ровном проигрывании
        time.sleep(30)
        steady = bench.mark("Ещё 30 с проигрывания", "проверка на утечку")
        if steady - bench.steps[-2].rss_mb > 20:
            print("  ⚠ память выросла больше чем на 20 МБ на ровном месте")

    # 5) закрываем всё
    for sid in sids:
        # сессия могла уже протухнуть — для замера это не важно
        with contextlib.suppress(Exception):
            close_session(bench.base, sid)
    time.sleep(1)
    bench.mark("Сессии закрыты", f"освобождено {len(sids)} сессий")

    bench.stop_server()
    print("=" * 62)
    print(
        f"Пик: {max(s.rss_mb for s in bench.steps):.1f} МБ, конец: {bench.steps[-1].rss_mb:.1f} МБ"
    )
    return bench


def play_in_browser(base: str, sid: str) -> float:
    """Открыть плеер в Chromium и играть 60 с. Возвращает RSS браузера."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright нет — проигрывание пропущено")
        return 0.0

    peak = 0.0
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(f"{base}/player/{sid}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
            page.evaluate("document.querySelector('#bigplay')?.click()")
            deadline = time.time() + 60
            while time.time() < deadline:
                page.wait_for_timeout(5000)
                state = page.evaluate(
                    "() => { const v = document.querySelector('video');"
                    " return v ? {t: v.currentTime, frames: v.webkitDecodedFrameCount || 0} : {}; }"
                )
                current = tree_rss_mb(browser_pid(browser)) or chrome_mb()
                peak = max(peak, current)
                if state.get("frames", 0) <= 0:
                    print("    ⚠ кадры не декодируются — вероятно, кодек или сеть")
            print(f"    Chromium: до {peak:.0f} МБ")
            browser.close()
    except Exception as exc:
        print(f"  проигрывание сорвалось: {type(exc).__name__}: {exc}")
    return round(peak, 1)


def browser_pid(browser: object) -> int:
    """PID процесса браузера.

    У Playwright это `browser.process` — НЕ всегда доступен: у подключённого
    браузера (connect) его нет, и тогда вернём 0. Раньше я брал его у
    `page.context.browser`, и замер молча показывал 0 МБ — то есть мерил
    пустоту и не сообщал об этом.
    """
    try:
        process = getattr(browser, "process", None)
        return int(process.pid) if process else 0
    except Exception:
        return 0


def chrome_mb() -> float:
    """RSS всех процессов Chromium, которые сейчас живут.

    Запасной способ: Playwright может не отдать PID, но процессы видны
    в таблице процессов. Берём только те, что запущены из его кэша.
    """
    total = 0
    for proc in psutil.process_iter(["name", "cmdline", "memory_info"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or ())
            if "chrome" not in (proc.info["name"] or "").lower():
                continue
            if "ms-playwright" not in cmdline and "chromium" not in cmdline.lower():
                continue
            info = proc.info["memory_info"]
            total += info.rss if info else 0
        except psutil.Error:
            continue
    return total / MB


def main() -> int:
    parser = argparse.ArgumentParser(description="Замер памяти tvhost2")
    parser.add_argument("--quick", action="store_true", help="без проигрывания в браузере")
    parser.add_argument("--url", default=SAMPLE_URL, help="ссылка для замера")
    parser.add_argument("--json", default="", help="куда сохранить замер")
    args = parser.parse_args()

    if not shutil.which("ffmpeg"):
        print("внимание: ffmpeg не найден, замер громкости не понадобится")

    bench = run(args.quick, args.url)
    if isinstance(bench, int):
        return bench

    if args.json:
        Path(args.json).write_text(
            json.dumps([asdict(step) for step in bench.steps], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"сохранено: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
