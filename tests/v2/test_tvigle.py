"""Tvigle: номер видео из `og:video` и ответы открытого API.

Проверки только на то, что не зависит от региона: номер видео из разметки
и разбор ответов открытого API.
"""

from __future__ import annotations

from tvhost2.sources import tvigle

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
