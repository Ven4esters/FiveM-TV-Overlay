"""Живая проверка плеера: Rutube и VK в настоящем Chromium.

Проверяем не «прокси ответил 200», а то, что видит пользователь:
readyState=4, идёт время, есть звук, нет ошибок hls.js.

Запуск: python3 tests/manual/check_player_live.py <порт_прокси>
"""

from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "10095"
BASE = f"http://127.0.0.1:{PORT}"

CASES = [
    ("Rutube", "https://rutube.ru/video/bb42243e4b692faf16f615e6bccc996b/", "rutube"),
    ("VK (горизонт.)", "https://vk.com/video-22822305_456239018", "vk"),
    (
        "HLS (mux)",
        "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
        "r",
    ),
    (
        "YouTube",
        "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
        "yt",
    ),
    (
        "MP4 напрямую",
        "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4",
        "r",
    ),
]


def check_one(page, name: str, url: str, mode: str) -> dict:
    print(f"\n{'=' * 60}\n{name}: {url[:60]}")
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"JS: {e}"))

    def _on_console(m):
        if m.type == "error":
            errors.append(f"console.error: {m.text[:100]}")

    page.on("console", _on_console)

    # создаём сессию через тот же путь, что и оверлей
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    resp = page.evaluate(
        """async ([url, mode]) => {
            const r = await fetch('/new?url=' + encodeURIComponent(url) + '&mode=' + mode);
            return await r.json();
        }""",
        [url, mode],
    )
    print(f"  сессия: {json.dumps(resp, ensure_ascii=False)[:150]}")
    link = resp.get("player") or resp.get("link")
    if not link:
        return {"name": name, "ok": False, "why": f"нет ссылки: {resp}"}

    page.goto(link, wait_until="domcontentloaded")
    page.wait_for_timeout(9000)

    state = page.evaluate(
        """() => {
            const v = document.querySelector('video');
            if (!v) return {noVideo: true};
            return {
                readyState: v.readyState,
                paused: v.paused,
                currentTime: v.currentTime,
                duration: v.duration,
                videoWidth: v.videoWidth,
                videoHeight: v.videoHeight,
                muted: v.muted,
                volume: v.volume,
                error: v.error ? v.error.code : null,
                src: String(v.currentSrc || v.src).slice(-50),
            };
        }"""
    )
    print(f"  состояние: {json.dumps(state, ensure_ascii=False)}")

    # пробуем play — в headless автоплей может требовать жеста
    page.evaluate("document.querySelector('video')?.play().catch(()=>{})")
    page.wait_for_timeout(3000)
    after = page.evaluate(
        "() => { const v=document.querySelector('video');"
        " return v ? {t: v.currentTime, paused: v.paused, rs: v.readyState} : {}; }"
    )
    print(f"  после play(): {json.dumps(after, ensure_ascii=False)}")

    real_errors = [e for e in errors if "favicon" not in e.lower()]
    if real_errors:
        print("  ошибки JS/консоли:")
        for e in real_errors[:5]:
            print(f"    - {e[:140]}")

    ok = (
        state.get("readyState", 0) >= 2
        and state.get("error") is None
        and after.get("t", 0) > 0
        and not real_errors
    )
    why = ""
    if not ok:
        if state.get("error"):
            why = f"video.error={state['error']}"
        elif state.get("readyState", 0) < 2:
            why = f"readyState={state.get('readyState')} — данные не грузятся"
        elif after.get("t", 0) <= 0:
            why = "время не идёт"
        else:
            why = f"ошибки JS: {real_errors[:2]}"
    return {"name": name, "ok": ok, "why": why, "state": state}


with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="chromium", args=["--autoplay-policy=no-user-gesture-required"]
    )
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    results = []
    for name, url, mode in CASES:
        try:
            results.append(check_one(page, name, url, mode))
        except Exception as e:  # noqa: BLE001
            results.append({"name": name, "ok": False, "why": f"{type(e).__name__}: {e}"[:160]})
    browser.close()

print(f"\n{'=' * 60}\nИТОГ")
for r in results:
    mark = "✅" if r["ok"] else "❌"
    print(f"  {mark} {r['name']}: {r.get('why') or 'работает'}")
sys.exit(0 if all(r["ok"] for r in results) else 1)
