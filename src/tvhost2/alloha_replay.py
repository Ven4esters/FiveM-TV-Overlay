"""Повторить запрос из браузера один в один: `python -m tvhost2.alloha_replay`.

ЗАЧЕМ. Пять отчётов с машины заказчика показали одно и то же: страница
плеера приходит (значит, не регион), номер файла правильный (значит, не id),
тело и заголовки перебраны (28 вариантов), а `POST /bnsi` всё равно отвечает
404 «контент не найден». Дальше угадывать бессмысленно: у браузера запрос
работает, у нас — нет, и разницу видно, только если сравнить два запроса
буквально.

ЧТО ДЕЛАТЬ (на компьютере заказчика, где плеер играет):

  1. Открыть страницу с видео, дождаться, пока плеер начнёт играть.
  2. F12 → вкладка «Network» (Сеть) → фильтр `bnsi`.
  3. Щёлкнуть правой кнопкой по строке `bnsi` → Copy → **Copy as cURL**.
     Подходит любой из двух вариантов — «bash» и «cmd».
  4. Вставить в файл `alloha_check/curl.txt` (обычный текстовый файл).
  5. Запустить `replay_alloha.bat`.

Скрипт разберёт вставленное, повторит запрос и покажет: код ответа, тело, и
таблицу «что браузер прислал — что мы шлём» по заголовкам.

Почему `http.client`, а не `urllib`: `urllib` переписывает имена заголовков
(`borth` → `Borth`) и дописывает свои (`Accept-Encoding`, `Connection`).
Смысл повтора — отправить ровно те же строки, в том же порядке и регистре,
поэтому запрос собирается руками.
"""

from __future__ import annotations

import gzip
import http.client
import shlex
import sys
import urllib.parse
import zlib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "alloha_check"
INPUT_NAMES = ("curl.txt", "curl", "bnsi.txt")

TIMEOUT_S = 25
"""Столько же, сколько у обычного запроса адресов: сравнивать надо честно."""

BROWSER_ONLY = (
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "priority",
    "dnt",
    "accept-encoding",
    "connection",
    "content-length",
    "host",
    "user-agent",
)
"""Заголовки, которые ставит сам браузер или клиент.

Их присутствие в разборе полезно видеть, но подделывать `User-Agent` нельзя:
сервер считает от него отпечаток (`sha256(UA)` в ответе `/sarn`), и подмена
просто создаст новый незнакомый клиент. Здесь же важно обратное: при повторе
мы берём `User-Agent` браузера как есть — иначе отпечаток не сойдётся.
"""

SKIP_WHEN_SENDING = ("host", "content-length", "connection")
"""Эти ставит сам клиент: `Host` — из адреса, `Content-Length` — из тела."""


@dataclass
class Captured:
    """Разобранный запрос из браузера."""

    url: str = ""
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    raw_lines: int = 0

    def header_pairs(self) -> list[tuple[str, str]]:
        """Пары «имя, значение» в исходном порядке — регистр сохраняется."""
        return list(self.headers.items())


def parse_curl(text: str) -> Captured:
    """Разобрать `Copy as cURL` — из bash или из cmd.

    Браузер кладёт команду одной строкой, но при вставке в файл её часто
    рвут переносами; в Windows-варианте строки соединены `^`, в bash — `\\\\`.
    Оба вида склеиваем, потом разбираем как обычную команду.
    """
    joined = _join_lines(text)
    if not joined.strip():
        return Captured()
    tokens = _tokenize(joined)
    result = Captured(raw_lines=len(text.splitlines()))
    body_parts: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        value = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token in ("-H", "--header") and value:
            name, _, header_value = value.partition(":")
            if name:
                result.headers[name.strip()] = header_value.strip()
            index += 2
            continue
        if token in ("-X", "--request") and value:
            result.method = value.upper()
            index += 2
            continue
        if token in ("--data-raw", "--data", "-d", "--data-binary", "--data-ascii") and value:
            body_parts.append(_unquote_ansi(value))
            index += 2
            continue
        if token in ("--url",) and value:
            result.url = value
            index += 2
            continue
        if token in ("-A", "--user-agent") and value:
            result.headers["User-Agent"] = value
            index += 2
            continue
        if token in ("-b", "--cookie") and value:
            result.headers["Cookie"] = value
            index += 2
            continue
        if token in ("-e", "--referer") and value:
            result.headers["Referer"] = value
            index += 2
            continue
        if token.startswith("http") and not result.url:
            result.url = token
        index += 1
    result.body = "&".join(body_parts).encode() if body_parts else b""
    if not result.method:
        result.method = "POST" if result.body else "GET"
    return result


def _unquote_ansi(value: str) -> str:
    """Убрать остаток `$'...'` — так bash-вариант пишет тело со спецзнаками.

    `shlex` кавычки уже снял, но знак `$` перед ними остаётся в слове.
    """
    if value.startswith("$") and not value.startswith("${"):
        return value[1:]
    return value


def _join_lines(text: str) -> str:
    """Склеить многострочную команду: `^` (cmd) и `\\\\` (bash) в конце строки."""
    lines = [line.rstrip("\r") for line in text.splitlines()]
    merged: list[str] = []
    buffer = ""
    for line in lines:
        stripped = line.rstrip()
        if stripped.endswith("^") or stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        merged.append(buffer + stripped)
        buffer = ""
    if buffer:
        merged.append(buffer)
    return " ".join(part for part in merged if part.strip())


def _tokenize(command: str) -> list[str]:
    """Разбить команду на слова. Кавычки — по правилам shell, но терпимо к Windows."""
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        # Windows-вариант иногда приходит с непарной кавычкой в теле — режем грубо.
        return command.replace('"', " ").replace("'", " ").split()


def _parts_of(url: str) -> tuple[str, int, str] | None:
    """Схема, порт и путь запроса из адреса. `None` — если адрес не тот."""
    split = urllib.parse.urlsplit(url)
    if split.scheme not in ("http", "https") or not split.hostname:
        return None
    port = split.port or (443 if split.scheme == "https" else 80)
    path = split.path or "/"
    if split.query:
        path += "?" + split.query
    return f"{split.scheme}://{split.hostname}:{port}", port, path


def header_lines(captured: Captured) -> list[str]:
    """Строки заголовков для отправки — ровно как в браузере, плюс `Host`.

    Вынесено отдельно, чтобы это можно было проверить тестом: именно тут
    решается «один в один» или «примерно так».
    """
    lines: list[str] = []
    host_name = urllib.parse.urlsplit(captured.url).netloc
    if host_name:
        lines.append(f"Host: {host_name}")
    for name, value in captured.header_pairs():
        if name.lower() in SKIP_WHEN_SENDING:
            continue
        lines.append(f"{name}: {value}")
    if captured.body:
        lines.append(f"Content-Length: {len(captured.body)}")
    return lines


def decompress(data: bytes, encoding: str) -> tuple[bytes, str]:
    """Распаковать ответ. Второй элемент — пояснение, если не вышло."""
    encoding = (encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return data, ""
    try:
        if "gzip" in encoding:
            return gzip.decompress(data), ""
        if "deflate" in encoding:
            try:
                return zlib.decompress(data), ""
            except zlib.error:
                return zlib.decompress(data, -zlib.MAX_WBITS), ""
    except (OSError, zlib.error) as exc:
        return data, f"{encoding} не распаковался: {exc}"
    return data, f"ответ сжат ({encoding}) — показан как есть"


def replay(captured: Captured) -> tuple[int, dict[str, str], str]:
    """Повторить запрос ровно так, как он записан. Возвращает код, заголовки, тело."""
    if not captured.url:
        return 0, {}, "в файле не нашлась ни ссылка, ни команда curl"
    parts = _parts_of(captured.url)
    if parts is None:
        return 0, {}, f"не понял адрес: {captured.url[:120]}"
    authority, _port, path = parts
    secure = authority.startswith("https:")
    host, port = authority.split("://", 1)[1].rsplit(":", 1)
    connection_class = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    connection = connection_class(host, int(port), timeout=TIMEOUT_S)
    try:
        connection.putrequest(captured.method, path, skip_host=True, skip_accept_encoding=True)
        for line in header_lines(captured):
            name, _, value = line.partition(":")
            connection.putheader(name, value.strip())
        connection.endheaders(captured.body or None)
        response = connection.getresponse()
        raw = response.read()
        headers = {name: value for name, value in response.getheaders()}
        data, note = decompress(raw, response.getheader("Content-Encoding") or "")
        text = data.decode("utf-8", "replace")
        if note:
            text = f"[{note}; распаковано {len(data)} из {len(raw)} байт]\n{text}"
        return response.status, headers, text
    except Exception as exc:  # сеть бывает любой
        return 0, {}, f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()


def our_headers() -> dict[str, str]:
    """Заголовки, которые сейчас шлёт приложение (для таблицы сравнения)."""
    from .sources import alloha_bnsi

    return {name: value for name, value in alloha_bnsi._common_headers().items()}


def differences(captured: Captured) -> list[str]:
    """Построчный список «есть в браузере — нет у нас» и наоборот."""
    ours = our_headers()
    browser = {name.lower(): value for name, value in captured.headers.items()}
    lines: list[str] = []
    for name in sorted(browser):
        if name in ("host", "content-length", "connection"):
            continue
        value = browser[name]
        short = value if len(value) <= 70 else value[:67] + "…"
        if name == "borth":
            lines.append(f"  Borth            ← браузер даёт {len(value)} символов")
            continue
        if name in BROWSER_ONLY:
            lines.append(f"  {name:20s} браузер ставит сам: {short}")
            continue
        lines.append(f"  {name:20s} ЕСТЬ в браузере: {short}")
    for name, value in ours.items():
        if name.lower() not in browser:
            lines.append(f"  {name:20s} шлём МЫ, в браузере нет: {value[:70]}")
    if not lines:
        lines.append("  отличий по заголовкам не видно")
    return lines


def _read_text(path: Path) -> str:
    """Прочитать как UTF-8, но пережить «Unicode» из старого Блокнота (UTF-16)."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or (b"\x00" in raw[:200] and raw.count(b"\x00") > 4):
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8-sig", "replace")


def _find_input() -> Path | None:
    for name in INPUT_NAMES:
        path = OUT / name
        if path.is_file():
            return path
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    OUT.mkdir(exist_ok=True)
    source = Path(args[0]) if args else _find_input()
    if source is None or not source.is_file():
        print(__doc__)
        print("Не нашёл файл с командой. Положите Copy as cURL в:")
        for name in INPUT_NAMES:
            print(f"  {OUT / name}")
        return 1

    text = _read_text(source)
    captured = parse_curl(text)
    print(f"Разобрал: {source}")
    print(f"  адрес:  {captured.url[:120] or 'НЕ НАЙДЕН'}")
    print(f"  метод:  {captured.method}")
    print(f"  тело:   {len(captured.body)} байт — {captured.body[:160]!r}")
    print(f"  заголовков: {len(captured.headers)}")
    if not captured.url:
        print("\nСсылка не разобралась. Проверьте, что вставили именно «Copy as cURL».")
        return 1

    print("\n── Чем запрос браузера отличается от нашего " + "─" * 18)
    for line in differences(captured):
        print(line)

    print("\n── Повтор запроса один в один " + "─" * 30)
    status, headers, body = replay(captured)
    print(f"ответ: {status}, {len(body)} байт")
    for wanted in ("content-type", "content-encoding", "borth", "pn"):
        for key, value in headers.items():
            if key.lower() == wanted:
                print(f"  {key}: {value[:100]}")
    print(f"тело: {body[:300]}")

    stamp = Path(source).stem
    (OUT / f"replay_{stamp}_body.txt").write_text(body, encoding="utf-8")
    print(f"\nтело сохранено: replay_{stamp}_body.txt")
    if "hlsSource" in body:
        print("\nИТОГ: браузерный запрос РАБОТАЕТ — пришлите этот файл и summary.txt;")
        print("по нему видно, какое ровно отличие делает разницу.")
        return 0
    print("\nИТОГ: тот же отказ, что и у нас. Значит, дело не в самом запросе,")
    print("а в том, ЧЕМ он отправлен (браузер против Python). Пришлите файл.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
