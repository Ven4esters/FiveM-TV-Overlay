"""
Источники видео, которые yt-dlp не умеет разбирать полностью:

* VK Видео (vkvideo.ru / m.vkvideo.ru) — yt-dlp даёт форматы, но:
    - прямые okcdn-ссылки подписаны на IP и требуют особые заголовки,
      поэтому в браузере они дают 502;
    - HLS-потоки отдаются по отдельности (видео и аудио), master нет.
  Здесь собираем СВОЙ master: видео + аудио, одно качество на высоту.

* Kodik (kodik.info / kodikplayer.com / kodik.biz) — плеер, который
  используют почти все онлайн-кинотеатры рунета. Поток отдаётся не сразу:
  на странице плеера лежат type/id/hash и подписанные urlParams, которые
  надо POST-ом отправить на /ftor, а в ответ приходят токены, зашифрованные
  «Цезарем» (сдвиг 18) + base64. Расшифровываем их сами — тогда
  можно и смотреть напрямую, и переключать озвучку.

Модуль ничего не скачивает: только получает ссылки на потоки.
"""

from __future__ import annotations

import base64
import html as html_mod
import json
import re
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TIMEOUT = 25

# Разбор ссылки стоит 3-10 секунд. Один и тот же плейлист запрашивают
# несколько раз (плеер, hls.js, вкладка «назад») — кешируем ненадолго.
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 180.0


def clear_cache() -> None:
    _CACHE.clear()


def _cached(key: str, build):
    """Взять из кеша или построить заново."""
    import time as _time

    hit = _CACHE.get(key)
    if hit and (_time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    val = build()
    if val:
        _CACHE[key] = (_time.time(), val)
    return val


def _get(
    url: str, headers: dict | None = None, data: bytes | None = None, timeout: int = TIMEOUT
) -> tuple[int, str, str]:
    """GET/POST с браузерными заголовками. Возвращает (код, тело, final_url)."""
    h = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            enc = "utf-8"
            ctype = r.headers.get("Content-Type", "") or ""
            if "windows-1251" in ctype.lower() or "cp1251" in ctype.lower():
                enc = "cp1251"
            return r.status, raw.decode(enc, "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), url


# ======================================================================
#                              VK ВИДЕО
# ======================================================================


def is_vk(url: str) -> bool:
    """Ссылка на VK: страница или CDN-поток (okcdn)."""
    low = (url or "").lower()
    return any(d in low for d in ("vkvideo.ru/", "vk.com/video", "vk.ru/video", "okcdn.ru"))


def is_rutube(url: str) -> bool:
    """Ссылка на Rutube (страница видео или встраиваемый плеер)."""
    low = (url or "").lower()
    return any(
        d in low
        for d in (
            "rutube.ru/video",
            "rutube.ru/play/embed",
            "rutube.ru/embed",
            "rutube.ru/metainfo",
        )
    )


def rutube_stream(url: str) -> dict | None:
    """
    Поток Rutube одной HLS-дорожкой (видео + звук вместе).

    Без этого Rutube попадал в «универсальный поиск потока», который
    вытаскивал только видео: получалось «0p без звука».

    Возвращает {"url": <m3u8>, "title": str, "qualities": [...]} или None.
    """
    return _cached("rutube:" + url, lambda: _rutube_stream_uncached(url))


def _rutube_stream_uncached(url: str) -> dict | None:
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 1,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return None
    if not info:
        return None

    title = info.get("title") or ""

    # 1) готовая мультиплексная HLS-дорожка (видео + звук)
    hls_mux = info.get("hls_manifest_url")
    if hls_mux:
        return {"url": hls_mux, "title": title, "qualities": []}

    # 2) собираем ВСЕ качества в один master-плейлист.
    #    ВАЖНО: раньше бралось только максимальное (1080p). На медленном
    #    канале/перегруженном CDN сегменты 1080p (6.8 Мбит/с) не успевали
    #    скачаться за 4 секунды — hls.js падал с manifestLoadError.
    #    Теперь hls.js сам выбирает качество по скорости и переключается.
    heights: dict[int, str] = {}
    files_map: dict[str, str] = {}
    for f in info.get("formats") or []:
        if "m3u8" not in (f.get("protocol") or ""):
            continue
        if not f.get("url"):
            continue
        if (f.get("vcodec") or "none") == "none":
            continue
        h = int(f.get("height") or 0)
        if not h:
            continue
        heights.setdefault(h, f["url"])
        # имя файла качества (360p_stream.m3u8) — чтобы /hls мог подставить
        # любое качество, не перезапрашивая yt-dlp
        m = re.search(r"(\d+p_stream\.m3u8)", f["url"])
        if m:
            files_map[str(h)] = m.group(1)

    if heights:
        return {
            "url": heights[max(heights)],  # по умолчанию — лучшее
            "title": title,
            "qualities": sorted(heights, reverse=True),
            "variants": {str(h): u for h, u in sorted(heights.items(), reverse=True)},
            "quality_files": files_map,
            "master_base": heights[max(heights)].rsplit("/", 1)[0] + "/",
        }

    # 3) последний шанс: любой m3u8, затем лучший mp4
    for f in reversed(info.get("formats") or []):
        if "m3u8" in (f.get("protocol") or "") and f.get("url"):
            return {"url": f["url"], "title": title, "qualities": []}
    for f in reversed(info.get("formats") or []):
        if (
            f.get("ext") == "mp4"
            and f.get("url")
            and (f.get("acodec") or "none") != "none"
            and (f.get("vcodec") or "none") != "none"
        ):
            return {"url": f["url"], "title": title, "qualities": []}
    return None


def _vk_formats(url: str) -> list[dict]:
    """
    Все форматы yt-dlp с сохранением URL и заголовков.

    Пустая или невалидная ссылка отсекается до сети: иначе yt-dlp бросает
    DownloadError, и вместо понятной ошибки пользователь видит трейсбек.
    """
    if not (url or "").strip():
        return []
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "all",
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 1,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    out: list[dict] = []
    for f in info.get("formats") or []:
        if not f.get("url"):
            continue
        out.append(
            {
                "id": f.get("format_id") or "",
                "url": f.get("url"),
                "protocol": (f.get("protocol") or ""),
                "height": f.get("height") or 0,
                "tbr": f.get("tbr") or 0,
                "vcodec": f.get("vcodec") or "none",
                "acodec": f.get("acodec") or "none",
                "ext": f.get("ext") or "",
                "headers": f.get("http_headers") or {},
            }
        )
    return out


def vk_master(url: str) -> dict | None:
    """
    Собрать master-плейлист для VK: по одному видео-потоку на каждую высоту
    + аудио-дорожка рядом (так hls.js получит и картинку, и звук).

    Возвращает {"playlist": "...", "audio": url|None, "qualities": [360, 480, ...]}
    или None, если разобрать не удалось.
    """
    return _cached("vk:" + url, lambda: _vk_master_uncached(url))


def _vk_master_uncached(url: str) -> dict | None:
    fmts = _vk_formats(url)
    if not fmts:
        return None

    hls = [f for f in fmts if "m3u8" in f["protocol"]]
    # мультиплексы (видео+звук сразу) — идеальный вариант
    mux = {
        f["height"]: f
        for f in hls
        if f["height"] and f["vcodec"] != "none" and f["acodec"] != "none"
    }
    # раздельные одиночные потоки ("hls-NNN" без кодеков в метаданных)
    video = {f["height"]: f for f in hls if f["height"] and f["vcodec"] == "none"}
    audio = [f for f in hls if f["vcodec"] == "none" and not f["height"]]

    if len(mux) >= 2:
        variants = mux
        audio_url = None
    elif len(video) >= 2:
        variants = video
        audio_url = audio[0]["url"] if audio else None
    else:
        return None

    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    if audio_url:
        lines += [
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Русский",'
            'DEFAULT=YES,AUTOSELECT=YES,URI="' + _q(audio_url) + '"',
        ]
    for h in sorted(variants, reverse=True):
        f = variants[h]
        br = int(f["tbr"] or max(400, h * 4.5))
        width = int(h * 16 / 9)
        if audio_url:
            lines.append(
                f'#EXT-X-STREAM-INF:BANDWIDTH={br * 1000},RESOLUTION={width}x{h},AUDIO="aud"'
            )
        else:
            lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={br * 1000},RESOLUTION={width}x{h}")
        lines.append(_q(f["url"]))
    return {
        "playlist": "\n".join(lines) + "\n",
        "audio": audio_url,
        "qualities": sorted(variants, reverse=True),
    }


def _q(u: str) -> str:
    """Обернуть ссылку в /hls-прокси (локальный маршрут)."""
    return f"/hls?url={urllib.parse.quote(u, safe='')}"


# ======================================================================
#                                TWITCH
# ======================================================================

# Публичный Client-ID веб-плеера Twitch (тот же, что отдаёт сам twitch.tv).
_TWITCH_CID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_TWITCH_GQL = "https://gql.twitch.tv/gql"
_TWITCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def is_twitch(url: str) -> bool:
    """Ссылка на Twitch: канал, VOD, клип или готовый поток ttvnw."""
    low = (url or "").lower()
    return any(d in low for d in ("twitch.tv/", "ttvnw.net/", "player.twitch.tv/"))


def _twitch_parse(url: str) -> dict:
    """
    Разобрать ссылку Twitch.

    Возвращает {"kind": "vod"|"channel"|"clip", "id": ..., "channel": ...}
    """
    parts = urllib.parse.urlsplit(url or "")
    path = parts.path.strip("/")
    qs = urllib.parse.parse_qs(parts.query)

    # player.twitch.tv/?video=123 или ?channel=name
    if "player.twitch.tv" in (parts.hostname or ""):
        vid = (qs.get("video") or [""])[0]
        ch = (qs.get("channel") or [""])[0]
        if vid.isdigit():
            return {"kind": "vod", "id": vid}
        return {"kind": "channel", "channel": ch}

    segs = [x for x in path.split("/") if x]
    if not segs:
        return {}

    # clips: /<channel>/clip/<slug>  или  clips.twitch.tv/<slug>
    if "clip" in segs:
        i = segs.index("clip")
        return {
            "kind": "clip",
            "id": segs[i + 1] if i + 1 < len(segs) else "",
            "channel": segs[0] if i else "",
        }
    if (parts.hostname or "").startswith("clips."):
        return {"kind": "clip", "id": segs[0]}

    # VOD: /videos/<numeric id>
    if segs[0] == "videos" and len(segs) > 1:
        vid = re.sub(r"\D", "", segs[1])
        return {"kind": "vod", "id": vid}

    # канал: /<login>
    return {"kind": "channel", "channel": segs[0].lower()}


def _twitch_gql(query: str, variables: dict) -> dict:
    """Запрос к публичному GraphQL Twitch."""
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        _TWITCH_GQL,
        data=body,
        headers={
            "Client-ID": _TWITCH_CID,
            "Content-Type": "application/json",
            "User-Agent": _TWITCH_UA,
            "Origin": "https://www.twitch.tv",
            "Referer": "https://www.twitch.tv/",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


# GraphQL-запрос, получающий токен вещания (не секрет: публичный API).
TWITCH_TOKEN_QUERY = """query($id: ID!){ videoPlaybackAccessToken(id: $id,
   params:{platform:"web",playerBackend:"mediaplayer",playerType:"site"})
   { value signature } }"""


def twitch_playlist(url: str) -> dict | None:
    """
    HLS-плейлист Twitch.

    Возвращает {"playlist": <master m3u8>, "qualities": [1080, 720, ...],
                "title": ..., "kind": "vod"|"channel"|"clip", "live": bool}

    Для VOD и канала схема одна: получить playback access token через
    GraphQL, затем попросить usher.ttvnw.net собрать master-плейлист.
    Разбор по типу ссылки — в отдельных функциях: у VOD, канала и клипа
    разные запросы и разные ответы.
    """
    parsed = _twitch_parse(url)
    if not parsed:
        return None

    kind = str(parsed.get("kind") or "")
    if kind == "clip":
        return _twitch_playlist_clip(parsed)
    source = _twitch_source_vod(parsed) if kind == "vod" else _twitch_source_channel(parsed)
    if source is None:
        return None
    return _twitch_fetch_master(source, kind)


def _twitch_source_vod(parsed: dict) -> dict | None:
    """Данные для мастера записи (VOD): адрес usher, токен, заголовок, тип."""
    video_id = parsed.get("id") or ""
    if not video_id:
        return None

    data = _twitch_gql(
        "query($id: ID!){ video(id: $id){ title lengthSeconds broadcastType } }",
        {"id": video_id},
    )
    payload = data.get("data") or {}
    meta = payload.get("video") or {}
    token = payload.get("videoPlaybackAccessToken")
    if not token:
        extra_data = _twitch_gql(TWITCH_TOKEN_QUERY, {"id": video_id})
        token = (extra_data.get("data") or {}).get("videoPlaybackAccessToken")
    if not token:
        return None

    return {
        "base": f"https://usher.ttvnw.net/vod/{video_id}.m3u8",
        "token": token,
        "title": meta.get("title") or f"Twitch VOD {video_id}",
        "extra": "&transcode_mode=critical_v1_vod_playlist",
        "live": False,
    }


def _twitch_source_channel(parsed: dict) -> dict | None:
    """Данные для мастера канала (прямой эфир)."""
    channel = parsed.get("channel") or ""
    if not channel:
        return None

    data = _twitch_gql(
        """query($ch: String!){
             user(login: $ch){ displayName stream{ id } }
             streamPlaybackAccessToken(channelName: $ch,
               params:{platform:"web",playerBackend:"mediaplayer",playerType:"site"})
             { value signature } }""",
        {"ch": channel},
    )
    payload = data.get("data") or {}
    token = payload.get("streamPlaybackAccessToken")
    user = payload.get("user") or {}
    if not token:
        if not user.get("stream"):
            raise RuntimeError("канал не в эфире")
        return None

    return {
        "base": f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8",
        "token": token,
        "title": f"Twitch · {user.get('displayName') or channel}",
        "extra": "",
        "live": True,
    }


def _twitch_playlist_clip(parsed: dict) -> dict | None:
    """Клип: у Twitch нет HLS-мастера — собираем из прямых ссылок."""
    slug = parsed.get("id") or ""
    if not slug:
        return None
    data = _twitch_gql(
        """query($slug: ID!){ clip(slug: $slug){
             title durationSeconds playbackAccessToken{ value signature } } }""",
        {"slug": slug},
    )
    clip = (data.get("data") or {}).get("clip") or {}
    return _twitch_clip(clip, slug)


def _twitch_fetch_master(source: dict, kind: str) -> dict | None:
    """Запросить master-плейлист и переписать ссылки на локальный прокси."""
    token = source["token"]
    query = (
        "allow_source=true&allow_audio_only=true&p=1234567&player=twitchweb"
        "&playlist_include_framerate=true&reassignments_supported=true"
        f"&sig={token['signature']}&supported_codecs=avc1"
        f"&token={urllib.parse.quote(token['value'])}{source['extra']}"
    )
    code, master, _ = _get(
        f"{source['base']}?{query}",
        {"Referer": "https://player.twitch.tv/", "Origin": "https://player.twitch.tv"},
    )
    if code != 200 or "#EXTM3U" not in (master or ""):
        return None

    qualities = sorted(
        {int(m.group(1)) for m in re.finditer(r"RESOLUTION=\d+x(\d+)", master)},
        reverse=True,
    )
    return {
        "playlist": _twitch_proxify_playlist(master),
        "qualities": qualities,
        "title": source["title"],
        "kind": kind,
        "live": source["live"],
    }


def _twitch_proxify_playlist(master: str) -> str:
    """
    Переписать ссылки на качества в master-плейлисте на локальный /hls.

    Ссылки внутри мастера абсолютные (ttvnw.net). Браузер в игре не сможет
    их взять напрямую из-за CORS, поэтому каждую заворачиваем в /hls-мост.
    Служебные #EXT-X-TWITCH-INFO и подобные — короткие, без ссылок,
    оставляем как есть; многокилобайтные теги выбрасываем.
    """
    lines: list[str] = []
    for line in master.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if len(stripped) > 800:
                continue  # служебный тег Twitch на несколько КБ
            lines.append(line)
            continue
        if not stripped:
            continue
        if "//" in stripped or stripped.startswith("/"):
            lines.append(_q(stripped))
    return "\n".join(lines) + "\n"


def _twitch_clip(clip: dict, slug: str) -> dict | None:
    """Клип Twitch — обычно прямой mp4, оборачиваем в однокачественный m3u8."""
    data = _twitch_gql(
        """query($slug: ID!){ clip(slug: $slug){
             videoQualities{ frameRate quality sourceURL } } }""",
        {"slug": slug},
    )
    quals = ((data.get("data") or {}).get("clip") or {}).get("videoQualities") or []
    if not quals:
        return None
    quals = sorted(quals, key=lambda q: int(q.get("quality") or 0), reverse=True)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for q in quals:
        h = int(q.get("quality") or 0)
        url = q.get("sourceURL") or ""
        if not url:
            continue
        br = int(h * 4.5 * 1000)
        lines += [f"#EXT-X-STREAM-INF:BANDWIDTH={br},RESOLUTION={int(h * 16 / 9)}x{h}", _q(url)]
    if len(lines) <= 2:
        return None
    return {
        "playlist": "\n".join(lines) + "\n",
        "qualities": [int(q.get("quality") or 0) for q in quals],
        "title": clip.get("title") or f"Twitch clip {slug}",
        "kind": "clip",
        "live": False,
    }


# ======================================================================
#                                KODIK
# ======================================================================

_KODIK_HOSTS = (
    "kodik.info",
    "kodikplayer.com",
    "kodik.biz",
    "kodik.cc",
    "kodik.vip",
    "kodik.online",
    "kodik.st",
    "kodik.pw",
)

# ссылка вида /serial/45534/d8619e.../720p или /video/91873/060cab.../720p
_KODIK_LINK_RE = re.compile(
    r"(?:https?:)?//(" + "|".join(h.replace(".", r"\.") for h in _KODIK_HOSTS) + r")"
    r"/(serial|video|seria|movie)/(\d+)/([a-f0-9]{20,})(?:/(\d+)p)?",
    re.I,
)


def is_kodik(url: str) -> bool:
    return bool(_KODIK_LINK_RE.search(url or ""))


def rot18(text: str) -> str:
    """Шифр Цезаря со сдвигом 18 (латиница), как в плеере Kodik."""
    out = []
    for ch in text:
        c = ord(ch)
        if 65 <= c <= 90:
            out.append(chr((c - 65 + 18) % 26 + 65))
        elif 97 <= c <= 122:
            out.append(chr((c - 97 + 18) % 26 + 97))
        else:
            out.append(ch)
    return "".join(out)


def decode_src(token: str) -> str:
    """Токен Kodik -> настоящая ссылка на поток."""
    if "//" in token[:8]:
        return token  # уже открытая ссылка
    s = rot18(token)
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s).decode("utf-8", "replace")
    except Exception:
        return ""


def _kodik_page(player_url: str, referer: str = "") -> dict | None:
    """
    Вытащить со страницы плеера type, id, hash, urlParams и озвучку.

    referer — адрес сайта-партнёра (например https://animego.tv/). Kodik
    подписывает запросы ровно под тот домен, с которого пришёл запрос,
    поэтому для /ftor нужен тот же referer, что и при открытии плеера.
    """
    code, page, final = _get(player_url, {"Referer": referer or "https://kodikplayer.com/"})
    if code != 200 or not page:
        return None

    def grab(pattern: str, default: str = "") -> str:
        m = re.search(pattern, page, re.S)
        return m.group(1) if m else default

    m = _KODIK_LINK_RE.search(player_url) or _KODIK_LINK_RE.search(final)
    kind = grab(r'var\s+type\s*=\s*"([^"]+)"') or (m.group(2) if m else "")
    vid = grab(r'var\s+videoId\s*=\s*"([^"]+)"') or (m.group(3) if m else "")
    vhash = (m.group(4) if m else "") or grab(r'hash["\']?\s*[:=]\s*["\']([a-f0-9]{20,})')
    params = grab(r"var\s+urlParams\s*=\s*'([^']+)'")
    tr_id = grab(r'var\s+translationId\s*=\s*"?(\d+)')
    tr_title = grab(r'var\s+translationTitle\s*=\s*"([^"]*)"')
    if not (kind and vid and vhash and params):
        return None
    return {
        "type": "video" if kind in ("video", "movie") else "serial",
        "id": vid,
        "hash": vhash,
        "urlParams": params,
        "translation_id": int(tr_id) if tr_id else 0,
        "translation": html_mod.unescape(tr_title),
        "host": final.split("/")[2] if "//" in final else "kodikplayer.com",
        # домен, под который Kodik подписал запрос — нужен для /ftor
        "ref": referer or "https://kodikplayer.com/",
    }


_OPTION_RE = re.compile(r"<option\b([^>]*)>(.*?)</option>", re.I | re.S)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')


# Известные сайты-партнёры Kodik. Kodik выдаёт доменные подписи ровно под
# тот домен, с которого запрошен плеер; подставляя Referer партнёра, мы
# получаем рабочий /ftor даже когда в руках только ссылка на iframe.
# Порядок важен: сначала самые частые.
_KODIK_PARTNERS = (
    "https://animego.tv/",  # живое зеркало animego (проверено 2026)
    "https://animego.me/",
    "https://animego.org/",
    "https://kinogo.biz/",
    "https://hdrezka.ag/",
    "https://rezka.ag/",
)


def kodik_guess_referer(player_url: str) -> str:
    """
    Подобрать сайт-партнёр для плеера Kodik.

    Если в ссылке уже есть параметр ref= — берём его, иначе пробуем по
    очереди известные каталоги. Возвращает "" если ничего не подошло.
    """
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(player_url).query)
    for key in ("ref", "ref_url", "referer"):
        val = (qs.get(key) or [""])[0]
        if val.startswith("http"):
            parts = urllib.parse.urlsplit(val)
            return f"{parts.scheme}://{parts.netloc}/"
    # Ничего не нашли — пусть решает каскад в kodik_streams (_KODIK_PARTNERS).
    return ""


def kodik_translations(player_url: str) -> list[dict]:
    """
    Все озвучки, которые предлагает плеер Kodik.

    В HTML плеера есть скрытый <select> со списком переводов:
    у каждого свой id перевода, id медиа и хеш. Это единственный
    способ переключать озвучку — /ftor различает их по этим полям.
    """
    code, page, _ = _get(player_url, {"Referer": "https://kodikplayer.com/"})
    if code != 200 or not page:
        return []

    # работаем только с блоком озвучек, чтобы не поймать другие <option>
    block_start = page.find("movie-translations-box")
    if block_start == -1:
        block_start = page.find("serial-translations-box")
    chunk = page[block_start : block_start + 30000] if block_start != -1 else page

    out: list[dict] = []
    seen: set[str] = set()
    for m in _OPTION_RE.finditer(chunk):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(m.group(1))}
        mid = attrs.get("data-media-id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        kind = (attrs.get("data-translation-type") or "voice").lower()
        title = html_mod.unescape(attrs.get("data-title") or m.group(2) or "").strip()
        tid = attrs.get("data-id") or attrs.get("value") or ""
        if not title:
            title = f"Озвучка {len(out) + 1}"
        out.append(
            {
                "id": int(tid) if str(tid).isdigit() else 0,
                "media_id": mid,
                "hash": attrs.get("data-media-hash", ""),
                "type": kind,
                "title": title + (" (субтитры)" if kind == "subtitles" else ""),
            }
        )
    return out


def kodik_streams(
    player_url: str, translation_id: int | None = None, referer: str = ""
) -> dict | None:
    """
    Ссылки на потоки Kodik.

    Возвращает:
      {"qualities": {720: "https://...m3u8", ...},
       "translation": "Дубляж", "translation_id": 869}
    """
    key = f"kodik:{player_url}:{translation_id}:{referer}"
    return _cached(key, lambda: _kodik_streams_uncached(player_url, translation_id, referer))


def _kodik_signs(page: str) -> dict:
    """
    Поля-подписи, которые плеер шлёт в /ftor.

    Kodik подписывает запрос парой «домен-партнёр» и «домен-плеер»:
    d/d_sign, pd/pd_sign, ref/ref_sign. Все они лежат прямо в HTML
    плеера как `var <name> = "..."`, поэтому браузер не нужен.
    """
    out = {}
    for name in ("domain", "d_sign", "pd", "pd_sign", "ref", "ref_sign"):
        m = re.search(rf'var\s+{name}\s*=\s*"([^"]*)"', page)
        out[name] = m.group(1) if m else ""
    return out


def _kodik_url_kind(player_url: str, fallback: str = "video") -> str:
    """
    Тип медиа ровно так, как он записан в URL плеера.

    /ftor различает "seria" (эпизод сериала), "serial" (сериал целиком)
    и "video" (фильм). Нормализованный type из _kodik_page ("serial")
    не подходит: для ссылок вида /seria/<id>/<hash>/ нужен "seria".
    """
    m = re.search(r"/(seria|serial|video|movie)/", player_url or "")
    if m:
        return "video" if m.group(1) == "movie" else m.group(1)
    return fallback


def _kodik_ftor(
    player_url: str, info: dict, media_id: str, media_hash: str, kind: str
) -> list[dict]:
    """
    Запрос к /ftor. Возвращает список потоков [{"url": ..., "quality": 720, ...}].

    Современный Kodik (2025+) требует доменные подписи и не принимает
    translationId в теле формы — озвучка определяется только хешем медиа.
    """
    ref_page = info.get("ref") or "https://kodikplayer.com/"
    code, page, _ = _get(player_url, {"Referer": ref_page})
    if code != 200 or not page:
        return []
    sg = _kodik_signs(page)
    host = info.get("host") or "kodikplayer.com"
    ref = sg.get("ref") or "https://kodikplayer.com/"

    forms = []
    for info_val in ("{}", info.get("urlParams") or "{}"):
        f = {
            "d": sg.get("domain", ""),
            "d_sign": sg.get("d_sign", ""),
            "pd": sg.get("pd", host),
            "pd_sign": sg.get("pd_sign", ""),
            "ref": ref,
            "ref_sign": sg.get("ref_sign", ""),
            "bad_user": "false",
            "cdn_is_working": "false",
            "type": kind,
            "hash": media_hash,
            "id": media_id,
            "info": info_val,
        }
        if not f["d"]:  # старый вариант без подписей
            f = {
                "type": kind,
                "hash": media_hash,
                "id": media_id,
                "ids": media_id,
                "translationId": str(info.get("translation_id") or ""),
                "urlParams": info.get("urlParams") or "{}",
                "badUser": "false",
                "isCdn": "false",
            }
        forms.append(f)

    for form in forms:
        code, body, _ = _get(
            f"https://{host}/ftor",
            {
                "Origin": f"https://{host}",
                "Referer": player_url,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            urllib.parse.urlencode(form).encode(),
        )
        if code != 200:
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        links = data.get("links") or {}
        if not links:
            continue
        out = []
        for q, items in links.items():
            if not isinstance(items, list) or not items:
                continue
            try:
                qn = int(q)
            except ValueError:
                continue
            for it in items:
                src = decode_src(it.get("src") or "")
                if src:
                    out.append({"url": src, "quality": qn})
        if out:
            return out
    return []


def _kodik_streams_uncached(
    player_url: str, translation_id: int | None = None, referer: str = ""
) -> dict | None:
    info = _kodik_page(player_url, referer)

    # Если выбран конкретный перевод — берём его id/хеш, не полагаясь на сам плеер
    trs = kodik_translations(player_url)
    wanted = None
    if translation_id is not None:
        wanted = next((t for t in trs if t["id"] == translation_id), None)

    if not info:
        # страница плеера могла отдать неполные данные — пробуем по переводам
        if not wanted or not trs:
            return None
        page_kind = "serial" if "/serial/" in player_url or "/seria/" in player_url else "video"
        info = {
            "type": page_kind,
            "host": "kodikplayer.com",
            "ref": "",
            "urlParams": "{}",
            "translation_id": wanted["id"],
            "translation": wanted["title"],
        }

    # /ftor требует тип ровно как в URL: seria / serial / video
    kind = _kodik_url_kind(player_url, info.get("type") or "video")
    candidates: list[dict] = []

    if wanted:
        candidates.append(
            {
                "media_id": wanted["media_id"],
                "hash": wanted["hash"],
                "title": wanted["title"],
                "tid": wanted["id"],
            }
        )
    else:
        candidates.append(
            {
                "media_id": info["id"],
                "hash": info["hash"],
                "title": info.get("translation") or "",
                "tid": info.get("translation_id"),
            }
        )
        # запас: если у основной озвучки потока нет, пробуем остальные
        for t in trs:
            if t["media_id"] != info["id"]:
                candidates.append(
                    {
                        "media_id": t["media_id"],
                        "hash": t["hash"],
                        "title": t["title"],
                        "tid": t["id"],
                    }
                )

    # Сайты-партнёры для перебора: явный referer, затем известные каталоги
    partners: list[str] = []
    if referer:
        partners.append(referer)
    for base in _KODIK_PARTNERS:
        if base not in partners:
            partners.append(base)

    for ref in partners:
        page_info = info if ref == (info.get("ref") or "") else _kodik_page(player_url, ref)
        if not page_info:
            continue
        for cand in candidates:
            streams = _kodik_ftor(player_url, page_info, cand["media_id"], cand["hash"], kind)
            if not streams:
                continue
            qualities: dict[int, str] = {}
            for st in streams:
                qualities.setdefault(st["quality"], st["url"])
            return {
                "qualities": dict(sorted(qualities.items(), reverse=True)),
                "translation": cand["title"] or (page_info.get("translation") or ""),
                "translation_id": cand["tid"],
                "referer": ref,
            }
    return None


def _kodik_streams_uncached_old(player_url: str, translation_id: int | None = None) -> dict | None:
    info = _kodik_page(player_url)
    if not info:
        return None
    tid = translation_id if translation_id is not None else info["translation_id"]

    # Если выбрана другая озвучка — берём её собственные id и хеш
    media_id = info["id"]
    media_hash = info["hash"]
    tr_title = info["translation"]
    if translation_id is not None and translation_id != info["translation_id"]:
        for tr in kodik_translations(player_url):
            if tr["id"] == translation_id:
                media_id, media_hash, tr_title = tr["media_id"], tr["hash"], tr["title"]
                break

    form = {
        "type": info["type"],
        "hash": media_hash,
        "id": media_id,
        "ids": media_id,
        "translationId": str(tid),
        "urlParams": info["urlParams"],
        "badUser": "false",
        "isCdn": "false",
    }
    host = info["host"]
    code, body, _ = _get(
        f"https://{host}/ftor",
        {
            "Origin": f"https://{host}",
            "Referer": f"https://{host}/",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        urllib.parse.urlencode(form).encode(),
    )
    if code != 200:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None

    qualities: dict[int, str] = {}
    for q, items in (data.get("links") or {}).items():
        if not isinstance(items, list) or not items:
            continue
        try:
            qn = int(q)
        except ValueError:
            continue
        url = decode_src(items[0].get("src") or "")
        if url:
            qualities[qn] = url
    if not qualities:
        return None
    return {
        "qualities": dict(sorted(qualities.items(), reverse=True)),
        "translation": tr_title,
        "translation_id": tid,
    }


def _real_height(url: str, claimed: int) -> int:
    """
    Настоящее качество потока по его адресу.

    Kodik иногда отдаёт под ключом «720» ссылку на файл 480.mp4 — если
    720-й версии у видео нет. Если этому верить, hls.js получает поток,
    который не совпадает с заявленным RESOLUTION, и падает с
    LevelLoadError при переключении. Поэтому берём высоту из имени файла.
    """
    m = re.search(r"/(\d{3,4})\.mp4", url or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return claimed


def _real_width(height: int) -> int:
    """Ширина под высоту кадра. Стандартные 16:9 — кроме 480p у Kodik."""
    if height >= 720:
        return int(round(height * 16 / 9))
    if height >= 480:
        return 852
    if height >= 360:
        return 640
    return int(round(height * 16 / 9))


def kodik_master(
    player_url: str, translation_id: int | None = None, referer: str = ""
) -> dict | None:
    """Master-плейлист из отдельных потоков Kodik (360/480/720)."""
    st = kodik_streams(player_url, translation_id, referer)
    if not st or len(st["qualities"]) < 1:
        return None
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    headers_hint = f"#EXT-X-KODIK-REFERER:{urllib.parse.quote(player_url, safe='')}"
    lines.append(headers_hint)

    # Оставляем только НАСТОЯЩИЕ качества и без дублей ссылок:
    # одна и та же ссылка не должна попадать в два варианта.
    seen_urls: set[str] = set()
    variants: list[tuple[int, str]] = []
    for claimed, url in st["qualities"].items():
        if url in seen_urls:
            continue
        seen_urls.add(url)
        real = _real_height(url, int(claimed))
        if any(h == real for h, _ in variants):
            continue
        variants.append((real, url))
    variants.sort(key=lambda x: x[0], reverse=True)

    for h, url in variants:
        br = max(400, int(h * 4.5))
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={br * 1000},RESOLUTION={_real_width(h)}x{h}")
        lines.append(_q(url))
    return {
        "playlist": "\n".join(lines) + "\n",
        "qualities": [h for h, _ in variants],
        "translation": st["translation"],
    }


# ======================================================================
#                     СТРАНИЦА САЙТА -> ПЛЕЕР
# ======================================================================

# Каталоги аниме, которые отдают плеер Kodik из JS (iframe нет в HTML).
_ANIME_SITES = (
    "animego.tv",
    "animego.me",
    "animego.org",
    "animego.one",
    "animevost.org",
    "anilibria.tv",
    "jut.su",
)


def is_anime_page(url: str) -> bool:
    """Ссылка на страницу аниме-каталога (плеер там вставляется скриптом)."""
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in _ANIME_SITES)


def animego_server_id(page_url: str) -> str:
    """
    ID аниме на animego — последние цифры в адресе страницы.

    Пример: https://animego.tv/anime/monolog-farmacevta-3-season-8300 -> 8300
    """
    m = re.search(r"/anime/[^/?#]+-(\d+)/?(?:[?#]|$)", page_url or "")
    return m.group(1) if m else ""


def animego_kodik_links(page_url: str) -> list[dict]:
    """
    Все озвучки страницы animego как ссылки Kodik.

    Каждая озвучка каталога — это отдельный медиа-хеш Kodik, поэтому
    по ссылкам из /api/player можно сразу собрать полный список.
    """
    return [
        {
            "url": p["url"],
            "title": (p.get("title") or "").replace("Kodik · ", ""),
            "episodes": p.get("episodes", 0),
        }
        for p in animego_players(page_url)
        if p.get("url")
    ]


def animego_players(page_url: str) -> list[dict]:
    """
    Плееры со страницы animego.

    animego вставляет iframe скриптом, поэтому в HTML плееров нет.
    Зато у каталога есть открытый эндпоинт POST /api/player с параметром
    id (число из адреса страницы) — он возвращает все озвучки и по
    каждой готовые ссылки Kodik на каждую серию.

    Возвращает [{"url": <плеер Kodik>, "title": "Kodik · AniMaunt",
                 "page": <адрес страницы>, "translations": [...], "episodes": N}]
    """
    aid = animego_server_id(page_url)
    if not aid:
        return []
    host = (urllib.parse.urlsplit(page_url).hostname or "").lstrip(".")
    if not host:
        return []

    # 1) Сначала родной домен: ID аниме уникален только внутри него.
    data = _animego_api(host, aid)
    if data:
        return _animego_build(data, page_url, f"https://{host}/")

    # 2) Родной домен не ответил (сайт лёг). У зеркал ДРУГИЕ ID, поэтому
    #    переносить число нельзя — ищем аниме по названию из адреса.
    slug = _animego_slug(page_url)
    if not slug:
        return []
    for dom in ("animego.tv", "animego.org", "animego.one", "animego.me"):
        if dom == host:
            continue
        real_id = _animego_find_id(dom, slug)
        if not real_id:
            continue
        data = _animego_api(dom, real_id)
        if data:
            return _animego_build(data, page_url, f"https://{dom}/")
    return []


def _animego_slug(page_url: str) -> str:
    """Название аниме из адреса: .../anime/monolog-farmatsevta-3-3803 -> monolog-farmatsevta-3"""
    m = re.search(r"/anime/([^/?#]+?)-(\d+)/?(?:[?#]|$)", page_url or "")
    return m.group(1) if m else ""


def _animego_api(dom: str, anime_id: str) -> dict | None:
    """POST /api/player на конкретном домене. Возвращает JSON или None."""
    base = f"https://{dom}"
    code, body, _ = _get(
        f"{base}/api/player",
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{base}/",
            "Origin": base,
        },
        urllib.parse.urlencode({"id": anime_id}).encode(),
    )
    if code != 200:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    return data if data.get("translations") else None


def _animego_find_id(dom: str, slug: str) -> str:
    """
    Найти ID аниме на зеркале по названию из исходного адреса.

    Ищем страницу /anime/<slug> прямым перебором вариантов окончания —
    у зеркал суффиксы отличаются (…-3-season-8300 против …-3-3803).
    """
    base = f"https://{dom}"
    stem = re.sub(r"-\d+$", "", slug)
    # Полное имя для сравнения: номер сезона/части — важный признак,
    # поэтому из slug убираем только сам ID (последнее число), если он там есть.
    full = slug

    # 1) прямой перебор адресов (дешёво, часто хватает)
    for v in dict.fromkeys((slug, stem, f"{stem}-season", f"{stem}-tv", f"{stem}-season-1")):
        code, body, final = _get(f"{base}/anime/{v}", {"Referer": f"{base}/"})
        # только реально существующая страница: на 404 animego отдаёт
        # адрес с цифрой, и можно принять «3» из «…-3» за ID аниме
        if code == 200 and "/anime/" in final and "Страница не найдена" not in (body or ""):
            found = animego_server_id(final)
            if found:
                return found

    # 2) поисковый API каталога.
    #    Названия на зеркалах пишут по-разному («farmatsevta» против
    #    «farmacevta»), поэтому точная фраза не находится. Ищем по словам,
    #    от длинных к коротким, и сравниваем без гласных-ошибок.
    words = [w for w in full.split("-") if len(w) >= 4]
    queries = [full.replace("-", " ")]
    queries += [w for w in sorted(words, key=len, reverse=True)]
    queries += [w for w in words]

    # Собираем карточки из ВСЕХ запросов: одни слова находятся, другие нет,
    # и останавливаться на первом непустом ответе нельзя.
    seen: dict[str, str] = {}
    for q in dict.fromkeys(queries):
        code, body, _ = _get(
            f"{base}/api/fastsearch?q={urllib.parse.quote(q)}",
            {"X-Requested-With": "XMLHttpRequest", "Referer": f"{base}/"},
        )
        if code != 200 or not body:
            continue
        html = ""
        try:
            html = json.loads(body).get("html") or ""
        except json.JSONDecodeError:
            html = body
        for name, num in re.findall(r'href="/anime/([a-z0-9-]+?)-(\d{2,6})/?', html):
            seen.setdefault(num, name)

    want_nums = set(re.findall(r"\d+", full))
    ranked = []
    for num, name in seen.items():
        score = _slug_score(full, name)
        name_nums = set(re.findall(r"\d+", name))
        if want_nums and want_nums & name_nums:
            score += 0.3  # совпал номер сезона/части — сильный признак
        elif want_nums and name_nums - want_nums:
            score -= 0.3  # у карточки другой номер — это другой сезон
        ranked.append((score, num, name))
    if not ranked:
        return ""
    ranked.sort(reverse=True)
    score, num, name = ranked[0]
    # требуем уверенного совпадения: иначе легко попасть на другой сезон
    return num if score >= 0.65 else ""


def _slug_score(a: str, b: str) -> float:
    """Похожесть двух слаг-названий (0..1) — устойчива к опечаткам в гласных."""

    def norm(x: str) -> str:
        x = re.sub(r"[^a-zа-я0-9]+", "", x.lower())
        return re.sub(r"[аеиоуыэюяёaeiouy]", "", x)  # убрать гласные

    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    common = 0
    for i in range(min(len(na), len(nb))):
        if na[i] == nb[i]:
            common += 1
    return common / max(len(na), len(nb))


def _animego_build(data: dict, page_url: str, site_ref: str) -> list[dict]:

    out: list[dict] = []
    for tr in data.get("translations") or []:
        name = (tr.get("name") or "").strip()
        episodes = tr.get("episodes") or []
        link = ""
        for ep in episodes:
            cand = (ep.get("link") or "").strip()
            if cand:
                link = cand
                break
        if not link:
            continue
        out.append(
            {
                "url": link,
                "title": f"Kodik · {name}" if name else "Kodik",
                "page": page_url,
                "site": site_ref,
                "translations": [
                    {"num": ep.get("num"), "season": ep.get("season"), "link": ep.get("link")}
                    for ep in episodes
                ],
                "episodes": len(episodes),
            }
        )
    return out


def find_player_iframes(page_url: str) -> list[dict]:
    """
    На странице онлайн-кинотеатра найти вставленные плееры.
    Возвращает [{"url": ..., "title": "Kodik (Дубляж)"}, ...]
    """
    if is_anime_page(page_url):
        try:
            via_api = animego_players(page_url)
        except Exception:
            via_api = []
        if via_api:
            return via_api

    code, page, final = _get(page_url)
    if code != 200 or not page:
        return []
    found: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for m in re.finditer(r"""(?:src|data-src|data-player)\s*=\s*["']([^"']+)["']""", page, re.I):
        raw = html_mod.unescape(m.group(1))
        if not raw.startswith("//") and not raw.startswith("http"):
            continue
        if raw.startswith("//"):
            raw = "https:" + raw
        if not any(h in raw for h in _KODIK_HOSTS):
            continue
        # один и тот же плеер часто вставлен для 480p и 720p —
        # считаем их одним вариантом (ключ: тип + id + хеш)
        km = _KODIK_LINK_RE.search(raw)
        key: tuple[str, str, str] = (
            (km.group(2) or "", km.group(3) or "", km.group(4) or "") if km else (raw, "", "")
        )
        if key in seen:
            continue
        seen.add(key)
        title = "Kodik"
        if km and km.group(2) in ("serial", "seria"):
            title = "Kodik (сериал)"
        found.append({"url": raw, "title": title, "page": final})
    return found


# ======================================================================
#            УНИВЕРСАЛЬНЫЙ ПОИСК ПОТОКА (аналог вкладки Network)
# ======================================================================

# Как выглядит ссылка на HLS-плейлист в коде страницы.
_M3U8_RE = re.compile(
    r"""(?:
        https?:)?//[^\s"'<>\\)]+?\.m3u8[^\s"'<>\\)]*   # абсолютная
        |/[^\s"'<>\\)]+?\.m3u8[^\s"'<>\\)]*            # от корня
        |[A-Za-z0-9_\-./]+?\.m3u8[^\s"'<>\\)]*         # относительная
    """,
    re.I | re.X,
)

# Плееры, которые почти всегда отдают HLS и часто прячут ссылку в JS.
_KNOWN_PLAYER_HOSTS = (
    "kodik",
    "aniboom",
    "anilibria",
    "sibnet",
    "vk.com/video",
    "vkvideo",
    "dailymotion",
    "ok.ru/video",
    "mail.ru/video",
    "my.mail.ru",
    "vimeo.com",
    "player.vimeo",
    "rutube",
    "youtube",
    "twitch",
    "gravitel",
    "gvideo",
    "cloud.mail",
    "cdnvideo",
    "vkcloud",
    "okcdn",
)

# Параметры, в которых сайты держат ссылку на поток.
_STREAM_PARAMS = (
    "file",
    "src",
    "source",
    "url",
    "video",
    "video_url",
    "videourl",
    "hls",
    "hls_url",
    "m3u8",
    "stream",
    "stream_url",
    "playlist",
    "manifest",
    "link",
    "path",
    "content",
)


def _absolutize(base: str, link: str) -> str:
    """Относительную ссылку сделать абсолютной."""
    link = html_mod.unescape(link.strip().strip("\"'"))
    if not link:
        return ""
    if link.startswith("//"):
        return "https:" + link
    if link.startswith("http"):
        return link
    return urllib.parse.urljoin(base, link)


def _decode_js_escapes(text: str) -> str:
    """Развернуть \\/ и \\u002F — сайты часто прячут так ссылки в JSON."""
    out = text.replace("\\/", "/")
    out = re.sub(r"\\u002[fF]", "/", out)
    out = re.sub(r"\\u003[aA]", ":", out)
    out = re.sub(r"\\u0026", "&", out)
    out = re.sub(r"\\u003[dD]", "=", out)
    return out


def scrape_streams(page_url: str, max_pages: int = 3) -> list[dict]:
    """
    Найти ссылки на медиапотоки на странице — как вкладка «Сеть → Медиа».

    Что делается:
      1. Загружается сама страница.
      2. Из HTML и всех скриптов вытаскиваются ссылки *.m3u8 (и mp4).
      3. Разворачиваются экранированные варианты (\\/ и \\u002F).
      4. Проверяются параметры вида file=, src=, video_url= и т.п.
      5. Если на странице есть iframe — рекурсивно заглядываем внутрь
         (до max_pages уровней), потому что плеер обычно живёт там.

    Возвращает список:
      [{"url": "https://…/index.m3u8", "title": "…", "page": <где нашли>,
        "quality": 720 или 0, "kind": "hls"|"file", "via": "html|iframe"}]

    Каждый источник находок — отдельная функция: так видно, откуда взялась
    ссылка, и легко добавить новый способ поиска.
    """
    found: list[dict] = []
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(page_url, 0)]
    visited: set[str] = set()

    while queue:
        current_url, depth = queue.pop(0)
        if current_url in visited or depth > max_pages:
            continue
        visited.add(current_url)

        code, page, final_url = _get(current_url, {"Referer": page_url})
        if code != 200 or not page:
            continue

        raw = _decode_js_escapes(page)
        base = final_url or current_url

        _collect_direct_playlists(raw, base, depth, found, seen)
        _collect_param_streams(raw, base, found, seen)
        _collect_built_streams(base, raw, found, seen)
        _queue_iframes(raw, base, depth, max_pages, visited, queue)

    # если потоков не нашли, пробуем известные плееры через yt-dlp
    return found or _scrape_via_ytdlp(page_url, page_url)


def _add_stream(
    found: list[dict], seen: set[str], link: str, base: str, kind: str, via: str
) -> None:
    """Добавить находку, если такой ссылки ещё не было."""
    if not link or link in seen:
        return
    seen.add(link)
    found.append(
        {
            "url": link,
            "title": "",
            "page": base,
            "quality": _quality_from_url(link),
            "kind": kind,
            "via": via,
        }
    )


def _collect_direct_playlists(
    raw: str, base: str, depth: int, found: list[dict], seen: set[str]
) -> None:
    """Прямые ссылки на плейлисты в HTML и скриптах."""
    for match in _M3U8_RE.finditer(raw):
        link = _absolutize(base, match.group(0).rstrip(".,;"))
        if not link:
            continue
        # отсекаем мусор вроде документации/шаблонов
        if link.count(".") < 2 or len(link) < 22:
            continue
        link = link.replace("//master", "/master")  # следы \u002F
        _add_stream(found, seen, link, base, "hls", "html" if depth == 0 else "iframe")


def _collect_param_streams(raw: str, base: str, found: list[dict], seen: set[str]) -> None:
    """
    Ссылки в параметрах (file=, src=, video_url=…).

    Значение должно быть ЦЕЛЬНОЙ ссылкой: начинаться с http(s), // или / —
    иначе регулярка цепляет куски JSON и адреса вида «…/embed/xample.com/…».
    """
    for param in _STREAM_PARAMS:
        pattern = re.compile(
            rf"""[?&{param}=]((?:https?:)?//[^&"'\s<>]{{10,400}}
                |/[^&"'\s<>]{{10,400}})
                (?=\.(?:m3u8|mp4|webm|mpd))""",
            re.I | re.X,
        )
        for match in pattern.finditer(raw):
            value = urllib.parse.unquote(html_mod.unescape(match.group(1)))
            if not re.search(r"\.(m3u8|mp4|webm|mpd)(\?|$)", value, re.I):
                continue
            link = _absolutize(base, value)
            kind = "hls" if ".m3u8" in link.lower() else "file"
            _add_stream(found, seen, link, base, kind, "param")


def _collect_built_streams(base: str, raw: str, found: list[dict], seen: set[str]) -> None:
    """Ссылки, которые плеер собирает из кусочков (base + id + index.m3u8)."""
    for link in build_stream_from_params(base, raw):
        _add_stream(found, seen, link, base, "hls", "built")


def _queue_iframes(
    raw: str, base: str, depth: int, max_pages: int, visited: set[str], queue: list[tuple[str, int]]
) -> None:
    """Поставить в очередь iframe-ы страницы: плеер обычно живёт там."""
    if depth >= max_pages:
        return
    for match in re.finditer(r"""<iframe[^>]+src\s*=\s*["']([^"']+)["']""", raw, re.I):
        sub_url = _absolutize(base, match.group(1))
        if sub_url and sub_url not in visited:
            queue.append((sub_url, depth + 1))


def probe_stream(url: str, referer: str = "", timeout: int = 6) -> dict:
    """
    Проверить, живой ли поток (как браузер перед воспроизведением).

    Возвращает {"ok": bool, "status": код, "reason": "…"}.
    ok=True только если сервер отдал 200 и в ответе похоже на плейлист (#EXTM3U)
    или на видео/сегмент. 403 — ссылка привязана к сессии, нужен Referer.
    """
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
        # некоторые CDN проверяют Origin вместе с Referer
        try:
            b = urllib.parse.urlsplit(referer)
            headers["Origin"] = f"{b.scheme}://{b.netloc}"
        except ValueError:
            pass
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            head = r.read(2048)
            code = r.status
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "reason": _http_reason(e.code)}
    except Exception as e:
        return {"ok": False, "status": 0, "reason": f"нет ответа: {type(e).__name__}"}
    if code != 200:
        return {"ok": False, "status": code, "reason": _http_reason(code)}
    if head[:7] == b"#EXTM3U" or (head and head[:1] in (b"\x00", b"\x47", b"\xff", b"\x1a")):
        return {"ok": True, "status": 200, "reason": "плейлист"}
    return {"ok": False, "status": 200, "reason": "ответ не похож на поток"}


def _http_reason(code: int) -> str:
    return {
        401: "нужна авторизация",
        403: "доступ закрыт: ссылка привязана к сессии/CDN",
        404: "поток не найден (устарел или собран неверно)",
        410: "поток удалён",
        429: "слишком много запросов",
    }.get(code, f"ошибка сервера ({code})")


def build_stream_from_params(page_url: str, raw: str) -> list[str]:
    """
    Собрать адрес потока из «кирпичиков», которые лежат в коде страницы.

    Многие плееры хранят не готовую ссылку, а её части:
      base = "https://cdn.site/s/FEblpk2WGjWNsdhDyVtOQg/1795425119/"
      id   = "330274"
      path = "/index.m3u8"
    Склеив их, получаем тот самый адрес, который видно в «Сеть → Медиа».

    Возвращает список собранных ссылок.
    """
    out: list[str] = []
    base = urllib.parse.urlsplit(page_url)
    origin = f"{base.scheme}://{base.netloc}"

    # --- 1) шаблоны с подстановкой: ".../{id}/index.m3u8" ---
    tmpl_pat = re.compile(
        r"""["'](?:https?:)?//[^"']{0,200}?\{\{?\s*(\w+)\s*\}?\}[^"']{0,200}?\.m3u8[^"']*["']"""
        r"""|["']/[^"']{0,200}?\{\{?\s*(\w+)\s*\}?\}[^"']{0,200}?\.m3u8[^"']*["']""",
        re.I,
    )
    for m in tmpl_pat.finditer(raw):
        name = m.group(1) or m.group(2) or ""
        val = _js_var(raw, name)
        if not val:
            continue
        built = re.sub(
            r"\{\{?\s*" + re.escape(name) + r"\s*\}?\}", val, m.group(0).strip(chr(34) + chr(39))
        )
        built = _absolutize(origin, built)
        if ".m3u8" in built:
            out.append(built)

    # --- 2) base + file: строка с /s/<токен>/<цифры>/ и отдельный id ---
    base_pat = re.compile(r"""["']((?:https?:)?//[^"']{4,200}/s/[A-Za-z0-9_\-]{6,}/\d{6,}/?)["']""")
    for m in base_pat.finditer(raw):
        stem = _absolutize(origin, m.group(1)).rstrip("/")
        # ищем рядом числовой id и имя файла
        for num in re.findall(r"""["'](\d{4,10})["']""", raw)[:6]:
            for fname in ("index.m3u8", "master.m3u8", "playlist.m3u8"):
                out.append(f"{stem}/{num}/{fname}")
                out.append(f"{stem}/{num}/hls/{fname}")

    # --- 3) origin + путь потока: "/s/<токен>/<цифры>/<id>/index.m3u8" ---
    path_pat = re.compile(
        r"""["'](/[^"']{2,120}/\d{6,}/\d{2,10}/(?:index|master|playlist)\.m3u8)["']"""
    )
    for m in path_pat.finditer(raw):
        out.append(_absolutize(origin, m.group(1)))

    return list(dict.fromkeys(out))


def _js_var(text: str, name: str) -> str:
    """Значение переменной в коде страницы: var id = "330274" / id: '330274'."""
    if not name:
        return ""
    pat = re.compile(
        rf"""(?:var|let|const)?\s*["']?{re.escape(name)}["']?\s*[:=]\s*
            (["']?)([A-Za-z0-9_\-./:]{{1,120}})\1""",
        re.X | re.I,
    )
    m = pat.search(text)
    return m.group(2) if m else ""


def _quality_from_url(url: str) -> int:
    """Вытащить качество из имени ссылки: …/720p/index.m3u8, …_1080.mp4."""
    m = re.search(r"[/_\-\.](\d{3,4})p?(?:[/_\-\.]|$)", url or "", re.I)
    if m:
        try:
            h = int(m.group(1))
            if 144 <= h <= 4320:
                return h
        except ValueError:
            pass
    return 0


def _scrape_via_ytdlp(page_url: str, referer: str) -> list[dict]:
    """
    Запасной путь: попросить yt-dlp разобрать страницу.

    Полезно для сайтов, где ссылка собирается скриптом и в тексте
    страницы её нет, — yt-dlp умеет дёргать плееры сам.
    """
    try:
        import yt_dlp
    except ImportError:
        return []
    try:
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "best",
            "noplaylist": True,
            "socket_timeout": 20,
            "http_headers": {"User-Agent": UA, "Referer": referer},
        }
        with yt_dlp.YoutubeDL({**opts, "extract_flat": False}) as ydl:
            info = ydl.extract_info(page_url, download=False)
        if not info:
            return []
        out = []
        entries = info.get("entries") or [info]
        for ent in entries:
            if not ent:
                continue
            for f in ent.get("formats") or []:
                u = f.get("url") or ""
                if ".m3u8" not in u and not (f.get("ext") == "mp4" and u):
                    continue
                if ".m3u8" not in u:
                    continue
                out.append(
                    {
                        "url": u,
                        "title": ent.get("title") or "",
                        "page": page_url,
                        "quality": f.get("height") or _quality_from_url(u),
                        "kind": "hls",
                        "via": "yt-dlp",
                    }
                )
        return out
    except Exception:
        return []
