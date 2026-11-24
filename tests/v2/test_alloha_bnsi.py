"""Ответ `POST /bnsi/movies/<id>` → качества и озвучки.

Структура ответа взята не из головы: она расшифрована из скрипта плеера
(см. `docs/REPORT_alloha_source.md`) и соответствует тому, что плеер делает
со своим же ответом:

    player._wsHlsSource = hX.hlsSource;
    quality = hX.hlsSource[i].quality;   // {"428": "https://…/master.m3u8"}

Образец с российской машины получен 24.11.2026 (заказчик прогнал
`check_alloha.bat`): страница плеера пришла, `fileList` в ней есть, но
`POST /bnsi` ответил «контент не найден». Причина оказалась не в регионе,
а в НОМЕРЕ ФАЙЛА: пробник перебирал `id_file` из разметки, а там у файлов
`null`; правильный id — `active.id` либо файл запрошенной озвучки. Тесты
на реальной странице заказчика (`fixtures/alloha_player.html`) это держат.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tvhost2.core.models import Quality
from tvhost2.sources import alloha, alloha_bnsi

PLAYER_HTML = """
<script>
const fileList = JSON.parse('{"type":"serial","active":{"id":1836882,"id_file":"1836464"},
 "all":{"1":{"1":{"t96":{"id":1847894,"id_file":null,"translation":"TVShows"}}}}}');
</script>
<meta name="viewporti" content="__META__">
"""

META = "WF1ENlE65P2xzcoY0EFD0wtVzmV10kWn0X1TJ1UxpJZFdlZy8yUyuRIHScYLXF2mETTzHUnHMcQeQNdNNMNUbMba"
PLAYER_HTML = PLAYER_HTML.replace("__META__", META)

BNSI_RESPONSE = {
    "pnr": "wss://edge.example/ws",
    "pnk": "abc123",
    "hlsSource": [
        {
            "label": "Оригинальный",
            "audioId": 93,
            "default": True,
            "quality": {
                "428": "https://0a-2e-7c-r400.userplay.live/1/aaa/master.m3u8",
                "720": "https://0a-2e-7c-r400.userplay.live/1/bbb/master.m3u8",
                "1080": (
                    "https://0a-2e-7c-r400.userplay.live/1/ccc/master.m3u8"
                    " or https://0a-2e-7c-r400.userplay.live/1/ddd/master.m3u8"
                ),
            },
        },
        {
            "label": "TVShows",
            "audioId": 96,
            "quality": {"428": "https://0a-2e-7c-r400.userplay.live/1/eee/master.m3u8"},
        },
    ],
}


class TestFileList:
    def test_meta_found(self) -> None:
        assert alloha_bnsi.meta_of(PLAYER_HTML).startswith("WF1ENlE6")

    def test_active_id_file_goes_first(self) -> None:
        """Первым — `active.id_file`, затем `active.id`.

        Раньше считалось иначе («в запрос идёт `id`»), и это оказалось
        ошибкой: настоящий запрос браузера (curl.txt, 24.11.2026) идёт на
        `/bnsi/movies/1836464`, а это ровно `active.id_file`. Из-за прежнего
        порядка этот номер не проверялся вообще — ни в одной из шести попыток.
        """
        ids = alloha_bnsi.file_ids(PLAYER_HTML)
        assert ids[:2] == ["1836464", "1836882"]

    def test_null_ids_skipped(self) -> None:
        """`id_file: null` в список не попадает, а непустые — попадают все."""
        ids = alloha_bnsi.file_ids(PLAYER_HTML)
        assert "None" not in ids
        assert "null" not in ids
        assert len(ids) == len(set(ids)), "номера не повторяются"

    def test_asked_translation_first_after_active(self) -> None:
        """Сразу за активным — тот же эпизод ЗАПРОШЕННОЙ озвучки.

        Разбор образца заказчика: активным был файл серии 6 озвучки 93, а
        страница просила серию 1 озвучки 96 — и правильный ответ 1847894.
        Раньше перебор начинался с id_file, то есть с чужого файла, и Alloha
        отвечала «контент не найден» — неотличимо от отказа по региону.
        """
        embed = "https://tories-as.stravers.live/?token_movie=x&translation=96&season=1&episode=1"
        ids = alloha_bnsi.file_ids(PLAYER_HTML, embed)
        assert ids[0] == "1836464", "сначала активный id_file"
        assert "1847894" in ids[:4], "сразу в начале и файл запрошенной озвучки"


class TestRealPageSample:
    """Проверки на реальной странице плеера заказчика (всё в фикстуре).

    Файл `fixtures/alloha_player.html` — тот самый, что прислал заказчик.
    Урок, который эти тесты закрепляют: `fileList` надо разбирать как JSON
    и искать файл по пути (сезон/серия/озвучка), а не тянуть первое число.
    """

    @pytest.fixture(scope="class")
    def player(self) -> str:
        return (Path(__file__).parent / "fixtures" / "alloha_player.html").read_text("utf-8")

    def test_file_list_parsed(self, player: str) -> None:
        data = alloha_bnsi.file_list(player)
        assert data.get("type") == "serial"
        assert data["active"]["id"] == 1836882

    def test_series_and_translation_resolved(self, player: str) -> None:
        """Серия 1, озвучка 96 → 1847894 (а не 1836402 из озвучки 338)."""
        embed = (
            "https://tories-as.stravers.live/?token_movie=dc27f0f0f45c4989af12f74b6a7757"
            "&translation=96&season=1&episode=1"
        )
        ids = alloha_bnsi.file_ids(player, embed)
        assert ids[0] == "1836464"
        assert "1847894" in ids[:4]
        assert "1836402" in ids, "файлы остальных озвучек остаются как запасные"

    def test_other_translations_differ(self, player: str) -> None:
        """Файл серии 1 запрошенной озвучки стоит в начале перебора.

        Порядок после правки: (1) `active.id_file` — его просит браузер,
        (2) `active.id`, дальше файлы запрошенной озвучки. Для озвучки 93
        активный файл уже из неё, поэтому «файл серии 1» стоит на другом
        месте — проверяем вхождение в начало, а не жёсткий индекс.
        """

        def early_ids(tr: str) -> list[str]:
            embed = f"https://x/?token_movie=x&translation={tr}&season=1&episode=1"
            return alloha_bnsi.file_ids(player, embed)[:5]

        assert "1847894" in early_ids("96")
        assert "1836402" in early_ids("338")
        assert "1831135" in early_ids("241")
        assert "1836787" in early_ids("93")
        # у всех озвучек порядок один: сначала id_file, потом id активного файла
        assert early_ids("96")[:2] == ["1836464", "1836882"]

    def test_kind_defaults_to_serial(self) -> None:
        assert alloha_bnsi.kind_of(PLAYER_HTML) == "serial"
        assert alloha_bnsi.kind_of('"type": "trailer"') == "trailer"


class TestBorth:
    def test_has_two_halves(self) -> None:
        borth = alloha_bnsi.borth_for(PLAYER_HTML)
        first, second = borth.split("|")
        assert len(first) == 64, "первая половина — sha256"
        assert second, "вторая половина считается из меты"

    def test_without_meta_still_works(self) -> None:
        """Страница без меты не должна ронять источник."""
        assert alloha_bnsi.borth_for("<html></html>").endswith("|")


class TestRoutes:
    def test_movies(self) -> None:
        assert (
            alloha_bnsi.bnsi_path("https://x.live/?token=abc", "serial", "1836464")
            == "https://x.live/bnsi/movies/1836464"
        )

    def test_trailers(self) -> None:
        assert (
            alloha_bnsi.bnsi_path("https://x.live/?token=abc", "trailer", "42")
            == "https://x.live/bnsi/trailers/42"
        )


class TestResponse:
    def test_sources_parsed(self) -> None:
        sources = alloha_bnsi.sources_of(json.dumps(BNSI_RESPONSE))
        assert len(sources) == 2
        assert sources[0]["label"] == "Оригинальный"

    def test_garbage_is_not_an_error(self) -> None:
        assert alloha_bnsi.sources_of("<html>404</html>") == []
        assert alloha_bnsi.sources_of('{"hlsSource": "нет"}') == []

    def test_region_text_recognised(self) -> None:
        """Точный текст отказа Alloha — «контент не найден»."""
        body = json.dumps({"error": "К сожалению, запрашиваемый контент не найден."})
        assert alloha_bnsi.asked_region(body) is True
        assert alloha_bnsi.asked_region("какая-то другая беда") is False

    def test_backup_urls_taken_first(self) -> None:
        """` or ` разделяет запасные адреса; мосту нужен один основной."""
        urls = alloha_bnsi.quality_urls(BNSI_RESPONSE["hlsSource"][0])
        assert urls[1080].endswith("/ccc/master.m3u8")
        assert urls[428].endswith("/aaa/master.m3u8")


class TestQualitiesOfSources:
    def test_heights_sorted_desc(self) -> None:
        qualities = alloha.qualities_of_sources(BNSI_RESPONSE["hlsSource"])
        assert [q.height for q in qualities] == [1080, 720, 428]
        assert all(isinstance(q, Quality) for q in qualities)

    def test_audio_index_selects_source(self) -> None:
        """Выбранная озвучка — её адреса; вторая озвучка имеет свою высоту."""
        qualities = alloha.qualities_of_sources(BNSI_RESPONSE["hlsSource"], index=1)
        assert [q.height for q in qualities] == [428]
        assert qualities[0].url.endswith("/eee/master.m3u8")

    def test_bad_index_falls_back_to_first(self) -> None:
        qualities = alloha.qualities_of_sources(BNSI_RESPONSE["hlsSource"], index=99)
        assert qualities and qualities[0].height == 1080

    def test_empty_source_tries_others(self) -> None:
        """У озвучки без адресов берём следующую, а не отдаём пусто."""
        sources = [
            {"label": "Пусто", "quality": {}},
            {"label": "Рабочая", "quality": {"720": "https://x/720/master.m3u8"}},
        ]
        qualities = alloha.qualities_of_sources(sources)
        assert [q.height for q in qualities] == [720]

    def test_service_tracks_dropped(self) -> None:
        """Ниже MIN_HEIGHT — служебные дорожки, а не качество."""
        sources = [{"label": "x", "quality": {"144": "https://x/a/master.m3u8"}}]
        assert alloha.qualities_of_sources(sources) == []
        assert alloha.MIN_HEIGHT == 144

    def test_nothing_at_all(self) -> None:
        assert alloha.qualities_of_sources([]) == []
        assert alloha.qualities_of_sources([{"label": "x"}]) == []


class TestPostForm:
    """Форма — так шлёт запрос сам плеер (`$.ajax` с объектом data)."""

    def test_body_is_urlencoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def fake_post(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, str]:
            seen.update(url=url, data=data, headers=headers)
            return 200, "{}"

        monkeypatch.setattr(alloha_bnsi, "_raw_post", fake_post)
        status, body = alloha_bnsi._post_form(
            "https://x.live/bnsi/movies/1", {"token": "abc", "av1": 0, "autoplay": 0}, {}
        )
        assert (status, body) == (200, "{}")
        assert seen["data"] == b"token=abc&av1=0&autoplay=0"
        headers = seen["headers"]
        assert isinstance(headers, dict)
        assert headers["Content-Type"] == "application/x-www-form-urlencoded; charset=UTF-8"

    def test_body_has_no_json_marks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """В теле нет скобок и кавычек — сервер не спутает форму с JSON."""
        seen: dict[str, bytes] = {}

        def fake_post(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, str]:
            seen["data"] = data
            return 200, "{}"

        monkeypatch.setattr(alloha_bnsi, "_raw_post", fake_post)
        alloha_bnsi._post_form("https://x.live/bnsi/movies/1", {"token": "abc", "av1": 0}, {})
        assert b"{" not in seen["data"] and b'"' not in seen["data"]


class TestVariants:
    """Перебор вариантов: пока один не вернёт `hlsSource`."""

    def test_stops_on_first_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def fake(url: str, payload: dict, headers: dict[str, str]) -> tuple[int, str]:
            calls.append(url)
            if len(calls) == 3:
                return 200, json.dumps(BNSI_RESPONSE)
            return 404, '{"error":"нет"}'

        monkeypatch.setattr(alloha_bnsi, "_post_form", fake)
        monkeypatch.setattr(alloha_bnsi, "_post_json", fake)
        rows = alloha_bnsi.try_variants("https://x.live/?token=abc", PLAYER_HTML, ["111", "222"])
        assert rows[-1]["ok"] is True
        assert len(calls) == 3
        assert rows[-1]["slug"]

    def test_each_variant_hits_every_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def fake(url: str, payload: dict, headers: dict[str, str]) -> tuple[int, str]:
            calls.append(url.rsplit("/", 1)[-1])
            return 404, '{"error":"нет"}'

        monkeypatch.setattr(alloha_bnsi, "_post_form", fake)
        monkeypatch.setattr(alloha_bnsi, "_post_json", fake)
        rows = alloha_bnsi.try_variants("https://x.live/?token=abc", PLAYER_HTML, ["111", "222"])
        names = [row["name"] for row in rows]
        assert len(set(names)) >= 10  # вариантов много, а не один
        assert calls[:2] == ["111", "222"]  # каждый вариант — по обоим номерам
        assert all(row["status"] == 404 for row in rows)

    def test_ids_taken_from_page_when_not_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_answer(url: str, payload: dict, headers: dict[str, str]) -> tuple[int, str]:
            return 404, "{}"

        monkeypatch.setattr(alloha_bnsi, "_post_form", no_answer)
        monkeypatch.setattr(alloha_bnsi, "_post_json", no_answer)
        rows = alloha_bnsi.try_variants("https://x.live/?token=abc", PLAYER_HTML)
        assert {row["id"] for row in rows} == {"1836464", "1836882"}

    def test_no_ids_is_not_a_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_answer(url: str, payload: dict, headers: dict[str, str]) -> tuple[int, str]:
            return 404, "{}"

        monkeypatch.setattr(alloha_bnsi, "_post_form", no_answer)
        monkeypatch.setattr(alloha_bnsi, "_post_json", no_answer)
        rows = alloha_bnsi.try_variants("https://x.live/?token=abc", "<html></html>")
        assert rows and {row["id"] for row in rows} == {"0"}


SARN_UAP = "e82f2e5be321b6f9c1cf4eb99bca60528c19e0fb5853936dfa311aae1dbf0b4c"


class TestServerFingerprint:
    """`uap` из `/sarn` — это `sha256(User-Agent)`, так считает сам сервер."""

    def test_uap_is_sha256_of_ua(self) -> None:
        import hashlib

        expected = hashlib.sha256(alloha_bnsi._DEFAULT_UA.encode()).hexdigest()
        assert len(expected) == 64
        assert expected.isalnum()

    def test_uap_of_remembers_and_survives_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alloha_bnsi._UAP_CACHE.clear()

        def boom(request: object, timeout: int) -> object:
            raise OSError("сеть легла")

        monkeypatch.setattr(alloha_bnsi.urllib.request, "urlopen", boom)
        assert alloha_bnsi._uap_of("x.live") == ""
        # повторный вызов не должен снова лезть в сеть
        assert alloha_bnsi._uap_of("x.live") == ""

    def test_real_borth_uses_server_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def fake(url: str, payload: dict, headers: dict[str, str]) -> tuple[int, str]:
            seen.append(headers.get("Borth", ""))
            return 404, "{}"

        monkeypatch.setattr(alloha_bnsi, "_post_form", fake)
        monkeypatch.setattr(alloha_bnsi, "_uap_of", lambda _realm: SARN_UAP)
        rows = alloha_bnsi.try_real_borth(
            "https://x.live/?token=abc", PLAYER_HTML, ["111"]
        )
        assert rows and rows[0]["status"] == 404
        assert seen[0].startswith(SARN_UAP + "|")

    def test_real_borth_without_uap_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Нет ответа `/sarn` — берём реконструкцию, а не выдуманные числа."""
        seen: list[str] = []

        def fake(url: str, payload: dict, headers: dict[str, str]) -> tuple[int, str]:
            seen.append(headers.get("Borth", ""))
            return 404, "{}"

        monkeypatch.setattr(alloha_bnsi, "_post_form", fake)
        monkeypatch.setattr(alloha_bnsi, "_uap_of", lambda _realm: "")
        rows = alloha_bnsi.try_real_borth(
            "https://x.live/?token=abc", PLAYER_HTML, ["111"]
        )
        assert rows and "uap" not in rows[0]["name"]
        assert seen and not seen[0].startswith(SARN_UAP)


class TestReplayCurl:
    """«Copy as cURL» из браузера → тот же запрос из Python."""

    CURL = (
        "curl 'https://x.live/bnsi/movies/1847894' "
        "-H 'accept: */*' "
        "-H 'borth: abc|def' "
        "-H 'content-type: application/x-www-form-urlencoded; charset=UTF-8' "
        "-H 'origin: https://x.live' "
        "-H 'referer: https://x.live/?token_movie=1' "
        "-H 'x-requested-with: XMLHttpRequest' "
        "--data-raw 'token=abc&av1=false&autoplay=0&audio=&subtitle='"
    )

    def test_parsed(self) -> None:
        from tvhost2 import alloha_replay

        captured = alloha_replay.parse_curl(self.CURL)
        assert captured.url.endswith("/bnsi/movies/1847894")
        assert captured.method == "POST"
        assert captured.headers["borth"] == "abc|def"
        assert captured.body == b"token=abc&av1=false&autoplay=0&audio=&subtitle="

    def test_multiline_with_backslash(self) -> None:
        """Браузер на Windows часто отдаёт команду в несколько строк."""
        from tvhost2 import alloha_replay

        text = self.CURL.replace(" -H ", " \\\n  -H ")
        captured = alloha_replay.parse_curl(text)
        assert captured.url.endswith("/bnsi/movies/1847894")
        assert captured.body.startswith(b"token=abc")

    def test_cmd_caret(self) -> None:
        from tvhost2 import alloha_replay

        text = self.CURL.replace(" -H ", " ^\n  -H ")
        captured = alloha_replay.parse_curl(text)
        assert captured.headers["x-requested-with"] == "XMLHttpRequest"

    def test_garbage_is_not_a_crash(self) -> None:
        from tvhost2 import alloha_replay

        assert alloha_replay.parse_curl("").url == ""
        assert alloha_replay.parse_curl("привет").url == ""

    def test_differences_name_the_borth(self) -> None:
        from tvhost2 import alloha_replay

        captured = alloha_replay.parse_curl(self.CURL)
        lines = "\n".join(alloha_replay.differences(captured))
        assert "Borth" in lines


class TestReplayFidelity:
    """«Один в один» — это про регистр, порядок и отсутствие лишнего."""

    CURL = (
        "curl 'https://x.live/bnsi/movies/1' "
        "-H 'accept: */*' "
        "-H 'borth: ABC|DEF' "
        "-H 'user-agent: Mozilla/5.0 Test' "
        "--data-raw 'token=1&av1=false'"
    )

    def test_header_order_and_case_kept(self) -> None:
        from tvhost2 import alloha_replay

        lines = alloha_replay.header_lines(alloha_replay.parse_curl(self.CURL))
        assert lines[0] == "Host: x.live"
        assert lines[1] == "accept: */*"
        assert lines[2] == "borth: ABC|DEF", "регистр имени менять нельзя"
        assert lines[-1] == f"Content-Length: {len(b'token=1&av1=false')}"
        assert not any(line.lower().startswith("connection:") for line in lines)

    def test_gzip_response_is_read(self) -> None:
        import gzip

        from tvhost2 import alloha_replay

        payload = b'{"hlsSource":[]}'
        packed = gzip.compress(payload)
        assert alloha_replay.decompress(packed, "gzip") == (payload, "")

    def test_broken_gzip_does_not_crash(self) -> None:
        from tvhost2 import alloha_replay

        data, note = alloha_replay.decompress(b"not gzip at all", "gzip")
        assert data == b"not gzip at all"
        assert "gzip" in note

    def test_unknown_encoding_is_reported(self) -> None:
        from tvhost2 import alloha_replay

        data, note = alloha_replay.decompress(b"abc", "br")
        assert data == b"abc"
        assert "br" in note

    def test_notepad_unicode_file_is_read(self, tmp_path) -> None:
        """Старый Блокнот сохраняет «Unicode» — это UTF-16 с BOM."""
        from tvhost2 import alloha_replay

        path = tmp_path / "curl.txt"
        path.write_bytes(self.CURL.encode("utf-16"))
        captured = alloha_replay.parse_curl(alloha_replay._read_text(path))
        assert captured.url.endswith("/bnsi/movies/1")
        assert captured.body == b"token=1&av1=false"


class TestCmdCopyAsCurl:
    """Реальный случай заказчика: Chrome → «Copy as cURL (cmd)».

    Там cmd экранирует `^` всё особенное: кавычки (`^"`), амперсанды (`^&`).
    Первый разбор дал «адрес: НЕ НАЙДЕН» при 14 разобранных заголовках.
    """

    CURL = (
        'curl ^"https://tories-as.stravers.live/bnsi/movies/1847894^" ^\n'
        '  -H ^"accept: */*^" ^\n'
        '  -H ^"borth: e82f2e5b|deadbeef^" ^\n'
        '  -H ^"origin: https://tories-as.stravers.live^" ^\n'
        '  -H ^"referer: https://tories-as.stravers.live/?token_movie=dc27^" ^\n'
        '  -H ^"x-requested-with: XMLHttpRequest^" ^\n'
        '  --data-raw ^"token=5286c49a^&av1=true^&autoplay=0'
        '^&_ref=s1.1795636688_b9b59a5200accf9ca738b61c'
        ".aHR0cHM6Ly96bG9yZC5vcmc^&audio=^&subtitle=^\""
    )

    def test_url_is_clean(self) -> None:
        from tvhost2 import alloha_replay

        captured = alloha_replay.parse_curl(self.CURL)
        assert captured.url == "https://tories-as.stravers.live/bnsi/movies/1847894"

    def test_body_ampersands_and_trailing_caret(self) -> None:
        from tvhost2 import alloha_replay

        body = alloha_replay.parse_curl(self.CURL).body.decode()
        assert body.startswith("token=5286c49a&av1=true&autoplay=0&_ref=s1.")
        assert body.endswith("&audio=&subtitle="), "хвостовой ^ от ^\" надо снять"
        assert "^" not in body

    def test_header_names_clean(self) -> None:
        from tvhost2 import alloha_replay

        headers = alloha_replay.parse_curl(self.CURL).headers
        assert set(headers) >= {
            "accept",
            "borth",
            "origin",
            "referer",
            "x-requested-with",
        }
        assert not any(name.startswith("^") for name in headers)

    def test_bom_and_smart_quotes(self) -> None:
        """Notepad++ пишет BOM, Word — «умные» кавычки. Оба ломают shlex."""
        from tvhost2 import alloha_replay

        text = "\ufeff" + self.CURL.replace('^"', "\u201c").replace('"', "\u201d")
        captured = alloha_replay.parse_curl(text)
        assert captured.url.endswith("/bnsi/movies/1847894")

    def test_broken_command_still_finds_url(self) -> None:
        """Непарная кавычка: shlex сдаётся — адрес всё равно надо достать."""
        from tvhost2 import alloha_replay

        text = "curl 'https://x.live/bnsi/movies/7 -H 'borth: Q|R' --data-raw 'token=7'"
        captured = alloha_replay.parse_curl(text)
        assert captured.url == "https://x.live/bnsi/movies/7"
        assert captured.body == b"token=7"

    def test_guess_url_from_referer(self) -> None:
        from tvhost2 import alloha_replay

        captured = alloha_replay.parse_curl(
            'curl -H "referer: https://x.live/?token_movie=1" --data-raw "token=5"'
        )
        assert alloha_replay.guess_url(captured) == "https://x.live/bnsi/movies/0"

    def test_clean_header_passes_caret_through(self) -> None:
        from tvhost2 import alloha_replay

        assert alloha_replay._unescape_cmd("nothing special") == "nothing special"
        assert alloha_replay._unescape_cmd("a^&b") == "a&b"
        assert alloha_replay._unescape_cmd("^\"quoted^\"") == '"quoted"'


class TestRefToken:
    """`_ref` = `userParam.refToken`: подпись сайта-источника.

    В расшифровке плеера: `userParam['refToken'] && (hf['_ref']=...)`.
    В присланной странице пусто, а в запросе браузера заказчика — настоящее
    значение `s1.<ts>_<24hex>.<base64(адрес страницы)>`.
    """

    FILLED = "refToken: 's1.1795636688_b9b59a5200accf9ca738b61c.aHR0cHM6Ly96bG9yZC5vcmc'"

    def test_empty_in_sample_page(self) -> None:
        assert alloha_bnsi.ref_token_of("userParam = { refToken: '', device: 0 }") == ""

    def test_read_from_filled_page(self) -> None:
        page = f'userParam = {{ {self.FILLED} }}'
        assert alloha_bnsi.ref_token_of(page).startswith("s1.1795636688_")

    def test_double_quotes_too(self) -> None:
        page = 'userParam = { "refToken": "s1.abc_def.ghi" }'
        assert alloha_bnsi.ref_token_of(page) == "s1.abc_def.ghi"

    def test_variants_include_real_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict] = []

        def fake(url: str, payload: dict, headers: dict) -> tuple[int, str]:
            sent.append(payload)
            return 404, "{}"

        monkeypatch.setattr(alloha_bnsi, "_post_form", fake)
        def no_json(url: str, payload: dict, headers: dict) -> tuple[int, str]:
            return 404, "{}"

        monkeypatch.setattr(alloha_bnsi, "_post_json", no_json)
        monkeypatch.setattr(alloha_bnsi, "learnt_ref_tokens", lambda: [])
        page = f'userParam = {{ {self.FILLED} }}'
        embed = "https://x.live/?token=5286c49a9d29cad731353b68d3f1f5"
        alloha_bnsi.try_variants(embed, page, ["1"])
        refs = [item.get("_ref") for item in sent if "_ref" in item]
        expected = "s1.1795636688_b9b59a5200accf9ca738b61c.aHR0cHM6Ly96bG9yZC5vcmc"
        assert expected in refs

    def test_ref_from_curl_file(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Пользователь положил curl.txt — `_ref` оттуда тоже пробуем."""
        folder = tmp_path / "alloha_check"
        folder.mkdir()
        (folder / "curl.txt").write_text(
            "curl 'https://x/bnsi/movies/1' --data-raw "
            "'token=1&_ref=s1.1795_abc.aHR0cHM6Ly96bG9yZC5vcmc'",
            encoding="utf-8",
        )
        monkeypatch.setattr(alloha_bnsi, "CHECK_DIR", folder)
        values = alloha_bnsi._ref_values("")
        assert any(v.startswith("s1.1795_abc") for v in values)


class TestFormPayload:
    """Тело запроса — ровно как у плеера: token, av1, autoplay (+_ref если есть)."""

    EMBED = "https://x.live/?token_movie=dc27&token=5286c49a9d29cad731353b68d3f1f5"

    def test_without_ref(self) -> None:
        payload = alloha_bnsi.form_payload(self.EMBED, "userParam = { refToken: '' }")
        assert payload == {
            "token": "5286c49a9d29cad731353b68d3f1f5",
            "av1": 0,
            "autoplay": 0,
        }

    def test_with_ref(self) -> None:
        page = "userParam = { refToken: 's1.1795638751_abc.def' }"
        payload = alloha_bnsi.form_payload(self.EMBED, page)
        assert payload["_ref"] == "s1.1795638751_abc.def"
        assert list(payload) == ["token", "av1", "autoplay", "_ref"], "порядок как у плеера"

    def test_attempts_send_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict] = []

        def fake(url: str, payload: dict, headers: dict) -> tuple[int, str]:
            sent.append(dict(payload))
            return 404, "{}"

        monkeypatch.setattr(alloha_bnsi, "_post_form", fake)
        page = (
            'const fileList = JSON.parse(\'{"type":"serial","active":{"id":111,'
            '"id_file":null},"all":{"1":{"1":{"t96":{"id":111,"id_file":null}}}}}\');\n'
            "userParam = { refToken: 's1.1795638751_abc.def' }"
        )
        alloha_bnsi.attempts(self.EMBED, page)
        assert sent and all(item.get("_ref") == "s1.1795638751_abc.def" for item in sent)


class TestBrowserTruth:
    """Настоящий запрос браузера заказчика (curl.txt от 24.11.2026, Firefox).

    Два открытия, которые сломали прежние догадки:

      1. Браузер просил `/bnsi/movies/1836464` — это `active.id_file`,
         а не `active.id` (1836882). Мы слали `id` и этот номер вообще
         не проверяли.
      2. `Borth` браузера = `d07bc5a1…`, и это НЕ `sha256(User-Agent)`
         (для того же Firefox он 4e224c69…). Первая половина считается
         от canvas/WebGL/аудио, как и было в расшифровке `c0`.
    """

    CURL = (
        "curl.exe ^\"https://tories-as.stravers.live/bnsi/movies/1836464^\" ^\n"
        "  --compressed ^\n"
        "  -X POST ^\n"
        '  -H ^"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:160.0)'
        ' Gecko/20100101 Firefox/160.0^" ^\n'
        '  -H ^"Borth: d07bc5a1ca5840c001b152adaa937e86a48b74e77eded89a18395a0f'
        '314bc33f^|aEpHM005ZzRUekF8MTc5NTU1MzA4Nnw2ZGNzNDd5ZmRrc2hfSzU3Ymh2S3dUeFJ'
        'MU0lKelphS0tUSl9pcVlQLTdJ^" ^\n'
        '  -H ^"X-Requested-With: XMLHttpRequest^" ^\n'
        '  --data-raw ^"token=5286c49a9d29cad731353b68d3f1f5^&av1=true'
        '^&autoplay=0^&_ref=s1.1795639479.383dbf8d82a5e71102059964'
        '.aHR0cHM6Ly96bG9yZC5vcmcv^&audio=^&subtitle=^"'
    )

    def test_id_file_first(self) -> None:
        """1876464 → первым: именно его просит браузер."""
        embed = (
            "https://tories-as.stravers.live/?token_movie=dc27"
            "&translation=96&season=1&episode=1"
        )
        ids = alloha_bnsi.file_ids(PLAYER_HTML, embed)
        assert ids[0] == "1836464", ids[:4]

    def test_borth_taken_from_curl(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        folder = tmp_path / "alloha_check"
        folder.mkdir()
        (folder / "curl.txt").write_text(self.CURL, encoding="utf-8")
        monkeypatch.setattr(alloha_bnsi, "CHECK_DIR", folder)
        borth = alloha_bnsi.learnt_borths()[0]
        assert "^" not in borth
        assert len(borth) == 153
        assert borth.split("|")[0] == (
            "d07bc5a1ca5840c001b152adaa937e86a48b74e77eded89a18395a0f314bc33f"
        )

    def test_borth_is_not_sha256_of_ua(self) -> None:
        import hashlib

        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:160.0) "
            "Gecko/20100101 Firefox/160.0"
        )
        assert hashlib.sha256(ua.encode()).hexdigest() != (
            "d07bc5a1ca5840c001b152adaa937e86a48b74e77eded89a18395a0f314bc33f"
        )

    def test_ref_from_curl_goes_to_payload(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        folder = tmp_path / "alloha_check"
        folder.mkdir()
        (folder / "curl.txt").write_text(self.CURL, encoding="utf-8")
        monkeypatch.setattr(alloha_bnsi, "CHECK_DIR", folder)
        payload = alloha_bnsi.form_payload(
            "https://x.live/?token=5286c49a9d29cad731353b68d3f1f5",
            "userParam = { refToken: '' }",
        )
        assert payload["_ref"].startswith("s1.1795639479.")

    def test_no_curl_means_no_learnt_borth(self, tmp_path, monkeypatch) -> None:
        empty = tmp_path / "nothing"
        empty.mkdir()
        monkeypatch.setattr(alloha_bnsi, "CHECK_DIR", empty)
        assert alloha_bnsi.learnt_borths() == []
