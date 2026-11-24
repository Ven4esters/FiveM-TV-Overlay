"""Ответ `POST /bnsi/movies/<id>` → качества и озвучки.

Структура ответа взята не из головы: она расшифрована из скрипта плеера
(см. `docs/REPORT_alloha_source.md`) и соответствует тому, что плеер делает
со своим же ответом:

    player._wsHlsSource = hX.hlsSource;
    quality = hX.hlsSource[i].quality;   // {"428": "https://…/master.m3u8"}

Живого образца с российской машины у нас пока нет — его снимет пробник
`tools/alloha/probe_bnsi.py`. Тесты держат форму ответа, чтобы разбор не
поехал от первой же правки.
"""

from __future__ import annotations

import json

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

    def test_ids_in_order_without_nulls(self) -> None:
        """`id_file: null` отбрасывается: с ним в адресе делать нечего."""
        ids = alloha_bnsi.file_ids(PLAYER_HTML)
        assert ids == ["1836464"]

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
