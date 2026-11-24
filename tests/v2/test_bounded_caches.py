"""У каждого долгоживущего словаря есть предел — иначе это утечка.

Замерено 24.11.2026: без пределов 25 000 сессий давали +10.6 МБ, и они
жили все шесть часов — TTL проверялся только при чтении ключа, а к ключам,
к которым не вернулись, никто не читает. Плейлисты CVH (по 615 КБ),
таблицы эмбедов и дорожки yummy вели себя так же.

Эти тесты дешёвые и не ходят в сеть: они проверяют сам механизм вытеснения.
"""

from __future__ import annotations

import time

from tvhost2.core.cache import SizeCache, TTLCache, file_key


class TestTTLCache:
    def test_limit_holds(self) -> None:
        cache: TTLCache[int] = TTLCache(limit=3, ttl_s=0)
        for i in range(100):
            cache.put(f"k{i}", i)
        assert len(cache) == 3

    def test_newest_survives(self) -> None:
        """Вытесняется самое давнее, а не что попало."""
        cache: TTLCache[int] = TTLCache(limit=2, ttl_s=0)
        cache.put("old", 1)
        cache.put("mid", 2)
        cache.put("new", 3)
        assert cache.get("new") == 3
        assert cache.get("old") is None

    def test_ttl_expires(self) -> None:
        cache: TTLCache[int] = TTLCache(limit=10, ttl_s=0.02)
        cache.put("k", 1)
        assert cache.get("k") == 1
        time.sleep(0.05)
        assert cache.get("k") is None

    def test_zero_ttl_means_no_expiry(self) -> None:
        """Размеры файлов не протухают — им срок не нужен."""
        cache: TTLCache[int] = TTLCache(limit=10, ttl_s=0)
        cache.put("k", 1)
        assert cache.get("k") == 1

    def test_missing_key(self) -> None:
        assert TTLCache[int](limit=2, ttl_s=1).get("нет") is None

    def test_limit_must_be_positive(self) -> None:
        try:
            TTLCache[int](limit=0, ttl_s=1)
        except ValueError:
            return
        raise AssertionError("нулевой предел должен быть ошибкой, а не молчанием")


class TestSizeCache:
    def test_limit_holds(self) -> None:
        cache = SizeCache(limit=5)
        for i in range(500):
            cache.put(f"f{i}", (1280, 720))
        assert len(cache) == 5

    def test_keeps_value(self) -> None:
        cache = SizeCache(limit=2)
        cache.put("a", (1920, 1080))
        assert cache.get("a") == (1920, 1080)

    def test_missing(self) -> None:
        assert SizeCache(limit=2).get("нет") is None


class TestFileKey:
    def test_id_wins_over_signature(self) -> None:
        """Подпись в адресе меняется каждые минуты, id — нет."""
        first = file_key("mpegHighUrl", "https://x/id/777/a?expires=1&sig=aaa")
        second = file_key("mpegHighUrl", "https://x/id/777/b?expires=2&sig=bbb")
        assert first == second

    def test_field_separates_variants(self) -> None:
        """720p и 1080p одного файла — разные ключи."""
        assert file_key("mpegHighUrl", "https://x/id/7/") != file_key(
            "mpegFullHdUrl", "https://x/id/7/"
        )

    def test_no_id_uses_path(self) -> None:
        assert (
            file_key("mpegHighUrl", "https://x/file.mp4?sig=1") == "mpegHighUrl:https://x/file.mp4"
        )


class TestRealCaches:
    """Наши боевые кэши тоже обязаны быть ограниченными."""

    def test_cvh_playlist_cache_bounded(self) -> None:
        from tvhost2.sources import cvh

        for i in range(200):
            cvh.CACHE.put(f"site:{i}", {"items": [{"x": "y"}]})
        assert len(cvh.CACHE) <= cvh.MAX_PLAYLISTS

    def test_cvh_size_cache_bounded(self) -> None:
        from tvhost2.sources import cvh

        for i in range(1000):
            cvh.SIZES.put(f"f{i}", (1280, 720))
        assert len(cvh.SIZES) <= cvh.MAX_SIZES

    def test_sessions_bounded(self) -> None:
        """25 000 сессий без предела — это +10.6 МБ и все шесть часов."""
        from tvhost2.web.session import MAX_SESSIONS, SessionStore

        store = SessionStore()
        for i in range(500):
            store.create(f"https://example.com/{i}")
        assert len(store.all()) <= MAX_SESSIONS

    def test_yummy_caches_bounded(self) -> None:
        from tvhost2.sources.yummy import YummyAnimeSource

        source = YummyAnimeSource()
        for i in range(300):
            source._titles.put(f"https://x/{i}", f"Тайтл {i}")
            source._players.put(f"https://x/{i}", {"Kodik"})
            source._cache.put(f"https://x/{i}", [{"iframe": "u", "dubbing": "d"}])
        assert len(source._titles) <= 32
        assert len(source._players) <= 32
        assert len(source._cache) <= 32

    def test_embed_cache_bounded(self) -> None:
        from tvhost2.sources.kodik_embed import KodikEmbedSource

        source = KodikEmbedSource()
        for i in range(300):
            source._cache.put(f"https://x/{i}", "https://player/1")
        assert len(source._cache) <= 32
