"""Живая проверка: декодируется ли ЗВУК в HLS-мастере.

Зачем этот скрипт существует (ERR-076): я один раз решил, что hls.js
не проиграет сырой AAC-сегмент YouTube, и построил обходной путь через
ffmpeg — 25 секунд на старте и 150 МБ на диске вместо мгновенного старта.
Заказчик сказал: «у меня играет». Оказался прав он.

Вывод, записанный в код: прежде чем строить обход, ИЗМЕРИТЬ.

Как пользоваться:
    py -m tests.manual.probe_hls_audio <адрес-master.m3u8>

Скрипт откроет настоящий Chromium с hls.js и покажет:
  - растёт ли readyState до 4 (идёт воспроизведение);
  - растёт ли счётчик ДЕКОДИРОВАННОГО звука (webkitAudioDecodedByteCount);
  - работает ли перемотка;
  - работает ли смена качества.

Счётчик аудио — главное. «Поток загрузился» и «звук звучит» — разные вещи.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HTML = """<!doctype html><html><body style="margin:0;background:#111">
<video id="v" muted playsinline style="width:100%;height:100%"></video>
<script src="hls.min.js"></script>
<script>
window.log = [];
const v = document.getElementById('v');
const H = window.Hls;
if (!H || !H.isSupported()) {
  window.log.push('hls.js не поддерживается этим браузером');
} else {
  const h = new H({debug: false});
  window.hls = h;
  h.attachMedia(v);
  h.on(H.Events.MANIFEST_PARSED, () => { v.play().catch(e => window.log.push('play: ' + e)); });
  h.on(H.Events.ERROR, (_e, d) => { window.log.push((d.fatal ? 'FATAL ' : 'warn ') + d.details); });
  h.loadSource(new URLSearchParams(location.search).get('src'));
}
</script></body></html>"""


def _snapshot(page, label: str) -> dict:
    data = page.evaluate(
        """() => {
            const v = document.getElementById('v');
            const h = window.hls || {};
            return {
                readyState: v.readyState,
                time: +(v.currentTime || 0).toFixed(1),
                audio: v.webkitAudioDecodedByteCount,
                video: v.webkitVideoDecodedByteCount,
                size: v.videoWidth + 'x' + v.videoHeight,
                level: h.currentLevel,
            };
        }"""
    )
    print(
        f"  {label:20} rs={data['readyState']}  t={data['time']:>7}"
        f"  звук={data['audio']:>8}  видео={data['video']:>9}"
        f"  {data['size']:>10}  уровень={data['level']}"
    )
    return data


def main(master_url: str, hls_js: Path) -> int:
    from playwright.sync_api import sync_playwright  # тяжёлая зависимость — только здесь

    html_file = Path(tempfile.mkdtemp()) / "probe.html"
    html_file.write_text(HTML, encoding="utf-8")
    (html_file.parent / "hls.min.js").write_bytes(hls_js.read_bytes())

    # Подставляем адрес прямо в HTML: set_content не передаёт query-параметры.
    html = HTML.replace("new URLSearchParams(location.search).get('src')", repr(master_url))
    html_file.write_text(html, encoding="utf-8")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chromium",
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
        page = browser.new_context(viewport={"width": 960, "height": 540}).new_page()
        page.goto(html_file.as_uri(), wait_until="load")
        page.wait_for_timeout(10_000)

        print("=== ВОСПРОИЗВЕДЕНИЕ ===")
        first = _snapshot(page, "старт+10с")
        page.wait_for_timeout(6_000)
        second = _snapshot(page, "+6с")

        print("\n=== ПЕРЕМОТКА на 2 минуты ===")
        page.evaluate("() => { document.getElementById('v').currentTime = 120; }")
        page.wait_for_timeout(5_000)
        seek = _snapshot(page, "после перемотки")

        print("\n=== СМЕНА КАЧЕСТВА на уровень 2 ===")
        page.evaluate("() => { if (window.hls) window.hls.currentLevel = 2; }")
        page.wait_for_timeout(6_000)
        last = _snapshot(page, "после смены")

        errors = page.evaluate("() => window.log")
        browser.close()

    print()
    if errors:
        print("ошибки плеера:")
        for line in errors[:8]:
            print("  ", line)
    else:
        print("ошибки плеера: нет")

    print()
    audio_ok = last["audio"] > second["audio"] > 10_000
    print("ЗВУК:", "ДЕКОДИРУЕТСЯ" if audio_ok else "НЕ декодируется")
    print("ВИДЕО:", "ИДЁТ" if last["video"] > 0 and last["readyState"] == 4 else "проблема")
    print("ПЕРЕМОТКА:", "работает" if seek["time"] > 100 else "не работает")
    print(
        "СМЕНА КАЧЕСТВА:",
        "работает" if last["level"] == 2 and last["size"] != first["size"] else "без изменений",
    )
    return 0 if audio_ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Как пользоваться: py -m tests.manual.probe_hls_audio <адрес master.m3u8>")
        raise SystemExit(2)
    hls_path = Path(__file__).resolve().parent / "hls.min.js"
    if not hls_path.exists():
        print(f"нет файла {hls_path} — положите рядом hls.min.js")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], hls_path))
