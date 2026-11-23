"""Проверка одного источника в Chromium: состояние + скриншот."""

import json
import sys

from playwright.sync_api import sync_playwright

PORT = sys.argv[1]
URL = sys.argv[2]
MODE = sys.argv[3] if len(sys.argv) > 3 else "r"
SHOT = sys.argv[4] if len(sys.argv) > 4 else ""
WAIT = int(sys.argv[5]) if len(sys.argv) > 5 else 16000

BASE = f"http://127.0.0.1:{PORT}"

with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="chromium", args=["--autoplay-policy=no-user-gesture-required"]
    )
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on(
        "console",
        lambda m: errors.append(m.text[:90]) if m.type == "error" else None,
    )
    page.goto(BASE + "/", wait_until="domcontentloaded")
    resp = page.evaluate(
        """async ([url, mode]) => {
            const r = await fetch('/new?url=' + encodeURIComponent(url) + '&mode=' + mode);
            return await r.json();
        }""",
        [URL, MODE],
    )
    if not resp.get("player"):
        print("НЕТ СЕССИИ:", json.dumps(resp, ensure_ascii=False)[:200])
        browser.close()
        sys.exit(1)

    page.goto(resp["player"], wait_until="domcontentloaded")
    page.wait_for_timeout(WAIT)
    page.evaluate("document.querySelector('video')?.play().catch(()=>{})")
    page.wait_for_timeout(2000)

    state = page.evaluate(
        """() => {
            const v = document.querySelector('video');
            if (!v) return {noVideo: true};
            return {rs: v.readyState, t: v.currentTime, w: v.videoWidth, h: v.videoHeight,
                    err: v.error ? v.error.code : null, muted: v.muted, vol: v.volume};
        }"""
    )
    levels = page.evaluate(
        """() => {
            try {
              if (!hlsInstance) return null;
              return {cur: hlsInstance.currentLevel,
                      all: hlsInstance.levels.map(function(l){
                        return l.height + 'p ' + Math.round(l.bitrate/1000) + 'kbps';
                      })};
            } catch (e) { return 'н/д'; }
        }"""
    )
    print("состояние:", json.dumps(state, ensure_ascii=False))
    print("уровни:", json.dumps(levels, ensure_ascii=False))
    print("ошибки:", errors[:3] if errors else "нет")
    if SHOT:
        page.screenshot(path=SHOT)
        print("скриншот:", SHOT)
    browser.close()
