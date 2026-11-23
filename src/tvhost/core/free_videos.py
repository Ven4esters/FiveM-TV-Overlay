"""
free_videos.py — готовые прямые ссылки на легальные видео из интернета.

Все источники — без копирайтных проблем: NASA (public domain), Wikimedia Commons
(CC-лицензии), Internet Archive (public domain / CC), демо-файлы.

Использование:
    python free_videos.py               # показать список
    python free_videos.py --check       # проверить, что все ссылки живые
    python free_videos.py --json        # машинночитаемый список

Скопируйте HTTPS-ссылку и вставьте в телевизор в GTA 5.
"""

from __future__ import annotations

import json
import sys
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

VIDEOS: list[dict[str, str]] = [
    {
        "name": "NASA — Apollo 11, высадка на Луну (mp4)",
        "url": "https://images-assets.nasa.gov/video/Apollo_11_moonwalk_montage_720p/Apollo_11_moonwalk_montage_720p~medium.mp4",
        "note": "Public domain. Проверенный 206 + video/mp4",
    },
    {
        "name": "NASA — Artemis / пуски и миссии (поиск)",
        "url": "https://images.nasa.gov/search-video?q=launch",
        "note": "Каталог: у каждого видео есть прямая mp4-ссылка в «Download»",
    },
    {
        "name": "Big Buck Bunny — Wikimedia (webm, 480p vp9)",
        "url": "https://upload.wikimedia.org/wikipedia/commons/transcoded/4/41/Big_Buck_Bunny_medium.ogv/Big_Buck_Bunny_medium.ogv.480p.vp9.webm",
        "note": "CC-BY. WebM идеален для CEF/WreckDTV",
    },
    {
        "name": "Big Buck Bunny — Wikimedia (оригинал, ogv)",
        "url": "https://upload.wikimedia.org/wikipedia/commons/4/41/Big_Buck_Bunny_medium.ogv",
        "note": "application/ogg — CEF может не понять; используйте webm-транскод выше",
    },
    {
        "name": "Internet Archive — Big Buck Bunny (mp4, redirect)",
        "url": "https://archive.org/download/BigBuckBunny_124/Content/big_buck_bunny_720p_surround.mp4",
        "note": "302 → CDN; curl/CEF обычно идут по редиректу сами",
    },
    {
        "name": "W3C — Sintel trailer (mp4, маленький)",
        "url": "https://media.w3.org/2010/05/sintel/trailer.mp4",
        "note": "Проверенный 200 video/mp4, 4.3 МБ — хороший тест связи",
    },
    {
        "name": "Mux — тестовый HLS-поток (x36xhzz)",
        "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
        "note": "НЕ прямой файл: для CEF не подойдёт без ffmpeg (см. proxy.py /r)",
    },
]

WIKI_TRANSCODE_HINT = (
    "Любое видео Wikimedia: страница файла → «Скачать» → выбрать webm 480p/720p, "
    "или открыть Commons API и взять transcoded-ссылку."
)


def check(url: str) -> tuple[bool, str]:
    """Проверить ссылку: 200/206 + тип содержимого."""
    if url.endswith(".m3u8") or "/search" in url:
        return True, "не файл (страница/поток)"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-1023"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ctype = resp.headers.get("Content-Type", "?")
            ok = resp.status in (200, 206) and ctype.startswith(("video/", "audio/"))
            return ok, f"{resp.status} {ctype}"
    except Exception as e:
        return False, str(e)[:80]


def main() -> None:
    if "--json" in sys.argv:
        print(json.dumps(VIDEOS, ensure_ascii=False, indent=1))
        return

    do_check = "--check" in sys.argv
    print("=" * 78)
    print("🎬 Прямые ссылки на легальные видео (NASA / Wikimedia / Archive / демо)")
    print("=" * 78)
    for i, v in enumerate(VIDEOS, 1):
        status = ""
        if do_check:
            ok, detail = check(v["url"])
            status = f"  [{'✅' if ok else '❌'} {detail}]"
        print(f"\n{i}. {v['name']}{status}")
        print(f"   {v['url']}")
        print(f"   ⓘ {v['note']}")
    print(f"\n💡 {WIKI_TRANSCODE_HINT}")
    print("\nСовет: вставьте ссылку в /get?url=... (proxy.py), если сайт требует заголовков.")


if __name__ == "__main__":
    main()
