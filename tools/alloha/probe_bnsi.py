"""Пробник Alloha: получить адреса потока и сохранить образец ответа.

ЗАЧЕМ ЭТО НУЖНО. Плеер Alloha не держит ссылки в разметке: адреса приходят
ответом на `POST /bnsi/movies/<id_file>` (в поле `hlsSource`). Снаружи России
этот запрос отвечает 404 «контент не найден» на любой id, поэтому у
разработчика образца нет, а без него нельзя проверить разбор. На компьютере
в России тот же запрос отдаёт данные — этот скрипт их и снимает.

ЗАПУСК (нужен только Python 3.9+, ничего ставить не надо):

    python3 tools/alloha/probe_bnsi.py "https://zlord.org/ссылка-на-страницу.html"

Скрипт сам:
  1. найдёт на странице встройку Alloha и токен сайта;
  2. получит озвучки из открытого API;
  3. скачает страницу плеера и проверит, что регионалка не мешает;
  4. достанет мету `viewporti` и соберёт заголовок `Borth`;
  5. сделает `POST /bnsi/movies/<id_file>` по каждому файлу из `fileList`;
  6. положит ответы в `alloha_probe/` рядом с собой: `*.json` — тела ответов,
     `summary.txt` — короткая сводка для отправки разработчику.

Ничего не логирует и не отправляет наружу: всё остаётся на диске.
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tvhost2.sources.alloha_borth import borth_suffix

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
OUT = Path(__file__).resolve().parent / "alloha_probe"

IFRAME_RE = re.compile(r"<iframe[^>]*?(?:data-)?src\s*=\s*[\"']([^\"']+)[\"']", re.I)
TOKEN_RE = re.compile(r"[?&]token=([0-9a-f]{20,})")
KP_RE = re.compile(r"[?&]kp=(\d+)")
META_RE = re.compile(r'name="viewporti"\s+content="([^"]+)"')
FILE_RE = re.compile(r'"id_file"\s*:\s*"?(\d+)"?')
FILE_LIST_RE = re.compile(r"fileList\s*[:=]\s*(\{.*?\})\s*[,;]\s*(?:const|var|let|\n)", re.S)


def _ctx() -> ssl.SSLContext:
    """Обычный контекст: сертификаты проверяются.

    Понадобится `--insecure`, если у заказчика старый набор корневых
    сертификатов и сайт не открывается по TLS.
    """
    return ssl.create_default_context()


CTX = _ctx()


def fetch(
    url: str,
    data: dict | None = None,
    headers: dict[str, str] | None = None,
    form: bool = False,
) -> tuple[int, bytes]:
    head = {"User-Agent": UA, "Accept": "*/*"}
    body: bytes | None = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            head["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        else:
            body = json.dumps(data).encode()
            head["Content-Type"] = "application/json"
    head.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=head, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # сеть бывает любой — код 0 значит «не дошло»
        return 0, str(e).encode()


def find_embed(page_url: str) -> tuple[str, str, str]:
    """Встройка Alloha на странице: (адрес iframe, токен сайта, kp)."""
    status, body = fetch(page_url)
    text = body.decode("utf-8", "replace")
    print(f"страница: {status}, {len(body)} байт")
    if status != 200:
        raise SystemExit(f"страница не открылась ({status}) — проверьте ссылку")
    for m in IFRAME_RE.finditer(text):
        url = m.group(1).replace("\\/", "/").strip()
        if url.startswith("//"):
            url = "https:" + url
        token = TOKEN_RE.search(url)
        kp = KP_RE.search(url)
        if not token:
            continue
        print(f"встройка: {url[:90]}")
        return url, token.group(1), (kp.group(1) if kp else "")
    raise SystemExit("на странице нет встройки Alloha с токеном")


def translations(token: str, kp: str) -> list[str]:
    """Озвучки из открытого API — они работают отовсюду."""
    url = f"https://api.alloha.tv/?token={token}" + (f"&kp={kp}" if kp else "")
    status, body = fetch(url)
    out: list[str] = []
    try:
        data = json.loads(body)
        raw = data.get("data", {}).get("translation") or ""
        out = [t.strip() for t in str(raw).split(",") if t.strip()]
    except (json.JSONDecodeError, AttributeError):
        pass
    print(f"озвучки ({status}): {', '.join(out) or '—'}")
    return out


def player_meta(embed: str, page_url: str) -> tuple[str, str]:
    """Страница плеера: проверяем регионалку и достаём мету + fileList."""
    status, body = fetch(embed, headers={"Referer": page_url})
    text = body.decode("utf-8", "replace")
    print(f"плеер: {status}, {len(body)} байт")
    if status != 200:
        low = text.lower()
        if "регион" in low or "region" in low:
            raise SystemExit("плеер закрыт по региону — запускать нужно с российского адреса")
        raise SystemExit(f"плеер не открылся ({status})")
    meta = META_RE.search(text)
    if not meta:
        raise SystemExit("в странице плеера нет меты viewporti — другая версия плеера")
    return meta.group(1), text


def file_ids(player_html: str) -> list[str]:
    """Номера файлов из fileList (в разметке только они, без адресов)."""
    ids: list[str] = []
    block = FILE_LIST_RE.search(player_html)
    text = block.group(1) if block else player_html
    for m in FILE_RE.finditer(text):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def base_of(embed: str) -> str:
    """Корень плеера без хвоста `?…` и без лишнего слэша на конце."""
    return embed.split("?", 1)[0].rstrip("/")


def guard(embed: str) -> str:
    """Токен `Authorizations` по протоколу плеера (может и не понадобиться)."""
    host = urllib.parse.urlsplit(embed).hostname or ""
    parent = "https://zlord.org"
    status, body = fetch(f"{base_of(embed)}/sarn", headers={"acceptis": f"{host}.93"})
    if status != 200:
        return ""
    try:
        challenge = json.loads(body)
        proof = hashlib.sha256(
            "|".join([challenge["nonce"], host, challenge["uap"], parent, parent]).encode()
        ).hexdigest()
        status, body = fetch(
            f"{base_of(embed)}/vorf",
            {
                "challenge": challenge["challenge"],
                "proof": proof,
                "parentOrigin": parent,
                "referrer": parent + "/",
            },
            {"accepts-block": f"{host}.93"},
        )
        return json.loads(body).get("token", "") if status == 200 else ""
    except (json.JSONDecodeError, KeyError, AttributeError):
        return ""


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    page_url = sys.argv[1].strip()
    OUT.mkdir(exist_ok=True)

    embed, token, kp = find_embed(page_url)
    tracks = translations(token, kp)

    meta, player_html = player_meta(embed, page_url)
    suffix = borth_suffix(meta)
    print(f"мета: {len(meta)} символов, Borth-суффикс собран")

    base = embed.split("?", 1)[0].rstrip("/")
    ids = file_ids(player_html)
    print(f"файлов в fileList: {len(ids)}")
    if not ids:
        raise SystemExit("не нашёл ни одного id_file — пришлите page.html плеера")

    auth = guard(embed)
    if auth:
        print(f"guard-токен получен: {len(auth)} символов")

    summary = [
        f"страница: {page_url}",
        f"встройка: {embed}",
        f"токен сайта: {token}",
        f"kp: {kp}",
        f"озвучки: {', '.join(tracks) or '—'}",
        f"файлов: {len(ids)}",
        "",
    ]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    got = 0
    for ident in ids[:8]:  # больше восьми не нужно: структура одинаковая
        for path in ("movies", "trailers"):
            url = f"{base}/bnsi/{path}/{ident}"
            payload = {"token": token, "av1": 0, "autoplay": 0}
            headers = {"Borth": f"{_fingerprint()}|{suffix}", "Referer": embed, "Origin": base}
            if auth:
                headers["Authorizations"] = f"Bearer {auth}"
            for mode, form in (("json", False), ("form", True)):
                status, body = fetch(url, payload, headers, form=form)
                text = body.decode("utf-8", "replace")
                has = "hlsSource" in text
                summary.append(
                    f"POST /bnsi/{path}/{ident} [{mode}] → {status}"
                    + ("   ← ЕСТЬ hlsSource" if has else "")
                )
                print(f"  /bnsi/{path}/{ident} [{mode}] → {status}" + ("   ← ЕСТЬ!" if has else ""))
                if has:
                    got += 1
                    fname = OUT / f"{stamp}_{path}_{ident}.json"
                    fname.write_text(text, encoding="utf-8")
                    summary.append(f"  сохранено: {fname.name}")
                    try:
                        summary.extend(_describe(json.loads(text)))
                    except json.JSONDecodeError:
                        summary.append("  (тело не JSON — сохранено как есть)")

    (OUT / f"{stamp}_summary.txt").write_text("\n".join(summary), encoding="utf-8")
    print("\n" + "\n".join(summary[-12:]))
    if got:
        print(f"\nГотово. Файлы в {OUT} — пришлите их разработчику.")
    else:
        print(
            "\nНи один запрос не отдал hlsSource. Это значит, что и с этого адреса "
            "плеер закрыт — пришлите summary.txt, посмотрю коды ответов."
        )


def _fingerprint() -> str:
    """Правдоподобный отпечаток браузера (первая половина Borth).

    Точный отпечаток считается только в браузере (canvas/audio/WebGL).
    Если сервер сверяет его строго, в ответе будет отказ — тогда смотрим по
    коду: 404 «не найден» → дело в регионе; другой код → в отпечатке.
    """
    import hashlib

    parts = [UA, "Europe/Moscow", "1920x1080", "ru-RU", "8", "8"]
    return hashlib.sha256("||".join(parts).encode()).hexdigest()


def _describe(data: dict) -> list[str]:
    """Краткая структура ответа — чтобы сводка была понятна без открытия JSON."""
    out: list[str] = []
    src = data.get("hlsSource")
    if isinstance(src, list):
        out.append(f"  hlsSource: {len(src)} озвучек")
        for item in src[:4]:
            if not isinstance(item, dict):
                continue
            quality = item.get("quality") or {}
            heights = list(quality.keys()) if isinstance(quality, dict) else []
            sample = ""
            if heights and isinstance(quality, dict):
                sample = str(quality[heights[0]])[:80]
            out.append(
                f"    • {item.get('label', '?')}: высоты {heights}, "
                f"audioId={item.get('audioId')}, адрес: {sample}…"
            )
    for key in ("pnr", "pnk"):
        if data.get(key):
            out.append(f"  {key}: есть ({len(str(data[key]))} символов)")
    return out


if __name__ == "__main__":
    main()
