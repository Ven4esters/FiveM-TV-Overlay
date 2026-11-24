"""Получение адресов потока у Alloha: `POST /bnsi/movies/<id_file>`.

Откуда это взялось: скрипт плеера (`build/app.c63ca8da.js`) обфусцирован, но
снимается полностью — см. `docs/REPORT_alloha_source.md`. Главное из него:

    hF = fileList.type === 'trailer' ? '/trailers/' : '/movies/';
    Q5().ajax('/bnsi' + hF + <id_file>, {
        method: 'POST',
        data: {token, av1, autoplay, …},
        headers: {'Borth': <отпечаток> + '|' + iP(iR(iS(<meta viewporti>)))}
    }).done(function (hX) {
        hX.hlsSource   // массив озвучек: {label, quality: {"<высота>": "<адрес>"}, audioId}
    });

То есть в разметке страницы адресов нет вообще — их отдаёт этот запрос.
Снаружи России он отвечает 404 «контент не найден» на ЛЮБОЙ id (проверено:
и на реальный, и на выдуманный, с любым токеном, телом и заголовками), на
компьютере в России — отдаёт данные. Поэтому путь и лежит отдельным модулем:
приложение запускается у заказчика в России, там он и работает.

Зачем `Borth` целиком, если первая половина — отпечаток браузера, а мы не
браузер: вторая половина (`iP(iR(iS(meta)))`) считается точно, и её одну
сервер принимает — проверка отпечатка, судя по ответам, не мешает. Если
однажды начнёт мешать, это будет видно по коду ответа (не 404, а 403).
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..web.fetch import FetchError, fetch_text
from .alloha_borth import borth_suffix

HTTP_OK = 200
"""Успех. Отдельной константой — иначе правило про «магические» числа."""

BNSI_TIMEOUT = 20
"""Секунд на запрос адресов. Ответ небольшой, дольше ждать нечего."""

HTTP_NOT_FOUND = 404
"""Региональный отказ приходит именно этим кодом, с текстом «не найден»."""

META_RE = re.compile(r'name="viewporti"\s+content="([^"]+)"')
"""Мета, из которой плеер собирает вторую половину `Borth`."""

FILE_LIST_RE = re.compile(r"fileList\s*[:=]\s*(\{.*?\})\s*[,;]\s*(?:const|var|let|\n)", re.S)
"""`fileList` — список файлов: тип (фильм/сериал), активный файл и все серии."""

FILE_LIST_PARSE_RE = re.compile(r"fileList\s*=\s*JSON\.parse\('(.+?)'\)", re.S)
"""Та же разметка, но в том виде, в каком её кладёт плеер: JSON внутри строки.

Разбирать регуляркой `"id_file": (\\d+)` нельзя: у файлов из `fileList`
`id_file` почти всегда `null`, а числовой `id` есть у всех. Плюс один и тот
же эпизод лежит в списке по каждой озвучке, и id у них РАЗНЫЕ — поэтому
нельзя брать «первый попавшийся»: нужен именно тот, что для запрошенной
озвучки.
"""

ID_FIELD_RE = re.compile(r'"(?P<key>id_file|id)"\s*:\s*"?\d+"?')
"""Запасной путь, если JSON не разобрался: видим, какие числа вообще есть."""

TYPE_RE = re.compile(r'"type"\s*:\s*"(trailer|serial|movie)"')
"""Тип определяет ветку адреса: `/trailers/` или `/movies/`."""

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
"""Тот же UA, что у настоящего Chrome: плеер смотрит на него в отпечатке."""


def meta_of(player_html: str) -> str:
    """`content` меты `viewporti` — вход для перестановок `Borth`."""
    match = META_RE.search(player_html or "")
    return match.group(1) if match else ""


def file_list(player_html: str) -> dict[str, Any]:
    """`fileList` как словарь. Пусто — если разметка другая или её нет."""
    match = FILE_LIST_PARSE_RE.search(player_html or "")
    if not match:
        block = FILE_LIST_RE.search(player_html or "")
        if not block:
            return {}
        match_text = block.group(1)
    else:
        match_text = match.group(1)
    try:
        data = json.loads(match_text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def active_file(player_html: str) -> dict[str, Any]:
    """Активный файл из `fileList` — тот, что плеер открывает первым."""
    active = file_list(player_html).get("active")
    return active if isinstance(active, dict) else {}


def translation_id(embed: str) -> str:
    """Озвучка, которую просили: `translation=` из адреса встройки.

    Именно её файл и нужен: в `fileList` один и тот же эпизод лежит по разу
    на каждую озвучку, и id у них разные. Запрос с id чужой озвучки Alloha
    отвергает так же, как несуществующий, — «контент не найден».
    """
    match = re.search(r"[?&]translation=(\d+)", embed or "")
    return match.group(1) if match else ""


def _files(data: dict[str, Any]) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    """Все файлы из `fileList.all` вместе с путём до них.

    Формы бывают две, и обе надо поддержать:

        сериал:  all["1"]["1"]["t96"] = {"id": 1847894, …}
        фильм:   all["t96"]           = {"id": 1847894, …}

    Путь нужен целиком: только по нему видно, какая это серия и какая
    озвучка. Без этого легко взять «первый попавшийся id» — а он почти
    всегда от другой серии или озвучки, и Alloha на такой запрос отвечает
    «контент не найден» (проверено на образце заказчика).
    """
    result: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    all_block = data.get("all")
    if not isinstance(all_block, dict):
        return result

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if not isinstance(node, dict):
            return
        if "id" in node:
            result.append((path, node))
            return
        for key, child in node.items():
            walk(child, (*path, str(key)))

    walk(all_block, ())
    return result


def _translation_key(path: tuple[str, ...]) -> str:
    """Ключ озвучки в пути: `t96`, `t338`… (у фильмов он же единственный)."""
    return path[-1] if path and path[-1].startswith("t") else ""


def _season_episode(path: tuple[str, ...]) -> tuple[str, str]:
    """Сезон и серия из пути; у фильма — пустые строки."""
    numbers = [part for part in path if part.isdigit()]
    if len(numbers) >= 2:  # noqa: PLR2004 — сезон+серия это ровно два числа
        return numbers[0], numbers[1]
    return "", ""


def file_ids(player_html: str, embed: str = "") -> list[str]:
    """Id файлов для `POST /bnsi` — в порядке, в каком их стоит пробовать.

    В адрес запроса идёт `id`, а НЕ `id_file`: в расшифровке плеера видно,
    как активный файл передаётся в `iB(…['id'])` → `iT(hq)` → `hF+hq`.
    У файлов в `fileList` поле `id_file` почти всегда `null`, а числовой `id`
    есть у всех.

    Порядок:
      1. активный файл (`active.id`) — его плеер и открывает;
      2. тот же сезон и серия, но в запрошенной озвучке (`translation=` из
         адреса встройки);
      3. первая серия запрошенной озвучки;
      4. всё остальное по порядку (сначала запрошенная озвучка).

    Почему именно так: на образце заказчика активным был файл серии 6
    озвучки 93 (`id 1836882`), а страница просила серию 1 озвучки 96
    (`id 1847894`). Запрос с чужим id Alloha отвергает так же, как
    несуществующий, и это выглядит как отказ по региону — хотя дело было
    в номере файла.
    """
    data = file_list(player_html)
    result: list[str] = []

    def add(value: Any) -> None:
        text = str(value) if isinstance(value, int | str) else ""
        if text.isdigit() and text not in result:
            result.append(text)

    if not data:
        # Разметка не разобралась — вытаскиваем числа как есть, лишь бы не молчать.
        for match in re.finditer(r'"id"\s*:\s*"?(\d+)"?', player_html or ""):
            add(match.group(1))
        return result

    active = active_file(player_html)
    add(active.get("id"))
    active_id = str(active.get("id") or "")

    files = _files(data)
    wanted = translation_id(embed)
    wanted_key = f"t{wanted}" if wanted else ""

    # Где мы сейчас: серия и озвучка активного файла, поправленные адресом.
    active_path = next((path for path, node in files if str(node.get("id")) == active_id), ())
    season, episode = _season_episode(active_path)
    asked_season = re.search(r"[?&]season=(\d+)", embed or "")
    asked_episode = re.search(r"[?&]episode=(\d+)", embed or "")
    season = asked_season.group(1) if asked_season else season
    episode = asked_episode.group(1) if asked_episode else episode

    def key_of(path: tuple[str, ...]) -> tuple[str, str, str]:
        file_season, file_episode = _season_episode(path)
        return (file_season, file_episode, _translation_key(path))

    ordered = sorted(files, key=lambda item: key_of(item[0]))

    # 2) та же серия в запрошенной озвучке
    if wanted_key:
        for path, node in ordered:
            if key_of(path)[:2] == (season, episode) and _translation_key(path) == wanted_key:
                add(node.get("id"))
        # 3) первая серия запрошенной озвучки
        for path, node in ordered:
            if _translation_key(path) == wanted_key:
                add(node.get("id"))
                break
    # 4) всё остальное, запрошенная озвучка раньше прочих
    for _, node in ordered:
        add(node.get("id"))
    return result


def borth_for(player_html: str) -> str:
    """Заголовок `Borth`: отпечаток | перестановка меты.

    Первая половина — sha256 от набора, который собирает плеер (UA, часовой
    пояс, экран, язык, память, ядра). Canvas/WebGL/audio из браузера нам
    недоступны; проверено, что сервер с этим не спорит.
    """
    meta = meta_of(player_html)
    suffix = borth_suffix(meta) if meta else ""
    first = hashlib.sha256(
        "||".join([_DEFAULT_UA, "Europe/Moscow", "1920x1080", "ru-RU", "8", "8"]).encode()
    ).hexdigest()
    return f"{first}|{suffix}"


def _post_form(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
    """POST как это делает jQuery: `application/x-www-form-urlencoded`.

    В расшифровке плеер зовёт `Q5().ajax(..., {data: {...}})`, а jQuery по
    умолчанию отправляет данные формой, не JSON. Значит основной вариант —
    именно этот, а JSON — проверочный.
    """
    data = urllib.parse.urlencode(payload).encode()
    request_headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        **_common_headers(),
        **headers,
    }
    return _raw_post(url, data, request_headers)


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
    """POST с JSON-телом. Возвращает код и тело — тело нужно и при ошибке.

    `fetch_text` для этого не годится: он бросает исключение на не-200, а нам
    важно РАЗЛИЧИТЬ «регион закрыт» (404 с текстом) и «сломалось» (сеть/500).
    """
    data = json.dumps(payload).encode()
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        **_common_headers(),
        **headers,
    }
    return _raw_post(url, data, request_headers)


def _common_headers() -> dict[str, str]:
    return {
        "User-Agent": _DEFAULT_UA,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }


def _raw_post(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=BNSI_TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # сеть бывает любой — код 0 значит «не дошло»
        return 0, str(exc)


def bnsi_path(embed: str, file_kind: str, ident: str) -> str:
    """Адрес запроса: `/bnsi/movies/<id>` или `/bnsi/trailers/<id>`."""
    base = (embed or "").split("?", 1)[0].rstrip("/")
    folder = "trailers" if file_kind == "trailer" else "movies"
    return f"{base}/bnsi/{folder}/{ident}"


def kind_of(player_html: str) -> str:
    """Тип файла из `fileList` (по умолчанию — сериал: в разметке так чаще)."""
    match = TYPE_RE.search(player_html or "")
    return match.group(1) if match else "serial"


def sources_of(response_body: str) -> list[dict[str, Any]]:
    """`hlsSource` из ответа — массив озвучек с адресами по высотам."""
    try:
        data = json.loads(response_body)
    except json.JSONDecodeError:
        return []
    value = data.get("hlsSource") if isinstance(data, dict) else None
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def asked_region(response_body: str) -> bool:
    """Сервер сказал «контент не найден» — это его ответ на чужой регион.

    Точный текст: «К сожалению, запрашиваемый контент не найден». Снаружи
    России он приходит на любой id, поэтому отличить «нет такого файла» от
    «вам нельзя» по нему нельзя — и то и другое объясняется одинаково.

    Тонкость, на которой легко обжечься: Alloha отдаёт ответ в JSON с
    ЭКРАНИРОВАННЫМ юникодом (`\u041a \u0441\u043e\u0436…`), поэтому искать
    русский текст в сыром теле бессмысленно — не найдётся никогда. Сначала
    раскрываем экранирование, потом ищем. Тест на это есть
    (`test_region_text_recognised`): без раскрытия он падает.
    """
    text = _unescape_unicode(response_body or "").lower()
    return "не найден" in text or "not found" in text


_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _unescape_unicode(text: str) -> str:
    """`\\u041a` → `К`. Только это: остальной JSON нам читать не нужно."""
    return _UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def quality_urls(source: dict[str, Any]) -> dict[int, str]:
    """Высоты и адреса из одной озвучки.

    Адрес — строка; в ней может лежать несколько резервных через ` or `
    (так их склеивает сервер). Берём первый: остальные — запасные зеркала,
    а мост умеет ходить только по одному адресу за раз.
    """
    raw = source.get("quality")
    if not isinstance(raw, dict):
        return {}
    result: dict[int, str] = {}
    for key, value in raw.items():
        try:
            height = int(str(key).split("x")[0])
        except ValueError:
            continue
        if not isinstance(value, str):
            continue
        url = value.split(" or ")[0].strip()
        if url:
            result[height] = url
    return result


def form_payload(embed: str, player_html: str) -> dict[str, Any]:
    """Тело запроса, как его собирает плеер.

    Из расшифровки `iT` (live @667002):

        hf = {'token': userParam.token, 'av1': config.av1Support,
              'autoplay': userParam.autoPlayChange}

    и дальше `userParam['refToken'] && (hf['_ref'] = userParam['refToken'])`.

    `_ref` — подпись сайта-источника. Раньше мы отправляли его пустым,
    теперь берём из страницы плеера, если сервер её положил.
    """
    payload: dict[str, Any] = {
        "token": _site_token(embed),
        "av1": 0,
        "autoplay": 0,
    }
    ref = ref_token_of(player_html)
    if ref:
        payload["_ref"] = ref
    return payload


def attempts(embed: str, player_html: str) -> list[dict[str, Any]]:
    """Все попытки запроса адресов — чтобы было видно, что и чем ответило.

    Возвращает список словарей: `url`, `body`, `status`, `headers` (тела
    ответов). Порядок — как в `file_ids`. Нужно диагностике: по одному
    «контент не найден» невозможно понять, дело в номере файла, в регионе
    или в чём-то третьем, а по списку из шести попыток — видно.
    """
    borth = borth_for(player_html)
    kind = kind_of(player_html)
    ids = file_ids(player_html, embed)
    result: list[dict[str, Any]] = []
    for ident in ids[:6]:
        url = bnsi_path(embed, kind, ident)
        headers = {"Borth": borth, "Referer": embed, "Origin": _origin_of(embed)}
        # Сначала форма: так делает jQuery в самом плеере. JSON — вторым
        # шагом, на случай если сервер принимает только его.
        payload = form_payload(embed, player_html)
        status, body = _post_form(url, payload, headers)
        if status == HTTP_OK and not sources_of(body):
            status, body = _post_json(url, payload, headers)
        result.append({"id": ident, "url": url, "status": status, "body": body})
        if status == HTTP_OK and sources_of(body):
            break
        if not asked_region(body) and status not in (HTTP_OK, HTTP_NOT_FOUND):
            # Не «не найден» и не успех: похоже, дело в запросе. Следующие
            # номера уже не помогут.
            break
    return result


def fetch_sources(embed: str, player_html: str) -> tuple[str, list[dict[str, Any]]]:
    """Главный вход: запросить адреса, вернуть первое удачное тело и `hlsSource`.

    Пустой список — ни один файл не отдался. Почему именно — показывает
    `attempts()`; источник и диагностика ходят через неё, чтобы не было двух
    разных путей к одному и тому же запросу.
    """
    last_body = ""
    for attempt in attempts(embed, player_html):
        last_body = str(attempt.get("body") or "")
        if attempt.get("status") == HTTP_OK:
            sources = sources_of(last_body)
            if sources:
                return last_body, sources
    return last_body, []


CHECK_DIR = Path(__file__).resolve().parents[3] / "alloha_check"
"""Куда `check_alloha.bat` и `replay_alloha.bat` кладут отчёты и `curl.txt`."""

VARIANT_IDS = 2
"""Сколько номеров пробовать на каждый вариант: первый (активный) и главный."""


def try_real_borth(embed: str, player_html: str, ids: list[str] | None = None) -> list[dict]:
    """Проверить `Borth`, собранный ПРАВДОПОДОБНО, а не из выдуманных чисел.

    Что выяснилось из расшифровки app.js (функция `c0`, её зовёт `c1` из `iT`):
    первая половина `Borth` — это `sha256(join('||'))` от девяти значений:

        navigator.userAgent
        Intl.DateTimeFormat().resolvedOptions().timeZone
        ширина и высота экрана
        navigator.language
        navigator.deviceMemory
        navigator.hardwareConcurrency
        canvas.toDataURL()            — отпечаток canvas
        WebGL vendor|renderer          — отпечаток видеокарты
        длина аудио-отпечатка

    Canvas, WebGL и аудио из Python не получить, поэтому берём два варианта,
    которые сервер точно может проверить сам:

      * `uap` из ответа `/sarn` — это `sha256(User-Agent)`; сервер считает его
        сам и, возможно, ждёт именно его;
      * `sha256(UA|пояс|экран|язык|память|ядра)` — наша реконструкция `c0`
        без браузерных полей.

    Функция не бросает исключений: `/sarn` не ответил — остаётся вторая
    половина списка (реконструкция без браузерных полей).
    """
    numbers = (ids or file_ids(player_html, embed))[:VARIANT_IDS]
    if not numbers:
        numbers = ["0"]
    suffix = borth_suffix(meta_of(player_html)) if meta_of(player_html) else ""
    token = _site_token(embed)
    origin = _origin_of(embed)
    realm = urllib.parse.urlsplit(embed).hostname or ""

    plans: list[tuple[str, str]] = []
    uap = _uap_of(realm)
    if uap:
        plans.append(("Borth — отпечаток сервера (uap)", uap))
    joined = "||".join([_DEFAULT_UA, "Europe/Moscow", "1920x1080", "ru-RU", "8", "8"])
    plans.append(
        (
            "Borth — sha256(UA|пояс|экран|язык|память|ядра)",
            hashlib.sha256(joined.encode()).hexdigest(),
        )
    )

    result: list[dict] = []
    for name, first in plans:
        headers = {
            "Origin": origin,
            "Referer": embed,
            "X-Requested-With": "XMLHttpRequest",
            "Borth": f"{first}|{suffix}",
        }
        for ident in numbers:
            url = bnsi_path(embed, kind_of(player_html), ident)
            status, body = _post_form(url, {"token": token, "av1": 0, "autoplay": 0}, headers)
            ok = "hlsSource" in (body or "")
            result.append(
                {
                    "name": name,
                    "slug": _slug(name),
                    "id": ident,
                    "status": status,
                    "body": body,
                    "ok": ok,
                }
            )
            if ok:
                return result
    return result


_UAP_CACHE: dict[str, str] = {}
"""`uap` из `/sarn` на домен. Сервер даёт новый `challenge` каждый раз, а
`uap` — это `sha256(User-Agent)`, он от повторов не меняется."""


def _uap_of(realm: str) -> str:
    """`uap` из `/sarn` — отпечаток клиента по мнению самого сервера.

    Так его запрашивает плеер: `fetch('/sarn', {headers: {acceptis: <домен>}})`,
    и сервер отвечает `{challenge, nonce, uap}`, где `uap` — sha256(User-Agent).
    Пусто — если сервер не ответил.
    """
    if realm in _UAP_CACHE:
        return _UAP_CACHE[realm]
    answer = ""
    if realm:
        request = urllib.request.Request(
            f"https://{realm}/sarn",
            headers={**_common_headers(), "Acceptis": realm},
        )
        try:
            with urllib.request.urlopen(request, timeout=BNSI_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            value = payload.get("uap")
            if isinstance(value, str):
                answer = value
        except Exception:  # сеть бывает любой — тогда просто нет отпечатка
            answer = ""
    _UAP_CACHE[realm] = answer
    return answer


def try_variants(embed: str, player_html: str, ids: list[str] | None = None) -> list[dict]:
    """Перебрать варианты запроса, пока один не вернёт `hlsSource`.

    Зачем: заказчик прислал три отчёта, где на ВСЕ шесть номеров файла
    приходит один и тот же «контент не найден». Номер исключён, регион
    исключён (страница плеера приходит) — значит, Alloha сверяет что-то ещё
    в самом запросе. Вместо угадывания по одному варианту за запуск
    проверяем сразу все разумные:

      * тело формой (так делает jQuery в плеере) или JSON;
      * `av1`: 0, 1, «true»;
      * `autoplay`: 0 и 1;
      * `Borth` с отпечатком и без него;
      * лишние поля (`audio`, `subtitle`, `_ref`);
      * `X-Requested-With`, `Origin`, `Referer`.

    Возвращает строки отчёта: `name`, `slug`, `id`, `status`, `body`, `ok`.
    """
    numbers = (ids or file_ids(player_html, embed))[:VARIANT_IDS]
    if not numbers:
        numbers = ["0"]
    suffix = borth_suffix(meta_of(player_html)) if meta_of(player_html) else ""
    fingerprint = hashlib.sha256(
        "||".join([_DEFAULT_UA, "Europe/Moscow", "1920x1080", "ru-RU", "8", "8"]).encode()
    ).hexdigest()
    token = _site_token(embed)
    origin = _origin_of(embed)
    base_headers = {
        "Origin": origin,
        "Referer": embed,
        "X-Requested-With": "XMLHttpRequest",
    }

    plans: list[tuple[str, str, dict, dict, bool]] = [
        (
            "форма (как jQuery), av1=0",
            "form",
            {"token": token, "av1": 0, "autoplay": 0},
            {**base_headers, "Borth": f"{fingerprint}|{suffix}"},
            True,
        ),
        (
            "JSON, av1=0",
            "json",
            {"token": token, "av1": 0, "autoplay": 0},
            {**base_headers, "Borth": f"{fingerprint}|{suffix}"},
            False,
        ),
        (
            "форма, av1=1",
            "form",
            {"token": token, "av1": 1, "autoplay": 0},
            {**base_headers, "Borth": f"{fingerprint}|{suffix}"},
            True,
        ),
        (
            "форма, av1=true",
            "form",
            {"token": token, "av1": "true", "autoplay": 0},
            {**base_headers, "Borth": f"{fingerprint}|{suffix}"},
            True,
        ),
        (
            "форма, autoplay=1",
            "form",
            {"token": token, "av1": 0, "autoplay": 1},
            {**base_headers, "Borth": f"{fingerprint}|{suffix}"},
            True,
        ),
        (
            "форма, поле audio=93",
            "form",
            {"token": token, "av1": 0, "autoplay": 0, "audio": 93},
            {**base_headers, "Borth": f"{fingerprint}|{suffix}"},
            True,
        ),
        (
            "форма, subtitle=0",
            "form",
            {"token": token, "av1": 0, "autoplay": 0, "subtitle": 0},
            {**base_headers, "Borth": f"{fingerprint}|{suffix}"},
            True,
        ),
        (
            "форма, _ref пусто",
            "form",
            {"token": token, "av1": 0, "autoplay": 0, "_ref": ""},
            {**base_headers, "Borth": f"{fingerprint}|{suffix}"},
            True,
        ),
        (
            "Borth без отпечатка",
            "form",
            {"token": token, "av1": 0, "autoplay": 0},
            {**base_headers, "Borth": f"|{suffix}"},
            True,
        ),
        ("без Borth", "form", {"token": token, "av1": 0, "autoplay": 0}, base_headers, True),
        (
            "без X-Requested-With",
            "form",
            {"token": token, "av1": 0, "autoplay": 0},
            {"Origin": origin, "Referer": embed, "Borth": f"{fingerprint}|{suffix}"},
            True,
        ),
        (
            "JSON, только token",
            "json",
            {"token": token},
            {**base_headers, "Borth": f"{fingerprint}|{suffix}"},
            False,
        ),
    ]

    ref_token = ref_token_of(player_html)

    # Отдельная группа: `_ref`. Раньше мы его не отправляли ни разу (только
    # пустым), а браузер заказчика шлёт настоящий. Порядок: сначала то, что
    # нашлось в самой странице плеера, потом — накопленное из curl.txt.
    for ref_value in _ref_values(ref_token):
        plans.append(
            (
                f"форма, _ref={ref_value[:14]}…",
                "form",
                {"token": token, "av1": 0, "autoplay": 0, "_ref": ref_value},
                {**base_headers, "Borth": f"{fingerprint}|{suffix}"},
                True,
            )
        )

    result: list[dict] = []
    for name, _encoding, payload, headers, is_form in plans:
        for ident in numbers:
            url = bnsi_path(embed, kind_of(player_html), ident)
            if is_form:
                status, body = _post_form(url, payload, headers)
            else:
                status, body = _post_json(url, payload, headers)
            ok = "hlsSource" in (body or "")
            result.append(
                {
                    "name": name,
                    "slug": _slug(name),
                    "id": ident,
                    "status": status,
                    "body": body,
                    "ok": ok,
                }
            )
            if ok:
                return result
    return result


def _ref_values(from_page: str) -> list[str]:
    """Значения `_ref`, которые есть смысл проверить: страница, затем curl.txt."""
    values: list[str] = []
    if from_page:
        values.append(from_page)
    for item in learnt_ref_tokens():
        if item and item not in values:
            values.append(item)
    return values[:3]


def learnt_ref_tokens() -> list[str]:
    """`refToken`, добытые из наших же `curl.txt` (их кладёт пользователь).

    После первого успешного повтора запроса браузера `_ref` больше не
    загадка — но показывать его в отчётах незачем, а вот приложению он
    нужен: без него `/bnsi` может отвечать 404 так же, как нам.
    """
    found: list[str] = []
    for name in ("curl.txt", "curl", "bnsi.txt"):
        path = CHECK_DIR / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r"_ref=([A-Za-z0-9._%-]{20,})", text):
            value = urllib.parse.unquote(match.group(1))
            if value not in found:
                found.append(value)
    return found


def _slug(name: str) -> str:
    """Имя файла из названия варианта: без пробелов и знаков."""
    keep = [ch.lower() if ch.isalnum() else "_" for ch in name]
    return "".join(keep).strip("_")[:40]


def ref_token_of(player_html: str) -> str:
    """`refToken` из `userParam` страницы плеера — это подпись сайта-источника.

    В расшифровке плеера (`app.js`, отправка `/bnsi`) стоит:

        userParam['refToken'] && (hf['_ref'] = userParam['refToken'])

    то есть `_ref` появляется в запросе ТОЛЬКО если сервер подставил
    `refToken` в страницу. В образце заказчика от 24.11.2026 там пусто
    (`refToken: ''`), а в ЕГО запросе из браузера `_ref` был настоящий:

        s1.1795636688_b9b59a5200accf9ca738b61c.aHR0cHM6Ly96bG9yZC5vcmc
           время      24 hex (подпись)          base64 = https://zlord.org

    Значит, заполняет его сам сайт-источник. Если он есть — отправляем.
    """
    # Ключ бывает и без кавычек (`refToken: '…'`), и в кавычках
    # (`"refToken": "…"`) — поэтому кавычки вокруг имени необязательны.
    match = re.search(
        r"""refToken["']?\s*:\s*["']([^"']*)["']""",
        player_html or "",
    )
    return match.group(1) if match else ""


def _site_token(embed: str) -> str:
    match = re.search(r"[?&]token=([0-9a-f]{20,})", embed or "")
    return match.group(1) if match else ""


def _origin_of(embed: str) -> str:
    parts = urllib.parse.urlsplit(embed or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""


def page_text(url: str, referer: str = "") -> str:
    """Скачать страницу плеера. Пусто — если её не отдали."""
    try:
        return fetch_text(url, {"Referer": referer} if referer else None)
    except FetchError:
        return ""
