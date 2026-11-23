"""Этап 1: тесты ядра. Без сети, без Qt — только логика."""

from __future__ import annotations

import pytest

from tvhost2.core import (
    LinkNotRecognisedError,
    NeedsCookiesError,
    NoQualitiesError,
    Quality,
    Registry,
    SourceFailedError,
    SourceState,
    Stream,
    StreamKind,
    sort_qualities,
)


def q(height: int, bitrate: int = 1000, width: int | None = None) -> Quality:
    return Quality(
        height=height,
        width=width if width is not None else height * 16 // 9,
        bitrate_kbps=bitrate,
        url=f"https://cdn.example/{height}.m3u8",
    )


# --- Quality ---------------------------------------------------------------


class TestQuality:
    def test_label_gorizontal(self) -> None:
        assert q(1080).label == "1080p"

    def test_vertical_video_keeps_both_sides(self) -> None:
        """ERR-062: вертикальное видео нельзя сводить к одной высоте."""
        vertical = Quality(height=1920, width=1080, bitrate_kbps=4000, url="u")
        assert vertical.is_portrait
        assert vertical.label == "1080x1920 (верт.)"

    def test_rejects_zero_height(self) -> None:
        with pytest.raises(ValueError, match="height"):
            Quality(height=0, width=1920, bitrate_kbps=1000, url="u")

    def test_rejects_fake_bitrate(self) -> None:
        """ERR-067: битрейт обязан быть реальным и положительным."""
        with pytest.raises(ValueError, match="bitrate"):
            Quality(height=720, width=1280, bitrate_kbps=0, url="u")

    def test_rejects_empty_url(self) -> None:
        with pytest.raises(ValueError, match="url"):
            Quality(height=720, width=1280, bitrate_kbps=1000, url="")

    def test_is_frozen(self) -> None:
        """Модель неизменяема: качество нельзя подменить по ходу."""
        quality = q(720)
        with pytest.raises((AttributeError, TypeError)):
            quality.height = 1080  # type: ignore[misc]


# --- Stream ----------------------------------------------------------------


class TestStream:
    def test_best_and_worst(self) -> None:
        stream = Stream(
            source="Rutube",
            title="Тест",
            kind=StreamKind.HLS,
            qualities=(q(1080, 5884), q(720, 2308), q(144, 286)),
        )
        assert stream.best is not None and stream.best.height == 1080
        assert stream.worst is not None and stream.worst.height == 144

    def test_empty_qualities_allowed_for_page_kind(self) -> None:
        """Kodik: сначала страница, потоки придут от браузерной сессии."""
        stream = Stream(source="Kodik", title="Аниме", kind=StreamKind.PAGE)
        assert stream.best is None
        assert stream.worst is None

    def test_by_height_exact(self) -> None:
        stream = Stream(
            source="VK",
            title="",
            kind=StreamKind.HLS,
            qualities=(q(1080), q(720), q(480)),
        )
        assert stream.by_height(720) is not None
        assert stream.by_height(720).height == 720  # type: ignore[union-attr]

    def test_by_height_falls_back_lower(self) -> None:
        """Просили 900 — отдаём ближайшее снизу, а не ошибку."""
        stream = Stream(
            source="VK",
            title="",
            kind=StreamKind.HLS,
            qualities=(q(1080), q(720), q(480)),
        )
        assert stream.by_height(900).height == 720  # type: ignore[union-attr]

    def test_by_height_very_low_returns_worst(self) -> None:
        stream = Stream(
            source="VK",
            title="",
            kind=StreamKind.HLS,
            qualities=(q(1080), q(480)),
        )
        assert stream.by_height(100).height == 480  # type: ignore[union-attr]

    def test_sorted_is_descending(self) -> None:
        stream = Stream(
            source="OK",
            title="",
            kind=StreamKind.HLS,
            qualities=(q(480), q(1080), q(720)),
        )
        heights = [x.height for x in stream.sorted_qualities()]
        assert heights == [1080, 720, 480]

    def test_split_audio_flag(self) -> None:
        """YouTube: видео и звук раздельно."""
        stream = Stream(
            source="YouTube",
            title="",
            kind=StreamKind.HLS_SPLIT,
            qualities=(q(720),),
            audio_url="https://cdn.example/audio.m3u8",
        )
        assert stream.has_audio
        assert stream.audio_url.endswith(".m3u8")

    def test_requires_source_name(self) -> None:
        with pytest.raises(ValueError, match="source"):
            Stream(source="", title="", kind=StreamKind.HLS)

    def test_live_flag(self) -> None:
        stream = Stream(source="Twitch", title="Стрим", kind=StreamKind.HLS, is_live=True)
        assert stream.is_live


# --- sort_qualities --------------------------------------------------------


class TestSortQualities:
    def test_descending(self) -> None:
        result = sort_qualities([q(360), q(1080), q(720)])
        assert [x.height for x in result] == [1080, 720, 360]

    def test_dedup_keeps_best_bitrate(self) -> None:
        """VK отдаёт несколько CDN-хостов на одно качество."""
        result = sort_qualities([q(720, 2000), q(720, 3500), q(720, 1500)])
        assert len(result) == 1
        assert result[0].bitrate_kbps == 3500

    def test_empty(self) -> None:
        assert sort_qualities([]) == ()


# --- Реестр ----------------------------------------------------------------


class _FakeSource:
    def __init__(self, name: str, host: str, state: SourceState = SourceState.OK) -> None:
        self.name = name
        self.host = host
        self.state = state
        self.note = ""

    def matches(self, url: str) -> bool:
        return self.host in url

    def resolve(self, url: str) -> Stream:
        return Stream(source=self.name, title="", kind=StreamKind.HLS)


class TestRegistry:
    def test_register_and_find(self) -> None:
        reg = Registry()
        reg.register(_FakeSource("Rutube", "rutube.ru"))
        found = reg.find("https://rutube.ru/video/abc/")
        assert found is not None and found.name == "Rutube"

    def test_order_matters(self) -> None:
        """Специфичный источник должен проверяться раньше общего."""
        reg = Registry()
        reg.register(_FakeSource("Прямой HLS", "http"))
        reg.register(_FakeSource("VK", "vkvideo.ru"))
        found = reg.find("https://vkvideo.ru/video-1_2")
        assert found is not None and found.name == "Прямой HLS"

    def test_no_match_returns_none(self) -> None:
        reg = Registry()
        reg.register(_FakeSource("Rutube", "rutube.ru"))
        assert reg.find("https://example.com/x") is None

    def test_duplicate_name_rejected(self) -> None:
        reg = Registry()
        reg.register(_FakeSource("VK", "vk.com"))
        with pytest.raises(ValueError, match="уже зарегистрирован"):
            reg.register(_FakeSource("VK", "vkvideo.ru"))

    def test_broken_matches_does_not_break_search(self) -> None:
        """Источник с исключением в matches не должен ломать поиск остальных."""

        class Broken:
            name = "Sломанный"
            state = SourceState.BROKEN
            note = ""

            def matches(self, url: str) -> bool:
                raise RuntimeError("boom")

            def resolve(self, url: str) -> Stream:  # pragma: no cover
                raise NotImplementedError

        reg = Registry()
        reg.register(Broken())
        reg.register(_FakeSource("Rutube", "rutube.ru"))
        found = reg.find("https://rutube.ru/video/abc/")
        assert found is not None and found.name == "Rutube"

    def test_status_table_for_sources_tab(self) -> None:
        """Данные для вкладки «Источники» (запрос 9)."""
        reg = Registry()
        reg.register(_FakeSource("Rutube", "rutube.ru"))
        reg.register(_FakeSource("Vimeo", "vimeo.com", SourceState.NEEDS_COOKIES))
        table = reg.status_table()
        assert table == [
            {"name": "Rutube", "state": "ok", "note": ""},
            {"name": "Vimeo", "state": "needs_cookies", "note": ""},
        ]


# --- Ошибки ----------------------------------------------------------------


class TestErrors:
    def test_link_not_recognised_explains_fix(self) -> None:
        err = LinkNotRecognisedError("https://ya.ru")
        text = err.user_text()
        assert "не распознана" in text
        assert "Что сделать" in text

    def test_needs_cookies_state(self) -> None:
        err = NeedsCookiesError("Vimeo")
        assert err.state is SourceState.NEEDS_COOKIES
        assert "cookies" in err.user_text()

    def test_source_failed_has_reason(self) -> None:
        err = SourceFailedError("Kodik", "подпись не получена")
        assert "Kodik" in err.what
        assert "подпись не получена" in err.why

    def test_no_qualities_is_partial_not_broken(self) -> None:
        """Видео нашлось, просто качеств нет — источник не «сломан»."""
        assert NoQualitiesError("Bilibili").state is SourceState.PARTIAL

    def test_error_is_exception(self) -> None:
        err = LinkNotRecognisedError("x")
        assert isinstance(err, Exception)
        assert str(err)
