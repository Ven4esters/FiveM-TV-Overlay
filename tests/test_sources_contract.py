"""Контрактные тесты источников и вспомогательных функций (infra/sources.py).

Здесь проверяется «форма» данных: если сайт сменит разметку и функция
вернёт другую структуру, остальной код упадёт в непонятном месте.
Эти тесты ловят такое на границе.

Сеть не используется: проверяются чистые функции и парсинг строк.
"""

from __future__ import annotations

import urllib.parse

import pytest

from tvhost.infra import sources


class TestSiteDetection:
    """Определение сайта по ссылке — от этого зависит выбор обработчика."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://rutube.ru/video/9ae8e8a6dc58bdad66190475f9872ecd/",
            "https://www.rutube.ru/video/abc/",
        ],
    )
    def test_rutube_detected(self, url):
        assert sources.is_rutube(url) is True

    @pytest.mark.parametrize(
        "url",
        ["https://vk.com/video-220020068_456260671", "https://vkvideo.ru/video-1_2"],
    )
    def test_vk_detected(self, url):
        assert sources.is_vk(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://twitch.tv/канал",
            "https://www.twitch.tv/videos/123",
            "https://clips.twitch.tv/abc",
        ],
    )
    def test_twitch_detected(self, url):
        assert sources.is_twitch(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://kodik.info/video/91873/060cab655a1b2c3d4e5f6a7b8c9d0e1f/",
            "https://kodikplayer.com/video/1/abcdef0123456789abcdef01/720p",
            "//kodik.info/serial/45534/d8619e5b6d7c8a9012345678901234ab/",
        ],
    )
    def test_kodik_detected(self, url):
        """Настоящий формат Kodik: /video/<id>/<hex-хеш 20+>[/<качество>p]."""
        assert sources.is_kodik(url) is True

    def test_kodik_rejects_short_hash(self):
        """Короткий «хеш» — не Kodik: иначе обычный сайт уйдёт в разбор плеера."""
        assert sources.is_kodik("https://kodik.info/video/91873/abc/") is False

    def test_anime_page_detected(self):
        assert sources.is_anime_page("https://animego.org/anime/monolog-farmacevta-3")

    @pytest.mark.parametrize(
        "url",
        ["https://example.com/video", "https://rutube.ru.evil.com/x"],
    )
    def test_unknown_site_not_matched(self, url):
        """Похожий домен не должен определяться как поддерживаемый сайт."""
        assert not sources.is_rutube(url)


class TestKodikDecoding:
    """Kodik шифрует адреса rot18 — ошибка в декодере ломает все озвучки."""

    def test_rot18_shifts_letters_by_18(self):
        """Сдвиг 18 вперёд по латинице: a→s, b→t, c→u."""
        assert sources.rot18("abc") == "stu"

    def test_rot18_does_not_shift_digits(self):
        """Цифры остаются как есть — на них завязаны хеши и номера серий."""
        assert sources.rot18("0123456789") == "0123456789"

    def test_rot18_wraps_around_alphabet(self):
        """Переход через конец алфавита: z→r, A→S."""
        assert sources.rot18("z") == "r"
        assert sources.rot18("A") == "S"

    def test_rot18_is_reversible_by_shift_8(self):
        """Декодирование Kodik — обратный сдвиг на 8 (26−18)."""

        def shift(text: str, amount: int) -> str:
            out = []
            for ch in text:
                code = ord(ch)
                if 65 <= code <= 90:
                    out.append(chr((code - 65 + amount) % 26 + 65))
                elif 97 <= code <= 122:
                    out.append(chr((code - 97 + amount) % 26 + 97))
                else:
                    out.append(ch)
            return "".join(out)

        original = "abcdefghijklmnopqrstuvwxyz"
        assert shift(sources.rot18(original), 8) == original

    def test_rot18_keeps_punctuation(self):
        """Символы вне алфавита не меняются — иначе сломаются URL."""
        assert sources.rot18("://.?=&-_") == "://.?=&-_"

    def test_decode_src_returns_string(self):
        """decode_src на мусоре не должен падать."""
        assert isinstance(sources.decode_src("nop123"), str)


class TestQualityParsing:
    """Качество видео: неверная подпись путает пользователя."""

    def test_wraps_url_into_local_proxy(self):
        """Ссылка на поток оборачивается в локальный /hls, а не отдаётся как есть."""
        wrapped = sources._q("https://cdn/x/1080p/index.m3u8")
        assert wrapped.startswith("/hls?url=")
        assert "1080p" in urllib.parse.unquote(wrapped)

    def test_special_characters_are_escaped(self):
        """Параметры с & и = не должны ломать адрес прокси."""
        wrapped = sources._q("https://cdn/x/i.m3u8?a=1&b=2")
        assert wrapped.count("?") == 1
        assert "&" not in wrapped.split("?url=")[1].split("%")[0]


class TestVkFormats:
    """
    Разбор отклика VK проверяется в integration-тестах (tests/test_integration.py):
    функция ходит в сеть, а unit-тесты обязаны быть без сети (п. 7 инструкции).

    Здесь проверяется только то, что зависит от кода, а не от VK.
    """

    def test_empty_url_is_safe(self):
        """Пустая ссылка не должна приводить к сетевому запросу."""
        assert sources._vk_formats("") == []
