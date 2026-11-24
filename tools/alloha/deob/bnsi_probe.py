"""POST /bnsi/<type>/<id> у Alloha: получаем hlsSource.

Borth = <отпечаток браузера> | <iP(iR(iS(<meta viewporti>)))>

Отпечаток считает c0() в app.js: sha256 от UA|timezone|screen|lang|
deviceMemory|cores|canvas|webgl|audio, склеенных через '||'.  Повторить его
вне браузера точно нельзя (canvas/audio), поэтому проверяем, насколько строго
сервер сверяет первую часть.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.request

BASE = "https://tories-as.stravers.live"
HOST = "tories-as.stravers.live"
PARENT = "https://zlord.org"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TOKEN = "5286c49a9d29cad731353b68d3f1f5"
MOVIE_TOKEN = "dc27f0f0f45c4989af12f74b6a7757"
ACTIVE_ID = "1836882"
ID_FILE = "1836464"


def call(url, data=None, headers=None):
    h = {"User-Agent": UA, "Origin": BASE, "Referer": BASE + "/"}
    h.update(headers or {})
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(url, data=body, headers=h,
                                 method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read(), dict(r.getheaders())
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode(), {}


def guard() -> str:
    st, b, _ = call(f"{BASE}/sarn", headers={"acceptis": f"{HOST}.93"})
    d = json.loads(b)
    proof = hashlib.sha256(
        "|".join([d["nonce"], HOST, d["uap"], PARENT, PARENT]).encode()
    ).hexdigest()
    st, b, _ = call(
        f"{BASE}/vorf",
        {"challenge": d["challenge"], "proof": proof,
         "parentOrigin": PARENT, "referrer": PARENT + "/"},
        {"content-type": "application/json", "accepts-block": f"{HOST}.93"},
    )
    return json.loads(b)["token"]


def suffix(meta: str) -> str:
    out = subprocess.run(
        ["node", "/tmp/borth3.js", meta], capture_output=True, text=True, timeout=60
    ).stdout
    m = re.search(r"^SUFFIX: (\S+)$", out, re.M)
    return m.group(1) if m else ""


def fingerprint() -> str:
    """Правдоподобный отпечаток: те же поля, что собирает c0()."""
    parts = [
        UA.replace(r"(\d+(?:[._]\d+)+)", ""),
        "Europe/Moscow",
        "1920x1080",
        "ru-RU",
        "8",
        "8",
    ]
    return hashlib.sha256("||".join(parts).encode()).hexdigest()


def main() -> None:
    html = open("/home/user/uploads/alloha_player.html", encoding="utf-8").read()
    meta = re.search(r'name="viewporti" content="([^"]+)"', html).group(1)

    # живая страница — та же мета?
    st, b, _ = call(f"{BASE}/?token={TOKEN}&token_movie={MOVIE_TOKEN}&translation=93")
    live = re.search(rb'name="viewporti" content="([^"]+)"', b)
    print(f"страница плеера: {st}, {len(b)} b, мета живая: "
          f"{len(live.group(1)) if live else 0} симв (сохранённая {len(meta)})")

    sfx = suffix(meta)
    print("суффикс Borth:", sfx[:32], "…", len(sfx))

    for part1_name, part1 in [("фингерпринт", fingerprint()), ("пусто", ""), ("ноль", "0")]:
        borth = f"{part1}|{sfx}"
        for path, ident in (("movies", ACTIVE_ID), ("movies", ID_FILE),
                            ("trailers", ACTIVE_ID)):
            url = f"{BASE}/bnsi/{path}/{ident}"
            st, b, hdrs = call(
                url,
                {"token": TOKEN, "av1": 0, "autoplay": 0},
                {"Borth": borth, "content-type": "application/json"},
            )
            txt = b[:220].decode("utf-8", "replace").replace("\n", " ")
            mark = ""
            if b"hlsSource" in b:
                open("/tmp/hls_resp.json", "wb").write(b)
                mark = "   ←←← ЕСТЬ hlsSource!"
            print(f"{part1_name:11s} POST /bnsi/{path}/{ident[:8]} → {st}"
                  f"  Borth-заголовок: {hdrs.get('Borth', '—')!r}{mark}")
            if st != 200 or mark:
                print("     ", txt)


if __name__ == "__main__":
    main()
