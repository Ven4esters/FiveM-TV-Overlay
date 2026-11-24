"""Тесты hdrezka: разбор обёртки, ответ AJAX и — главное — гео.

Сети нет. Все данные взяты с живой страницы 24.11.2026 и приведены дословно:
сервер из США ответил `premium_content: 1, url: false` и написал в плеере
«К сожалению, это видео не доступно в вашем регионе». Это надо отличать от
поломки сайта — иначе заказчик получит «разбирайся сам» вместо понятного
«это видео не для вашей страны».
"""

from __future__ import annotations

import pytest

from tvhost2.core.errors import GeoblockedError, SourceFailedError
from tvhost2.core.models import StreamKind
from tvhost2.sources.hdrezka import (
    HdrezkaSource,
    explain_payload,
    is_hdrezka,
    parse_qualities,
    title_from,
    translator_id_from,
    video_id_from,
)

PAGE = """<html><head>
<title>Смотреть сериал Бессонные ночи онлайн бесплатно в хорошем качестве</title></head>
<body>
<ul class="b-translators__list">
<li title="HDrezka Studio" class="b-translator__item b-prem_translator active"
    data-id="94122" data-translator_id="316" data-camrip="0">HDrezka Studio</li>
<li title="Дубляж" class="b-translator__item b-prem_translator"
    data-id="94122" data-translator_id="672">Дубляж</li>
</ul>
<div id="player" class="b-player"><div id="videoplayer"></div></div>
<script>
$(function () { sof.tv.initCDNSeriesEvents(
    94122, 316, 0, 0, 0, 'hdrezka-home.tv', false, false, {}); });
</script>
</body></html>"""

REGION_BLOCK_PAGE = (
    PAGE
    + """
<div class="b-player__restricted__block_message">
  К сожалению, это видео не доступно в вашем регионе
</div>"""
)

PAYLOAD_OK = {
    "success": True,
    "message": "",
    "premium_content": 0,
    "url": "https://p12.hdrezka.tv/s/m/abc/def:1795489503/720.mp4:hls:manifest.m3u8",
    "quality": '<li class="quality" data-value="1080">1080p</li>'
    '<li class="quality" data-value="720">720p</li>'
    '<li class="quality" data-value="480">480p</li>',
}

PAYLOAD_REGION = {
    "success": True,
    "message": "",
    "premium_content": 1,
    "url": False,
    "quality": False,
    "subtitle": False,
}


class TestMatches:
    def test_hdrezka_ag(self):
        assert is_hdrezka("https://hdrezka.ag/films/drama/94061-posledniy-uzhin.html")

    def test_mirror(self):
        """Зеркало сменило домен на hdrezka-home.tv — ссылка обязана попадать сюда."""
        assert is_hdrezka("https://hdrezka-home.tv/films/drama/94061-posledniy-uzhin-2026.html")

    def test_rezka_ag(self):
        assert is_hdrezka("https://rezka.ag/series/1000-film.html")

    def test_foreign_is_rejected(self):
        assert not is_hdrezka("https://anidub.com/12492-operacija.html")

    def test_not_a_url_is_rejected(self):
        assert not is_hdrezka("просто текст")

    def test_source_matches(self):
        assert HdrezkaSource().matches("https://hdrezka.ag/films/drama/1-film.html")


class TestPageParsing:
    def test_video_id(self):
        assert video_id_from(PAGE) == "94122"

    def test_video_id_absent(self):
        assert video_id_from("<html>нет плеера</html>") == ""

    def test_translator_active(self):
        """Активная озвучка — 316, а не первая из списка: плеер открывает её."""
        assert translator_id_from(PAGE) == "316"

    def test_translator_fallback_first(self):
        html = '<li data-translator_id="672">Дубляж</li>'
        assert translator_id_from(html) == "672"

    def test_title_suffix_cut(self):
        assert title_from(PAGE).startswith(("Сериал Бессонные ночи", "Смотреть"))

    def test_title_online_cut(self):
        html = "<title>Бессонные ночи смотреть онлайн бесплатно</title>"
        assert title_from(html) == "Бессонные ночи"


class TestQualities:
    def test_heights_from_quality_list(self):
        assert [q.height for q in parse_qualities(PAYLOAD_OK)] == [1080, 720, 480]

    def test_same_master_for_every_quality(self):
        """Сайт даёт один плейлист и переключает качество параметром:
        подменять адрес на «своё качество» нельзя, его нет."""
        urls = {q.url for q in parse_qualities(PAYLOAD_OK)}
        assert len(urls) == 1

    def test_width_from_aspect(self):
        best = parse_qualities(PAYLOAD_OK)[0]
        assert (best.width, best.height) == (1920, 1080)

    def test_bitrate_not_zero(self):
        """0 кбит/с заставил бы ABR застрять на низком уровне (ERR-067)."""
        assert all(q.bitrate_kbps > 0 for q in parse_qualities(PAYLOAD_OK))

    def test_no_url_gives_nothing(self):
        assert parse_qualities(PAYLOAD_REGION) == ()

    def test_no_quality_list_falls_back_to_720(self):
        payload = {"url": "https://p12.hdrezka.tv/x.m3u8", "quality": ""}
        assert parse_qualities(payload)[0].height == 720


class TestExplainPayload:
    def test_ok_payload_has_no_reason(self):
        assert explain_payload(PAYLOAD_OK, PAGE) == ""

    def test_region_detected(self):
        """Сервер из США: premium_content=1 И текст про регион. Это не поломка."""
        assert explain_payload(PAYLOAD_REGION, REGION_BLOCK_PAGE) == "region"

    def test_premium_without_region_text(self):
        assert explain_payload(PAYLOAD_REGION, PAGE) == "premium"

    def test_unknown_answer_passes_message(self):
        reason = explain_payload({"url": False, "message": "Время сессии истекло"}, PAGE)
        assert "сессии" in reason

    def test_silent_no_url(self):
        assert "потока" in explain_payload({"url": False}, PAGE)


class TestResolveHtml:
    def test_stream_from_payload(self):
        source = HdrezkaSource()
        stream = source.resolve_html(PAGE, lambda _path, _data: PAYLOAD_OK)
        assert stream.kind is StreamKind.HLS
        assert [q.height for q in stream.qualities] == [1080, 720, 480]

    def test_region_raises_geoblocked(self):
        """Гео — отдельный тип ошибки: человеку говорим про регион, а не «сломалось»."""
        source = HdrezkaSource()
        with pytest.raises(GeoblockedError):
            source.resolve_html(REGION_BLOCK_PAGE, lambda _p, _d: PAYLOAD_REGION)

    def test_bot_check_page_is_not_an_error_of_site(self):
        """Пока Anubis решает задачу, виноват не сайт: просим подождать."""
        source = HdrezkaSource()
        with pytest.raises(SourceFailedError) as info:
            source.resolve_html("<title>Проверяем, что вы не бот!</title>", lambda _p, _d: {})
        assert "проверяет браузер" in info.value.why

    def test_ajax_gets_video_and_translator(self):
        """AJAX уходит ровно с теми id, что на странице: иначе сайт ответит отказом."""
        seen: dict = {}

        def fake_fetch(path: str, data: dict) -> dict:
            seen["path"] = path
            seen.update(data)
            return PAYLOAD_OK

        HdrezkaSource().resolve_html(PAGE, fake_fetch)
        assert seen["path"] == "/ajax/get_cdn_series/"
        assert seen["id"] == "94122"
        assert seen["translator_id"] == "316"
        assert seen["action"] == "get_movie"

    def test_series_flag(self):
        seen: dict = {}

        def fake_fetch(_path: str, data: dict) -> dict:
            seen.update(data)
            return PAYLOAD_OK

        HdrezkaSource().resolve_html(PAGE, fake_fetch)
        assert seen["is_tv"] == "1"

    def test_movie_flag(self):
        html = PAGE.replace("initCDNSeriesEvents", "initCDNMoviesEvents")
        seen: dict = {}

        def fake_fetch(_path: str, data: dict) -> dict:
            seen.update(data)
            return PAYLOAD_OK

        HdrezkaSource().resolve_html(html, fake_fetch)
        assert seen["is_tv"] == "0"

    def test_no_player_in_page(self):
        source = HdrezkaSource()
        with pytest.raises(SourceFailedError) as info:
            source.resolve_html("<html><body>пусто</body></html>", lambda _p, _d: {})
        assert "нет плеера" in info.value.why


def test_state_is_partial():
    """Честно: без браузера и вне России источник не открывается."""
    assert HdrezkaSource().state.value == "partial"


def test_registry_accepts_hdrezka():
    from tvhost2.core import registry
    from tvhost2.sources import register_all

    register_all()
    url = "https://hdrezka.ag/films/drama/94061-posledniy-uzhin.html"
    found = registry.find(url)
    assert found is not None and found.name == "hdrezka"
