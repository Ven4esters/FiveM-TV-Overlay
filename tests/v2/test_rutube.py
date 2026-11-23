"""Этап 2: Rutube. Сеть эмулируется — тесты быстрые и не зависят от сайта."""

from __future__ import annotations

import pytest

from tvhost2.core import StreamKind, registry
from tvhost2.sources import RutubeSource, register_all, video_id
from tvhost2.sources.rutube import EMBED_URL

# Реальный ответ yt-dlp на Rutube (сокращён, структура подлинная).
REAL_FORMATS = [
    {
        "format_id": "hls-1080",
        "protocol": "m3u8_native",
        "height": 1080,
        "width": 1920,
        "tbr": 5884,
        "vcodec": "avc1",
        "acodec": "mp4a",
        "url": "https://bl.rutube.ru/hls/abc/1080p_stream.m3u8",
    },
    {
        "format_id": "hls-720",
        "protocol": "m3u8_native",
        "height": 720,
        "width": 1280,
        "tbr": 2308,
        "vcodec": "avc1",
        "acodec": "mp4a",
        "url": "https://bl.rutube.ru/hls/abc/720p_stream.m3u8",
    },
    {
        "format_id": "hls-144",
        "protocol": "m3u8_native",
        "height": 144,
        "width": 256,
        "tbr": 286,
        "vcodec": "avc1",
        "acodec": "mp4a",
        "url": "https://bl.rutube.ru/hls/abc/144p_stream.m3u8",
    },
    {
        "format_id": "audio-only",
        "protocol": "m3u8_native",
        "vcodec": "none",
        "acodec": "mp4a",
        "tbr": 128,
        "url": "https://bl.rutube.ru/hls/abc/audio.m3u8",
    },
]


class TestVideoId:
    @pytest.mark.parametrize(
        "url",
        [
            "https://rutube.ru/video/e1c3757f9a3db09a224ac95c836cecbf/",
            "https://rutube.ru/play/embed/e1c3757f9a3db09a224ac95c836cecbf",
            "https://rutube.ru/play/embed/e1c3757f9a3db09a224ac95c836cecbf?p=abc",
            "https://rutube.ru/video/e1c3757f9a3db09a224ac95c836cecbf/?t=12",
        ],
    )
    def test_extracts_hash(self, url: str) -> None:
        assert video_id(url) == "e1c3757f9a3db09a224ac95c836cecbf"

    def test_uppercase_hash_is_lowered(self) -> None:
        """Ссылки иногда приходят с заглавными — сайт регистр не различает."""
        assert video_id("https://rutube.ru/video/E1C3757F9A3DB09A224AC95C836CECBF/") == (
            "e1c3757f9a3db09a224ac95c836cecbf"
        )

    def test_no_hash(self) -> None:
        assert video_id("https://rutube.ru/") == ""


class TestMatches:
    def test_video_page(self) -> None:
        src = RutubeSource()
        assert src.matches("https://rutube.ru/video/e1c3757f9a3db09a224ac95c836cecbf/")

    def test_embed(self) -> None:
        src = RutubeSource()
        assert src.matches("https://rutube.ru/play/embed/c158cbdc84872ecb1d45883ad87d7492")

    def test_foreign_site_rejected(self) -> None:
        src = RutubeSource()
        assert not src.matches("https://vkvideo.ru/video-1_2")
        assert not src.matches("https://youtube.com/watch?v=x")


class TestBuildQualities:
    def test_three_video_qualities_descending(self) -> None:
        qualities = RutubeSource().build_qualities({"formats": REAL_FORMATS})
        assert [q.height for q in qualities] == [1080, 720, 144]

    def test_audio_track_is_not_a_quality(self) -> None:
        """ERR: видео без звука появлялось, когда аудио-дорожка считалась качеством."""
        qualities = RutubeSource().build_qualities({"formats": REAL_FORMATS})
        assert all(q.height > 0 for q in qualities)
        assert len(qualities) == 3

    def test_bitrates_are_real(self) -> None:
        """ERR-067: завышенный битрейт = вечное 144p. Берём tbr как есть."""
        qualities = RutubeSource().build_qualities({"formats": REAL_FORMATS})
        by_h = {q.height: q.bitrate_kbps for q in qualities}
        assert by_h[1080] == 5884
        assert by_h[720] == 2308
        assert by_h[144] == 286

    def test_best_is_1080(self) -> None:
        qualities = RutubeSource().build_qualities({"formats": REAL_FORMATS})
        assert qualities[0].height == 1080
        assert qualities[0].width == 1920

    def test_fallback_to_mux_manifest(self) -> None:
        """Если разбивки нет, но есть мультиплексная дорожка — отдаём её."""
        info = {
            "formats": [],
            "hls_manifest_url": "https://bl.rutube.ru/mux.m3u8",
            "height": 720,
            "width": 1280,
            "tbr": 2500,
        }
        qualities = RutubeSource().build_qualities(info)
        assert len(qualities) == 1
        assert qualities[0].url.endswith("mux.m3u8")
        assert qualities[0].bitrate_kbps == 2500

    def test_nothing_at_all(self) -> None:
        assert RutubeSource().build_qualities({"formats": []}) == ()


class TestResolve:
    def test_resolve_builds_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_extract(_url: str, **_kw: object) -> dict:
            return {"title": "Тестовое видео", "duration": 225, "formats": REAL_FORMATS}

        monkeypatch.setattr("tvhost2.sources.base.extract_info", fake_extract)
        stream = RutubeSource().resolve("https://rutube.ru/video/e1c3757f9a3db09a224ac95c836cecbf/")
        assert stream.source == "Rutube"
        assert stream.title == "Тестовое видео"
        assert stream.kind is StreamKind.HLS
        assert stream.duration_s == 225
        assert len(stream.qualities) == 3

    def test_fallback_to_embed_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Страница не отдалась — пробуем embed с тем же hash."""
        calls: list[str] = []

        def fake_extract(url: str, **kw: object) -> dict:
            calls.append(url)
            if "play/embed" in url:
                return {"title": "Через embed", "formats": REAL_FORMATS}
            raise RuntimeError("страница пустая")

        monkeypatch.setattr("tvhost2.sources.base.extract_info", fake_extract)
        from tvhost2.sources import rutube as mod

        monkeypatch.setattr(mod, "extract_info", fake_extract)
        stream = RutubeSource().resolve("https://rutube.ru/video/e1c3757f9a3db09a224ac95c836cecbf/")
        assert stream.title == "Через embed"
        assert EMBED_URL.format(video_id="e1c3757f9a3db09a224ac95c836cecbf") in calls

    def test_short_link_does_not_retry_embed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ссылка без hash не должна уходить в embed — там нечего открывать."""

        def boom(url: str, **kw: object) -> dict:
            raise RuntimeError("нет данных")

        monkeypatch.setattr("tvhost2.sources.base.extract_info", boom)
        with pytest.raises(RuntimeError):
            RutubeSource().resolve("https://rutube.ru/video/")


class TestRegistration:
    def test_register_all_puts_rutube_in_registry(self) -> None:
        register_all()
        assert "Rutube" in registry.names()
        found = registry.find("https://rutube.ru/video/e1c3757f9a3db09a224ac95c836cecbf/")
        assert found is not None and found.name == "Rutube"

    def test_double_registration_is_safe(self) -> None:
        register_all()
        register_all()
        assert registry.names().count("Rutube") == 1

    def test_sources_tab_row(self) -> None:
        """Строка для вкладки «Источники» (запрос 9)."""
        register_all()
        rows = {row["name"]: row for row in registry.status_table()}
        assert rows["Rutube"] == {"name": "Rutube", "state": "ok", "note": "до 1080p, без входа"}


class TestRightsBlocked:
    """Реальный случай: «Супермен (2025)» — правообладатель ограничил показ.

    Rutube отдаёт `blocking_rule`, и это НЕ поломка источника: из России
    видео откроется. Поэтому источник остаётся «работает», а пользователь
    получает объяснение, а не «ошибка».
    """

    def test_blocking_rule_text_is_recognised(self) -> None:
        from tvhost2.core import RightsBlockedError
        from tvhost2.sources.base import _translate_download_error

        err = _translate_download_error(
            "ERROR: [rutube] abc: Unable to download JSON: "
            '{"detail": {"name": "blocking_rule_1", "type": "blocking_rule"}}'
        )
        assert isinstance(err, RightsBlockedError)
        assert err.state.value == "ok"

    def test_message_explains_it_is_not_our_fault(self) -> None:
        from tvhost2.core import RightsBlockedError

        text = RightsBlockedError("Rutube", "ограничено РФ").user_text()
        assert "правообладатель" in text
        assert "России" in text

    def test_404_from_rutube_is_rights_block(self) -> None:
        """yt-dlp на скрытом видео отдаёт 404, а не blocking_rule."""
        from tvhost2.core import RightsBlockedError
        from tvhost2.sources.base import _translate_download_error

        err = _translate_download_error(
            "ERROR: [rutube] abc: Unable to download options JSON: HTTP Error 404: Not Found"
        )
        assert isinstance(err, RightsBlockedError)

    def test_embed_is_not_tried_for_rights_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Embed не обойдёт запрет правообладателя — не тратим запрос."""
        from tvhost2.core import RightsBlockedError

        calls: list[str] = []

        def fake_extract(url: str, **_kw: object) -> dict:
            calls.append(url)
            raise RightsBlockedError("Rutube")

        monkeypatch.setattr("tvhost2.sources.base.extract_info", fake_extract)
        with pytest.raises(RightsBlockedError):
            RutubeSource().resolve("https://rutube.ru/video/e1c3757f9a3db09a224ac95c836cecbf/")
        assert len(calls) == 1, "повтор через embed не должен выполняться"
