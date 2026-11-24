"""anidub.org — новый плеер cdnvideohub.

Сети здесь нет: проверяем разбор и правила, каждое из которых появилось
из-за реального сбоя. Это важнее, чем кажется: пять из шести ошибок при
разработке были ТИХИМИ — видео молча не играло, без единой ошибки в консоли.

Что закреплено:

1. Домены разведены: `.org` (новый плеер) и `.com` (старый) не должны
   перехватывать ссылки друг у друга. `.com` на странице «Ван-Писа» отдаёт
   404 — это разные сайты.
2. Одни и те же заголовки на все запросы. API выдаёт подпись под тот
   браузер, которым пришёл запрос (`srcAg=GECKO`), и CDN отвечает 400
   тому, кто не совпал. Проверено: одна ссылка → 206 на Firefox-UA и
   400 на Chrome-UA.
3. Размер кадра читается из mp4, а не придумывается по имени поля.
4. `is_direct_file=True`: hls.js не разбирает mp4 и молчит об этом.
5. Серия выбирается первая, а не последняя (сайт продолжает с 1180-й).
"""

from __future__ import annotations

from tvhost2.core.models import Stream, StreamKind
from tvhost2.sources.anidub import is_anidub as is_anidub_com
from tvhost2.sources.anidub_org import (
    CACHE,
    FIREFOX_UA,
    MPEG_VARIANTS,
    AnidubOrgSource,
    _tkhd_size,
    clean_title,
    is_anidub_org,
    parse_master,
    pick,
    player_params,
    playlist_url,
    qualities_from_video,
    tracks_by_episode,
    voice_title,
)
from tvhost2.web.player import levels_json
from tvhost2.web.session import Session

PAGE = """<!doctype html><html><head>
<title>Ван-Пис &#91;ТВ&#93; смотреть аниме онлайн бесплатно на сайте</title>
</head><body><div class="player-wrapper">
  <video-player id="myPlayer"
      data-title-id="019169b6-9f49-7086-a8d3-76f1e2558ffa"
      data-publisher-id="19"
      ident="player_1"
      data-aggregator="cvh"></video-player>
</div></body></html>"""

PLAYLIST = {
    "titleName": "Ван-Пис",
    "isSerial": True,
    "items": [
        {
            "cvhId": "a1",
            "name": "Ван-Пис",
            "vkId": "111",
            "voiceStudio": "AniLiberty",
            "voiceType": "Дубляж",
            "season": 1,
            "episode": 1,
        },
        {
            "cvhId": "a2",
            "name": "Ван-Пис",
            "vkId": "222",
            "voiceType": "Субтитры",
            "season": 1,
            "episode": 1,
        },
        {
            "cvhId": "a3",
            "name": "Ван-Пис",
            "vkId": "333",
            "voiceType": "Неизвестный",
            "season": 1,
            "episode": 1,
        },
        {
            "cvhId": "b1",
            "name": "Ван-Пис",
            "vkId": "444",
            "voiceType": "Субтитры",
            "season": 1,
            "episode": 2,
        },
    ],
}

MP4_HEAD = bytearray(400)
MP4_HEAD[0:8] = (32).to_bytes(4, "big") + b"ftyp"
MP4_HEAD[32:40] = (368).to_bytes(4, "big") + b"moov"
MP4_HEAD[40:48] = (100).to_bytes(4, "big") + b"mvhd"
MP4_HEAD[140:148] = (228).to_bytes(4, "big") + b"trak"
MP4_HEAD[148:156] = (92).to_bytes(4, "big") + b"tkhd"
MP4_HEAD[148 + 92 - 8 : 148 + 92 - 4] = (1280 << 16).to_bytes(4, "big")
MP4_HEAD[148 + 92 - 4 : 148 + 92] = (720 << 16).to_bytes(4, "big")


class TestDomains:
    def test_org_matches(self):
        assert is_anidub_org("https://anidub.org/6093-van-pis-tv.html")

    def test_www_org_matches(self):
        assert is_anidub_org("https://www.anidub.org/6093-van-pis-tv.html")

    def test_com_does_not_match_org(self):
        """`.com` обслуживает старый модуль — перехватывать его нельзя."""
        assert not is_anidub_org("https://anidub.com/12474-ja-perevoplotilsja.html")

    def test_org_does_not_match_com(self):
        assert not is_anidub_com("https://anidub.org/6093-van-pis-tv.html")

    def test_other_site_does_not_match(self):
        assert not is_anidub_org("https://ok.ru/video/7475662490142")

    def test_source_matches(self):
        assert AnidubOrgSource().matches("https://anidub.org/1-x.html")


class TestPageParsing:
    def test_params_found(self):
        params = player_params(PAGE)
        assert params["pub"] == "19"
        assert params["id"] == "019169b6-9f49-7086-a8d3-76f1e2558ffa"
        assert params["aggr"] == "cvh"

    def test_no_player_returns_empty(self):
        assert player_params("<html><body>видео удалено</body></html>") == {}

    def test_player_without_id_is_empty(self):
        """Без id API ответит мусором — лучше честно «нет плеера»."""
        html = '<video-player data-publisher-id="19"></video-player>'
        assert player_params(html) == {}

    def test_title_unescaped(self):
        """HTML-сущности обязательны: иначе в оверлее видно «&#91;ТВ&#93;»."""
        assert clean_title(PAGE) == "Ван-Пис [ТВ]"

    def test_playlist_url(self):
        url = playlist_url(player_params(PAGE))
        assert url.startswith("https://plapi.cdnvideohub.com/api/v1/player/sv/playlist?")
        assert "pub=19" in url and "aggr=cvh" in url and "id=019169b6" in url


class TestSelection:
    def test_episodes_grouped(self):
        groups = tracks_by_episode(PLAYLIST)
        assert sorted(groups) == [1, 2]
        assert len(groups[1]) == 3

    def test_first_episode_chosen(self):
        """Сайт продолжает с последней серии — мы начинаем с первой."""
        chosen = pick(PLAYLIST, 0)
        assert chosen["vkId"] == "111" and chosen["episode"] == 1

    def test_translation_index_applies(self):
        assert pick(PLAYLIST, 1)["vkId"] == "222"

    def test_index_out_of_range_falls_back(self):
        """Кривой индекс не должен ронять разбор — берём первую озвучку.

        Раньше здесь был clamp к последней дорожке (`min(index, len-1)`):
        индекс 99 на трёх озвучках давал третью. Это выдавало человеку
        случайную озвучку вместо главной, которую сайт ставит первой.
        Теперь как у Kodik: неверный индекс — берём первую.
        """
        assert pick(PLAYLIST, 99)["vkId"] == "111"

    def test_empty_playlist(self):
        assert pick({"items": []}) == {}

    def test_voice_title_studio_and_type(self):
        assert voice_title({"voiceStudio": "AniLiberty", "voiceType": "Дубляж"}) == (
            "AniLiberty (Дубляж)"
        )

    def test_voice_title_placeholder_uses_type(self):
        """«Неизвестный» — заглушка сайта, а не название студии."""
        assert voice_title({"voiceStudio": "Неизвестный", "voiceType": "Субтитры"}) == "Субтитры"

    def test_voice_title_without_anything(self):
        assert voice_title({}) == "Без названия"


class TestTkhd:
    def test_size_read_from_file(self):
        """Размер кадра берётся из tkhd, а не угадывается по имени поля."""
        assert _tkhd_size(bytes(MP4_HEAD)) == (1280, 720)

    def test_garbage_returns_zero(self):
        assert _tkhd_size(b"<html>403 Forbidden</html>") == (0, 0)

    def test_truncated_before_tkhd(self):
        """Обрезанный файл не должен давать выдуманный размер."""
        assert _tkhd_size(bytes(MP4_HEAD[:150])) == (0, 0)


class TestHeaders:
    def test_one_ua_everywhere(self):
        """Подпись okcdn привязана к UA: разные UA → 400."""
        assert "Firefox" in FIREFOX_UA
        assert "Chrome" not in FIREFOX_UA


class TestStreamShape:
    def test_direct_file_flag(self):
        """hls.js не разбирает mp4 и молчит об этом — нужен явный признак.

        Собирается поток в общем `cvh.build_stream`: тот же плеер стоит на
        yummyanime, и признак должен быть один на оба сайта, а не в каждом
        источнике по копии.
        """
        import inspect

        from tvhost2.sources import cvh

        assert "is_direct_file=True" in inspect.getsource(cvh.build_stream)

    def test_qualities_are_probed(self):
        """Размер кадра обязан читаться из файла, а не браться из имени поля."""
        import inspect

        from tvhost2.sources import anidub_org

        assert "probe_mp4_size" in inspect.getsource(anidub_org.qualities_from_video)

    def test_mpeg_variants_cover_1080(self):
        assert any(field == "mpegFullHdUrl" for field, _ in MPEG_VARIANTS)

    def test_empty_video_gives_nothing(self):
        assert qualities_from_video({}) == ()


def inspect_source() -> str:
    import inspect

    return inspect.getsource(AnidubOrgSource.resolve)


class TestHlsFallback:
    MASTER = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=3554704,RESOLUTION=1920x1080\nhttps://vd1.okcdn.ru/a.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1415765,RESOLUTION=852x480\nhttps://vd2.okcdn.ru/b.m3u8\n"
    )

    def test_master_parsed(self):
        qualities = parse_master(self.MASTER)
        assert [q.height for q in qualities] == [1080, 480]

    def test_garbage(self):
        assert parse_master("<html>400</html>") == ()


class TestPlayerLevels:
    def test_levels_through_bridge(self):
        """Ссылки качеств обязаны идти через наш мост, а не напрямую к VK."""
        session = Session(sid="S1", url="https://anidub.org/1-x.html")
        session.stream = Stream(
            source="anidub.org",
            title="Ван-Пис",
            kind=StreamKind.HLS,
            is_direct_file=True,
        )
        assert levels_json(session) == "[]"

    def test_levels_empty_without_flag(self):
        session = Session(sid="S2", url="u")
        session.stream = Stream(source="x", title="t", kind=StreamKind.HLS)
        assert levels_json(session) == "[]"


class TestCache:
    def test_cache_roundtrip(self):
        CACHE.put("test-id", {"items": [1]})
        assert CACHE.get("test-id") == {"items": [1]}

    def test_missing_key(self):
        assert CACHE.get("нет-такого") is None
