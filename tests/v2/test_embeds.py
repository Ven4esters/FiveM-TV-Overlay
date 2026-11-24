"""Сайты с чужим плеером: Kodik-встройка, YummyAnime и честные отказы.

Заказчик просил ответить на три вопроса, и здесь ответы закреплены тестами:

1. **AniBoom (animego.me)** — сайт отдаёт 500 на любой запрос с зарубежного
   адреса: это DDoS-Guard, а не наша ошибка. Тест на сам сайт сделать
   нельзя (он не отвечает), но можно закрепить, что мы не врём про успех
   и что плеер Kodik, на котором он работает, у нас разбирается.

2. **ani-media.online** — поддерживается: встройка Kodik находится в
   `data-src` ленивого iframe.

3. **old.yummyani.me** — поддерживается через открытый API, но только там,
   где сайт отдаёт Kodik. Прочие плееры сайта — честный отказ.

Плюс отдельный тест на баг, который стоил часа: у сериалов Kodik хэш в
адресе iframe подменяется, настоящие `type/hash/id` лежат в `vInfo`.
"""

from __future__ import annotations

import json

import pytest

from tvhost2.sources import kodik, kodik_embed, yummy

# --------------------------------------------------------------- Kodik vInfo

VINFO_HTML = """
<script>
    var vInfo = {};
    vInfo.type = 'seria';
    vInfo.hash = '46777063de315556486d14dbc161eae7';
    vInfo.id = '1692483';
</script>
"""


class TestKodikVinfo:
    def test_reads_real_parameters(self):
        found = kodik.page_vinfo(VINFO_HTML)
        assert found == {
            "type": "seria",
            "hash": "46777063de315556486d14dbc161eae7",
            "id": "1692483",
        }

    def test_missing_video_means_empty(self):
        """У фильмов `vInfo` может не быть — тогда работаем по адресу."""
        assert kodik.page_vinfo("<html>ничего</html>") == {}

    def test_season_kind_parses(self):
        """Тип `season` встречается у сериалов yummyani — раньше он не
        разбирался, и ссылка считалась «не Kodik»."""
        ref = kodik.parse_player_url(
            "https://kodikplayer.com/season/92734/2e0dcea51a73a774f33c775d59e6fdfd/720p?episode=1"
        )
        assert ref is not None
        assert ref.kind == "season"
        assert ref.video_id == "92734"


# ----------------------------------------------------------- встройка Kodik

ANI_MEDIA_HTML = """
<div class="player">
  <iframe data-src="//kodikplayer.com/serial/37803/1bd6a36cae8a372caa225c430691bd4d/720p"
          data-pid="1541543c50b2a1b44c9fcce8dc086291"></iframe>
</div>
"""


class TestEmbedFind:
    def test_finds_lazy_src(self):
        """ani-media отдаёт iframe лениво: адрес в `data-src`, а не в `src`."""
        assert kodik_embed.find_embed(ANI_MEDIA_HTML) == (
            "https://kodikplayer.com/serial/37803/1bd6a36cae8a372caa225c430691bd4d/720p"
        )

    def test_adds_scheme(self):
        found = kodik_embed.find_embed(ANI_MEDIA_HTML)
        assert found is not None and found.startswith("https://")

    def test_no_player(self):
        assert kodik_embed.find_embed("<html><iframe src='//vk.com/video'></iframe></html>") is None

    def test_direct_player_url_is_not_embed_page(self):
        """Прямую ссылку на плеер должен брать kodik.py, а не обёртка.

        Иначе два источника спорят за один адрес, и озвучки Kodik теряются:
        обёртка их не раздаёт.
        """
        assert kodik_embed.is_embed_page("https://kodikplayer.com/seria/1420137/x/720p") is False
        assert kodik_embed.is_embed_page("https://ani-media.online/anime-seriali/1575-86.html")


# --------------------------------------------------------------- YummyAnime

YUMMY_JSON = {
    "response": [
        {
            "number": 1,
            "data": {"player": "Плеер Kodik", "dubbing": "Озвучка JAM"},
            "iframe_url": "//kodikplayer.com/season/92734/aaa/720p?episode=1",
        },
        {
            "number": 1,
            "data": {"player": "Плеер Kodik", "dubbing": "Озвучка AniDUB"},
            "iframe_url": "//kodikplayer.com/season/92562/bbb/720p?episode=1",
        },
        {
            "number": 1,
            "data": {"player": "Плеер CVH", "dubbing": "Озвучка StudioBand"},
            "iframe_url": "//ru.yummyani.me/iframeCVH.html?token_movie=1",
        },
        {
            "number": 2,
            "data": {"player": "Плеер Kodik", "dubbing": "Озвучка JAM"},
            "iframe_url": "//kodikplayer.com/season/92734/ccc/720p?episode=2",
        },
    ]
}


class TestYummyParsing:
    def test_takes_only_first_episode(self):
        """13 серий × 23 озвучки нельзя показывать как 284 «озвучки»."""
        tracks = yummy.kodik_embeds(YUMMY_JSON)
        assert len(tracks) == 2
        assert all("aaa" in t["iframe"] or "bbb" in t["iframe"] for t in tracks)

    def test_skips_other_players(self):
        """Чужие плееры не наши: у них свои токены и свой разбор."""
        tracks = yummy.kodik_embeds(YUMMY_JSON)
        assert not any("iframecvh" in t["iframe"].lower() for t in tracks)

    def test_keeps_site_order(self):
        """Порядок как на сайте: человек видит тот же список, что в браузере."""
        assert [t["dubbing"] for t in yummy.kodik_embeds(YUMMY_JSON)] == [
            "Озвучка JAM",
            "Озвучка AniDUB",
        ]

    def test_drops_duplicate_addresses(self):
        """25 подписей на 20 адресов: две подписи на один поток — обман."""
        data = {
            "response": [
                {
                    "number": 1,
                    "data": {"player": "Плеер Kodik", "dubbing": "A"},
                    "iframe_url": "//kodikplayer.com/season/1/x/720p",
                },
                {
                    "number": 1,
                    "data": {"player": "Плеер Kodik", "dubbing": "B"},
                    "iframe_url": "//kodikplayer.com/season/1/x/720p",
                },
            ]
        }
        assert len(yummy.kodik_embeds(data)) == 1

    def test_page_id(self):
        html = '<meta id="page_id" name="page_id" content="10618">'
        assert yummy.page_id(html) == "10618"

    def test_no_page_id(self):
        assert yummy.page_id("<html></html>") is None

    def test_videos_url_keeps_mirror(self):
        """Зеркала у сайта разные: ходить надо на то, что открыл человек."""
        assert yummy.videos_url("https://old.yummyani.me/x", "10618") == (
            "https://old.yummyani.me/api/anime/10618/videos"
        )


class TestYummyPlayers:
    def test_player_names(self):
        assert yummy.players_present(YUMMY_JSON) == {"Kodik", "CVH"}

    def test_broken_json(self):
        assert yummy.players_present({"нет": "ответа"}) == set()
        assert yummy.players_present(json.loads("{}")) == set()

    def test_hosts(self):
        assert yummy.is_yummy("https://old.yummyani.me/catalog/item/x")
        assert yummy.is_yummy("https://yummyani.me/x")
        assert not yummy.is_yummy("https://ani-media.online/x")


class TestMessages:
    def test_refusal_names_the_player(self):
        """Заказчик просил говорить прямо: причина — плеер, а не «не умеем»."""
        from tvhost2.core.errors import SourceFailedError

        source = yummy.YummyAnimeSource()
        source._players["https://old.yummyani.me/catalog/item/x"] = {"CVH"}
        with pytest.raises(SourceFailedError) as caught:
            source.resolve("https://old.yummyani.me/catalog/item/x")
        assert "CVH" in str(caught.value)
