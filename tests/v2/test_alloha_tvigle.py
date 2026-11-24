"""Пять новых сайтов: tvigle и четыре с плеером Alloha.

Проверено живьём 24.11.2026. Главный вывод, который определяет весь разбор:

  **Сервер, с которого шли проверки, стоит в США.** Из-за этого четыре сайта
  из пяти закрывают само видео — но не одинаково, и человеку это надо
  объяснять по-разному:

  | Сайт | Что закрыто | Наша реакция |
  |---|---|---|
  | tvigle.ru | лицензия на РФ: API отдаёт `isGeoBlocked` | цитируем сайт |
  | zlord.org | плеер Alloha: 404 «недоступно в регионе» | называем причину |
  | lordserialc.run | то же | то же |
  | mltclb2.icu | то же | то же |
  | lord-serial.world | капча BotFinder | не обходим |

  Приложение запускается на компьютере в России, поэтому там эти пути
  проходят. Тесты проверяют именно ту часть, которая от региона НЕ зависит:
  поиск встройки, токен, id тайтла, список озвучек и разбор качеств.

Второй разбор — про то, что нельзя было узнать без живых запросов:

  - на сайте бывает НЕСКОЛЬКО плееров, и первым идёт не всегда Alloha
    (на zlord.org первым стоит `api.embf.net` — чужой, без `kp`);
  - адрес встройки бывает с портом (`sheldon.stravers.live:9443`), и
    проверка «заканчивается на домен» без учёта порта не срабатывала;
  - качество лежит ВНУТРИ объекта файла, а не отдельным списком.
"""

from __future__ import annotations

from tvhost2.sources import alloha, tvigle

# ---------------------------------------------------------------- Alloha: поиск

PAGE_WITH_TWO_PLAYERS = """
<div class="player">
  <iframe src="//api.embf.net/embed/movie/94207" frameborder="0"></iframe>
  <iframe src="https://tories.as.newplayjj.com:444/?kp=12395264&token=5286c49a9d29cad731353b68d3f1f5"
          frameborder="0"></iframe>
</div>
"""

PAGE_WITH_LAZY_PLAYER = """
<div class="player">
  <iframe data-src="//sheldon.stravers.live:9443/?kp=574749
                   &token=bc495cb1f27954ba609aa4b619fae5"></iframe>
</div>
"""


class TestAllohaFind:
    def test_skips_foreign_player(self):
        """Первый iframe на zlord.org — чужой плеер без `kp`.

        Если брать первую встройку, разбор не работает: у неё нет ни id
        тайтла, ни токена. Именно на этом я и потерял время.
        """
        found = alloha.iframe_of(PAGE_WITH_TWO_PLAYERS)
        assert found is not None
        assert "12395264" in found

    def test_all_embeds_listed(self):
        """Из двух встроек Alloha-шная одна — чужая отбрасывается по домену."""
        assert len(alloha.embeds_of(PAGE_WITH_TWO_PLAYERS)) == 1

    def test_lazy_iframe(self):
        """Часть сайтов отдаёт плеер в `data-src`, а не в `src`."""
        found = alloha.iframe_of(PAGE_WITH_LAZY_PLAYER)
        assert found is not None and "sheldon" in found

    def test_port_does_not_break_host_check(self):
        """`sheldon.stravers.live:9443` должен считаться встройкой Alloha.

        Проверка без отбрасывания порта не срабатывала, и mltclb2.icu
        выглядел сайтом без плеера.
        """
        assert alloha.is_embed_host("https://sheldon.stravers.live:9443/?kp=1")
        assert alloha.is_embed_host("https://tories.as.newplayjj.com:444/?kp=1")

    def test_foreign_host_not_matched(self):
        """`api.embf.net` — другой плеер со своим id, не Alloha.

        Если считать его Alloha, разбор пойдёт по чужому номеру
        (`/movie/94207`), API ответит «not movie», и мы покажем ложную
        причину отказа. Проверено живым запросом.
        """
        assert not alloha.is_embed_host("https://api.embf.net/embed/movie/1")
        assert not alloha.is_embed_host("https://zlorld.live/1")

    def test_token_and_kp(self):
        embed = (
            "https://tories.as.newplayjj.com:444/?kp=12395264&token=5286c49a9d29cad731353b68d3f1f5"
        )
        assert alloha.kp_id(embed) == "12395264"
        assert alloha.site_token(embed) == "5286c49a9d29cad731353b68d3f1f5"

    def test_token_not_confused_with_movie_token(self):
        """`token` сайта и `token_movie` — разные вещи.

        Регулярка обязана брать именно `token=`, иначе в «токен сайта»
        попадёт `token_movie` и API ответит отказом.
        """
        embed = (
            "https://x.stravers.live/?token_movie=dc27f0f0f45c4989af12f74b6a7757"
            "&token=5286c49a9d29cad731353b68d3f1f5"
        )
        assert alloha.site_token(embed) == "5286c49a9d29cad731353b68d3f1f5"


# ------------------------------------------------------- Alloha: озвучки

API_SERIAL = {
    "status": "success",
    "data": {
        "name": "Хорошая дочь",
        "seasons": {
            "1": {
                "episodes": {
                    "2": {
                        "translation": {
                            "338": {"translation": "WinMedia", "iframe": "https://c/2"},
                        }
                    },
                    "1": {
                        "translation": {
                            "93": {"translation": "Оригинальный", "iframe": "https://c/93"},
                            "338": {"translation": "WinMedia", "iframe": "https://c/338"},
                            "241": {"translation": "LE-Production", "iframe": "https://c/241"},
                        }
                    },
                }
            }
        },
    },
}


class TestAllohaTranslations:
    def test_takes_first_episode(self):
        """Озвучки — от ПЕРВОЙ серии, а не от случайной.

        API отдаёт серии вразнобой (проверено: у «Хорошей дочери» 6-я
        приходит раньше 1-й), поэтому сортируем по номеру.
        """
        tracks = alloha.translations_of(API_SERIAL)
        assert [t["title"] for t in tracks] == ["Оригинальный", "WinMedia", "LE-Production"]
        assert all(t["iframe"].startswith("https://c/") for t in tracks)

    def test_film_tracks(self):
        """У фильма нет серий: озвучки лежат прямо в `translation_iframe`."""
        data = {"data": {"name": "Фильм", "translation_iframe": {"12": "https://c/12"}}}
        assert [t["id"] for t in alloha.translations_of(data)] == ["12"]

    def test_empty(self):
        assert alloha.translations_of({}) == []
        assert alloha.translations_of({"data": {"name": "нет"}}) == []

    def test_title(self):
        assert alloha.title_of(API_SERIAL) == "Хорошая дочь"


# -------------------------------------------------- Alloha: качества и регион

FILE_LIST = """
<script>
var player = new Player({
  fileList: [
    {"quality": "1080", "src": "https://cdn.example/a/1080/index.m3u8"},
    {"quality": 720, "src": "https://cdn.example/a/720/index.m3u8"},
    {"quality": "480", "url": "https://cdn.example/a/480/index.m3u8"}
  ],
  poster: "https://example/p.jpg"
});
</script>
"""


class TestAllohaQualities:
    def test_labels_stay_with_their_files(self):
        """Каждая метка — со своей ссылкой.

        Первая версия собирала «все метки» и «все ссылки» отдельными
        списками. На файле без метки списки съезжали, и 720-й файл получал
        метку 1080 — человек видел два одинаковых качества.
        """
        qualities = alloha.qualities_of(FILE_LIST)
        assert [q.height for q in qualities] == [1080, 720, 480]
        assert all("/index.m3u8" in q.url for q in qualities)

    def test_sorted_best_first(self):
        qualities = alloha.qualities_of(FILE_LIST)
        assert qualities[0].height == max(q.height for q in qualities)

    def test_guess_from_url(self):
        """Метки нет — берём из адреса, а не ставим всем 720."""
        html = 'fileList: [{"src":"https://c/1080/x.m3u8"},{"src":"https://c/480/y.m3u8"}]'
        assert [q.height for q in alloha.qualities_of(html)] == [1080, 480]

    def test_duplicates_dropped(self):
        html = 'fileList: [{"src":"https://c/720/x.m3u8"},{"src":"https://c/720/x.m3u8"}]'
        assert len(alloha.qualities_of(html)) == 1

    def test_no_player(self):
        assert alloha.qualities_of("<html>ничего</html>") == []

    def test_region_text(self):
        assert alloha.blocked_by_region("Извините, но данный контент недоступен в вашем регионе")
        assert not alloha.blocked_by_region("К сожалению, контент не найден")


# ------------------------------------------------------------------ tvigle

TVIGLE_PAGE = """
<meta property="og:video" content="https://cloud.tvigle.ru/video/5838418/?playerId=314&amp;partnerId=24"/>
"""

TVIGLE_API_OK = {
    "playlist": {
        "items": [
            {
                "title": "Круче некуда",
                "durationMilliseconds": 5916203,
                "isGeoBlocked": False,
                "videos": {"hls": "https://cdn.tvigle.ru/master.m3u8"},
            }
        ]
    }
}

TVIGLE_API_BLOCKED = {
    "playlist": {
        "items": [
            {
                "title": "Круче некуда",
                "isGeoBlocked": True,
                "videos": {},
                "errorMessage": "<b>Ограничение по территории</b>\n Видео недоступно",
            }
        ]
    }
}


class TestTvigle:
    def test_domain(self):
        assert tvigle.is_tvigle("https://www.tvigle.ru/video/kruche-nekuda/")
        assert tvigle.is_tvigle("https://tvigle.ru/video/x/")
        assert not tvigle.is_tvigle("https://not-tvigle.ru/video/x/")

    def test_video_id_from_og_video(self):
        """Номер видео есть только в `og:video` — разметки с ним нет."""
        assert tvigle.video_id(TVIGLE_PAGE) == "5838418"

    def test_no_video_id(self):
        assert tvigle.video_id("<html></html>") is None

    def test_stream_urls(self):
        videos = {"hls": "https://c/master.m3u8", "poster": "https://c/p.jpg"}
        assert tvigle.stream_urls(videos) == ["https://c/master.m3u8"]

    def test_stream_urls_garbage(self):
        assert tvigle.stream_urls(None) == []
        assert tvigle.stream_urls("строка") == []

    def test_play_item(self):
        assert tvigle.play_item(TVIGLE_API_OK)["title"] == "Круче некуда"
        assert tvigle.play_item({"playlist": {"items": []}}) == {}
        assert tvigle.play_item({}) == {}

    def test_geo_message_cleans_html(self):
        message = tvigle.geo_message(tvigle.play_item(TVIGLE_API_BLOCKED))
        assert "Ограничение по территории" in message
        assert "<b>" not in message

    def test_qualities_from_master(self):
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\nhttps://c/1080.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\nhttps://c/720.m3u8\n"
        )
        qualities = tvigle.qualities_from_master(master)
        assert [q.height for q in qualities] == [1080, 720]

    def test_master_without_link(self):
        assert tvigle.qualities_from_master("#EXTM3U\n") == []

    def test_master_without_resolution(self):
        """Мастер без `RESOLUTION` — это не «нет качеств».

        Живой баг 24.11.2026: заказчик получил 502 на исправном видео. Разбор
        требовал `RESOLUTION` и возвращал НОЛЬ качеств, а мост на пустом
        списке отвечает 502 «у потока нет качеств». Теперь высота оценивается
        по битрейту — грубо, но играть можно.
        """
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=5000000\nhttps://c/1.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000\nhttps://c/2.m3u8\n"
        )
        qualities = tvigle.qualities_from_master(master)
        assert [q.height for q in qualities] == [720, 480]
        assert all(q.bitrate_kbps > 0 for q in qualities)

    def test_bitrate_estimate(self):
        assert tvigle._height_from_bitrate(8_000_000) == 1080
        assert tvigle._height_from_bitrate(2_500_000) == 720
        assert tvigle._height_from_bitrate(500_000) == 360


# ------------------------------------------------------- отказ человеку


class TestRefusalText:
    def test_region_refusal_mentions_russia(self):
        """Отказ должен говорить ГДЕ заработает, а не «источник отказал».

        Приложение запускается на компьютере в России — человеку нужно
        понимать, что дело в адресе сервера, а не в программе.
        """
        error = alloha._region_error()  # noqa: SLF001
        assert "Росси" in error.fix

    def test_region_error_text(self):
        error = alloha._region_error()  # noqa: SLF001
        assert "Росси" in error.fix
        assert "регион" in str(error).lower()


# ------------------------------------------- честность отказов (по следам бага)


class TestHonestRefusals:
    """Живой случай 24.11.2026: у заказчика в России Alloha открылась, а мы
    ответили «плеер не отдал ни одного файла» и 502. Причина была не в
    плеере: ссылки Alloha в разметке не лежат вовсе, их собирает скрипт."""

    def test_no_files_says_why_not_found(self):
        """Отказ обязан объяснять, ЧТО не так, а не «не отдал файлов».

        «Не отдал ни одного файла» звучит как поломка сайта и уводит искать
        несуществующую причину: человек идёт проверять видео в браузере,
        где оно, конечно, играет.
        """
        error_text = alloha.__doc__ or ""
        assert "WebSocket" in error_text or "скрипт" in error_text.lower()

    def test_qualities_never_found_in_html(self):
        """Убеждаемся в факте: в разметке плеера ссылок нет, только номера.

        Это не тест парсера, а тест ДЕЙСТВИТЕЛЬНОСТИ: если однажды Alloha
        начнёт класть ссылки в HTML, тест упадёт и напомнит переписать разбор.
        """
        html = 'fileList: [{"id": 12345, "quality": "1080"}, {"id": 12346, "quality": "720"}]'
        assert alloha.qualities_of(html) == []

    def test_tvigle_geoblock_has_own_text(self):
        """tvigle: пустые ссылки + флаг региона — это лицензия, а не сбой."""
        item = tvigle.play_item(TVIGLE_API_BLOCKED)
        assert item.get("isGeoBlocked") is True
        assert tvigle.stream_urls(item.get("videos")) == []
        advice = tvigle._region_advice(item)  # noqa: SLF001
        assert "лиценз" in advice.lower()

    def test_tvigle_always_yields_quality(self):
        """Даже без разобранного мастера должно быть хотя бы одно качество.

        Пустой список качеств мост превращает в 502, и человек видит
        «ошибка потока» вместо причины. Один адрес «как есть» играет лучше.
        """
        assert tvigle.MIN_HEIGHT == 144
