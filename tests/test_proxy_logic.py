"""Тесты чистой логики прокси: определение типа ссылки и выбор потока.

Эти функции решают «играть как файл или как поток» и «какую дорожку
выбрать». Ошибка здесь видна пользователю как «видео не открывается»,
поэтому границы проверяются отдельно.

Сеть не используется.
"""

from __future__ import annotations

import pytest

from tvhost import proxy


class TestLoginWalled:
    """Заведомо закрытые сервисы определяются до сетевого запроса."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.netflix.com/watch/123",
            "https://www.kinopoisk.ru/film/123",
            "https://www.ivi.ru/watch/123",
        ],
    )
    def test_closed_services_detected(self, url):
        walled, reason = proxy.is_login_walled(url)
        assert walled is True
        assert reason

    def test_open_site_is_not_walled(self):
        walled, _reason = proxy.is_login_walled("https://example.com/video.mp4")
        assert walled is False

    def test_vk_video_is_not_considered_walled(self):
        """У VK есть открытые ролики — их надо пробовать, а не отсекать."""
        walled, _reason = proxy.is_login_walled("https://vk.com/video-220020068_456260671")
        assert walled is False

    def test_empty_url_is_safe(self):
        walled, _reason = proxy.is_login_walled("")
        assert walled is False


class TestProbablyDirect:
    """Определение «это готовый файл» против «это плейлист»."""

    def test_mp4_is_direct(self):
        direct, hls = proxy.is_probably_direct("https://cdn/x/movie.mp4")
        assert direct is True
        assert hls is False

    def test_m3u8_is_hls_not_direct(self):
        direct, hls = proxy.is_probably_direct("https://cdn/x/index.m3u8")
        assert direct is False
        assert hls is True

    def test_webm_is_direct(self):
        direct, _hls = proxy.is_probably_direct("https://cdn/x/clip.webm?x=1")
        assert direct is True

    def test_page_is_neither(self):
        direct, hls = proxy.is_probably_direct("https://example.com/page")
        assert direct is False
        assert hls is False

    def test_empty_url_is_safe(self):
        assert proxy.is_probably_direct("") == (False, False)


class TestPickDirect:
    """Выбор готовой ссылки: h264 mp4 до 720p с аудио в приоритете."""

    @staticmethod
    def _format(**kwargs) -> dict:
        base = {
            "url": "https://cdn/x",
            "vcodec": "avc1",
            "acodec": "mp4a",
            "height": 720,
            "protocol": "https",
        }
        base.update(kwargs)
        return base

    def test_prefers_h264_with_audio(self):
        formats = [
            self._format(url="https://cdn/vp9", vcodec="vp9", acodec="opus"),
            self._format(url="https://cdn/avc", vcodec="avc1", acodec="mp4a"),
        ]
        assert proxy.pick_direct(formats) == "https://cdn/avc"

    def test_prefers_up_to_720p(self):
        """1080p не берём: телевизор в игре его не тянет."""
        formats = [
            self._format(url="https://cdn/1080", height=1080),
            self._format(url="https://cdn/720", height=720),
        ]
        assert proxy.pick_direct(formats) == "https://cdn/720"

    def test_video_only_is_less_preferred(self):
        """Дорожка без аудио — крайний случай, лучше с аудио даже в меньшем качестве."""
        formats = [
            self._format(url="https://cdn/silent", acodec="none", height=1080),
            self._format(url="https://cdn/sound", height=360),
        ]
        assert proxy.pick_direct(formats) == "https://cdn/sound"

    def test_empty_list_returns_none(self):
        assert proxy.pick_direct([]) is None


class TestKnownMediaHost:
    """Сайты со штатным разбором не должны попадать в общий перехват."""

    @pytest.mark.parametrize(
        "url",
        ["https://rutube.ru/x", "https://vk.com/video1", "https://youtube.com/watch"],
    )
    def test_known_hosts(self, url):
        assert proxy._is_known_media_host(url) is True

    def test_subdomain_matches(self):
        assert proxy._is_known_media_host("https://www.rutube.ru/x") is True

    def test_unknown_host(self):
        assert proxy._is_known_media_host("https://example.com/x") is False

    def test_lookalike_domain_does_not_match(self):
        """rutube.ru.evil.com — не Rutube: иначе чужой сайт получит доверие."""
        assert proxy._is_known_media_host("https://rutube.ru.evil.com/x") is False

    def test_empty_url_is_safe(self):
        assert proxy._is_known_media_host("") is False


class TestRutubeDetection:
    """Rutube идёт отдельной веткой — проверяем, что делегирование работает."""

    def test_rutube_url(self):
        assert proxy._is_rutube("https://rutube.ru/video/abc/") is True

    def test_not_rutube(self):
        assert proxy._is_rutube("https://example.com/x") is False
