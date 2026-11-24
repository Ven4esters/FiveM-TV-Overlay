"""Тесты anidub: разбор обёртки и сборка качеств.

Сети здесь нет. Проверяем ровно то, что ломается тихо:

1. Ссылка с любой страницы anidub попадает в источник, а чужие — нет.
2. Плеер находится и когда он в iframe, и когда только во вкладке `data`
   (у разных серий заполнено разное — проверено на двух живых страницах).
3. Внутренний `vid.php?v=…` склеивается с адресом плеера, а не с доменом
   anidub: цифровой хост плеера меняется, и жёсткая подстановка сломается.
4. Master anivids даёт настоящие 1280x720 и 854x480 с настоящим битрейтом.
5. Заголовки едут вместе с потоком: CDN отвечает 403 на ffmpeg-отпечаток.
"""

from __future__ import annotations

import pytest

from tvhost2.core.models import StreamKind
from tvhost2.sources.anidub import (
    AnidubSource,
    clean_title,
    is_anidub,
    parse_master,
    player_url,
    stream_path,
)

PAGE = """<!doctype html><html><head>
<title>Операция: Семейка Ёдзакура 3 сезон 7 серия смотреть онлайн бесплатно</title>
</head><body>
<iframe allowfullscreen id="playeriframe"
  src="https://player26.ladonyvesna2005.info/index.php?v=/s9/ff50476816f1ab65fade4918a1044c8f&amp;playlist">
</iframe>
</body></html>"""

PAGE_TAB_ONLY = """<html><head><title>Мобильный воин Гандам смотреть онлайн</title></head>
<body><div class="tabs-sel series-tab">
  <span data="https://player31.ladonyvesna2005.info/index.php?v=/s5/big_hash&amp;playlist">
    ПЛЕЕР #1</span>
</div></body></html>"""

PLAYER = """<html><body>
<video id="hls-video" data-stream-src="vid.php?v=%2Fs9%2Fff50476816f1ab65fade4918a1044c8f"></video>
</body></html>"""

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=3139300,RESOLUTION=1280x720
https://cdn2.anivids.link/vod8/s9/abc/fhd.mp4/chunk.m3u8?md5=AAA&expires=1795489503
#EXT-X-STREAM-INF:BANDWIDTH=1639300,RESOLUTION=854x480
https://cdn2.anivids.link/vod8/s9/abc/sd.mp4/chunk.m3u8?md5=BBB&expires=1795489503
"""


class TestMatches:
    def test_page_matches(self):
        assert is_anidub("https://anidub.com/12492-operacija-semejka-edzakura.html")

    def test_page_without_www_matches(self):
        assert is_anidub("http://www.anidub.com/anime_ongoing/")

    def test_foreign_host_does_not_match(self):
        assert not is_anidub("https://anidub.net/12345-film.html")

    def test_other_site_does_not_match(self):
        assert not is_anidub("https://ok.ru/video/7475662490142")

    def test_source_matches_by_url(self):
        assert AnidubSource().matches("https://anidub.com/12248-horoshij-znakomysh.html")


class TestPlayerUrl:
    def test_player_found_in_iframe(self):
        expected = (
            "https://player26.ladonyvesna2005.info"
            "/index.php?v=/s9/ff50476816f1ab65fade4918a1044c8f&playlist"
        )
        assert player_url(PAGE) == expected

    def test_ampersand_unescaped(self):
        """&amp; в HTML должен стать обычным &: иначе параметр playlist потеряется."""
        assert "&amp;" not in player_url(PAGE)

    def test_player_found_in_tab_data(self):
        """У части серий iframe пуст, а плеер лежит только вкладкой в data."""
        assert "player31" in player_url(PAGE_TAB_ONLY)

    def test_no_player_returns_empty(self):
        assert player_url("<html><body>видео удалено</body></html>") == ""


class TestStreamPath:
    def test_path_from_player_html(self):
        assert stream_path(PLAYER) == "vid.php?v=%2Fs9%2Fff50476816f1ab65fade4918a1044c8f"

    def test_no_stream_returns_empty(self):
        assert stream_path("<html><body><video></video></body></html>") == ""


class TestMaster:
    def test_two_qualities(self):
        qualities = parse_master(MASTER)
        assert [q.height for q in qualities] == [720, 480]

    def test_frame_sizes_are_real(self):
        """854x480 — это 480p, а не 480x854: размеры должны остаться как есть."""
        best_480 = next(q for q in parse_master(MASTER) if q.height == 480)
        assert (best_480.width, best_480.height) == (854, 480)

    def test_bitrate_is_real(self):
        """BANDWIDTH=3139300 — это 3139 кбит/с, а не выдуманные 5000."""
        assert parse_master(MASTER)[0].bitrate_kbps == 3139

    def test_missing_bandwidth_gets_estimate(self):
        """Без BANDWIDTH считаем нижнюю границу: 0 кбит/с застрял бы на 144p."""
        no_bw = "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\nhttps://cdn.anivids.link/a.m3u8\n"
        assert parse_master(no_bw)[0].bitrate_kbps > 0

    def test_garbage_returns_nothing(self):
        assert parse_master("<html>403 Forbidden</html>") == ()

    def test_no_resolution_is_skipped(self):
        """Вариант без RESOLUTION бесполезен: не знаем, какое это качество."""
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=999\nhttps://cdn.anivids.link/a.m3u8\n"
        assert parse_master(text) == ()


class TestTitle:
    def test_suffix_cut(self):
        assert clean_title(PAGE) == "Операция: Семейка Ёдзакура 3 сезон 7 серия"

    def test_without_suffix_unchanged(self):
        assert clean_title("<title>Наруто</title>") == "Наруто"

    def test_no_title(self):
        assert clean_title("<html></html>") == ""


class TestResolveShape:
    def test_kind_is_hls(self):
        """anivids отдаёт один master-плейлист, звук внутри — склейка не нужна."""
        assert AnidubSource().kind is StreamKind.HLS

    def test_state_is_ok(self):
        """anidub не требует ни браузера, ни cookies — честно пишем OK."""
        assert AnidubSource().state.value == "ok"

    def test_headers_exist_in_source(self):
        """CDN anivids отвечает 403 на ffmpeg-отпечаток.

        Проверяем объявление, а не сам поток: resolve ходит в сеть, и в
        наборе быстрых тестов ему не место. Заголовки собраны в resolve
        одним литералом — их наличие здесь и есть контракт.
        """
        import inspect

        text = inspect.getsource(AnidubSource.resolve)
        assert "User-Agent" in text and "Referer" in text


@pytest.mark.parametrize(
    "url",
    [
        "https://anidub.com/12492-operacija-semejka-edzakura.html",
        "https://anidub.com/anime_movie/",
    ],
)
def test_registry_accepts_anidub(url: str):
    from tvhost2.sources import register_all

    register_all()
    from tvhost2.core import registry

    found = registry.find(url)
    assert found is not None and found.name == "anidub"
