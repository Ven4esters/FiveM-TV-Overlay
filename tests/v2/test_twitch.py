"""Этап 5: Twitch — живые каналы, записи и клипы.

Данные — реальные ответы yt-dlp от 23.11.2026:
  живой канал hasanabi, запись 2928653119 (WORLDS 2026 Finals),
  клип riotgames.

Главное отличие Twitch от YouTube: здесь мультиплекс везде — видео и звук
в одной дорожке. Ни склейки, ни аудиогруппы не нужно.
"""

from __future__ import annotations

import pytest

from tvhost2.core import StreamKind, registry
from tvhost2.sources import TwitchSource, channel_name, link_kind, register_all
from tvhost2.sources.twitch import offline_error, pick_clip_formats

# Живой канал: мультиплекс с видео и звуком в одной дорожке.
LIVE_FORMATS = [
    {
        "format_id": "audio_only",
        "protocol": "m3u8_native",
        "vcodec": "none",
        "acodec": "mp4a.40.2",
        "tbr": 160.0,
        "url": "https://video-weaver/audio.m3u8",
    },
    {
        "format_id": "160p",
        "protocol": "m3u8_native",
        "width": 284,
        "height": 160,
        "vcodec": "avc1.4D401F",
        "acodec": "mp4a.40.2",
        "tbr": 230.0,
        "url": "https://video-weaver/160p/index.m3u8",
    },
    {
        "format_id": "360p",
        "protocol": "m3u8_native",
        "width": 640,
        "height": 360,
        "vcodec": "avc1.4D401F",
        "acodec": "mp4a.40.2",
        "tbr": 630.0,
        "url": "https://video-weaver/360p/index.m3u8",
    },
    {
        "format_id": "480p",
        "protocol": "m3u8_native",
        "width": 852,
        "height": 480,
        "vcodec": "avc1.4D401F",
        "acodec": "mp4a.40.2",
        "tbr": 1428.0,
        "url": "https://video-weaver/480p/index.m3u8",
    },
    {
        "format_id": "720p60",
        "protocol": "m3u8_native",
        "width": 1280,
        "height": 720,
        "vcodec": "avc1.4D401F",
        "acodec": "mp4a.40.2",
        "tbr": 3423.0,
        "url": "https://video-weaver/720p60/index.m3u8",
    },
    {
        "format_id": "1080p60__source_",
        "protocol": "m3u8_native",
        "width": 1920,
        "height": 1080,
        "vcodec": "avc1.64002A",
        "acodec": "mp4a.40.2",
        "tbr": 8928.0,
        "url": "https://video-weaver/1080p60/index.m3u8",
    },
]

# Запись: на 720p приходят две дорожки — обычная и 60 fps.
VOD_FORMATS = [
    {
        "format_id": "sb1",
        "protocol": "mhtml",
        "width": 160,
        "height": 90,
        "vcodec": "none",
        "acodec": "none",
        "url": "https://thumb/sb1",
    },
    {
        "format_id": "sb0",
        "protocol": "mhtml",
        "width": 220,
        "height": 124,
        "vcodec": "none",
        "acodec": "none",
        "url": "https://thumb/sb0",
    },
    {
        "format_id": "Audio_Only",
        "protocol": "m3u8_native",
        "vcodec": "none",
        "acodec": "mp4a.40.2",
        "tbr": 145.0,
        "url": "https://weaver/audio.m3u8",
    },
    {
        "format_id": "720p",
        "protocol": "m3u8_native",
        "width": 1280,
        "height": 720,
        "vcodec": "avc1.4D401F",
        "acodec": "mp4a.40.2",
        "tbr": 2245.0,
        "url": "https://weaver/720p/index.m3u8",
    },
    {
        "format_id": "720p60",
        "protocol": "m3u8_native",
        "width": 1280,
        "height": 720,
        "vcodec": "avc1.4D401F",
        "acodec": "mp4a.40.2",
        "tbr": 3072.0,
        "url": "https://weaver/720p60/index.m3u8",
    },
    {
        "format_id": "1080p60",
        "protocol": "m3u8_native",
        "width": 1920,
        "height": 1080,
        "vcodec": "avc1.4D401F",
        "acodec": "mp4a.40.2",
        "tbr": 7338.0,
        "url": "https://weaver/1080p60/index.m3u8",
    },
]

# Клип: прямые mp4 БЕЗ кодеков в метаданных (особенность Twitch).
CLIP_FORMATS = [
    {
        "format_id": "360",
        "protocol": "https",
        "height": 360,
        "tbr": None,
        "vcodec": None,
        "acodec": None,
        "ext": "mp4",
        "url": "https://d1ndex63qxojbr.cloudfront.net/nauth/x/360.mp4",
    },
    {
        "format_id": "480",
        "protocol": "https",
        "height": 480,
        "tbr": None,
        "vcodec": None,
        "acodec": None,
        "ext": "mp4",
        "url": "https://d1ndex63qxojbr.cloudfront.net/nauth/x/480.mp4",
    },
    {
        "format_id": "720",
        "protocol": "https",
        "height": 720,
        "tbr": None,
        "vcodec": None,
        "acodec": None,
        "ext": "mp4",
        "url": "https://d1ndex63qxojbr.cloudfront.net/nauth/x/720.mp4",
    },
]


def fake_info(payload: dict):
    def _inner(_url: str, **_kw: object) -> dict:
        return payload

    return _inner


class TestLinkKind:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.twitch.tv/xqc",
            "https://twitch.tv/hasanabi",
            "https://m.twitch.tv/kaicenat",
        ],
    )
    def test_channel(self, url: str) -> None:
        assert link_kind(url) == "channel"

    def test_vod(self) -> None:
        assert link_kind("https://www.twitch.tv/videos/2928653119") == "vod"

    def test_clip(self) -> None:
        assert link_kind("https://www.twitch.tv/riotgames/clip/ObservantKawaii") == "clip"

    def test_unknown(self) -> None:
        assert link_kind("https://example.com/x") == "unknown"


class TestChannelName:
    def test_extracts(self) -> None:
        assert channel_name("https://www.twitch.tv/hasanabi") == "hasanabi"
        assert channel_name("https://twitch.tv/xqc/") == "xqc"

    def test_query_is_ignored(self) -> None:
        assert channel_name("https://www.twitch.tv/hasanabi?sr=a") == "hasanabi"

    def test_empty_for_vod(self) -> None:
        assert channel_name("https://www.twitch.tv/videos/123") == ""


class TestMatches:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.twitch.tv/hasanabi",
            "https://www.twitch.tv/videos/2928653119",
            "https://clips.twitch.tv/SomeClip",
        ],
    )
    def test_recognised(self, url: str) -> None:
        assert TwitchSource().matches(url)

    def test_directory_is_not_video(self) -> None:
        """Каталог каналов — не видео, не должен уходить в разбор."""
        assert not TwitchSource().matches("https://www.twitch.tv/directory/game/Dota%202")

    def test_foreign_rejected(self) -> None:
        assert not TwitchSource().matches("https://youtube.com/watch?v=x")


class TestLiveQualities:
    def test_all_heights(self) -> None:
        qualities = TwitchSource().build_qualities({"formats": LIVE_FORMATS})
        assert [q.height for q in qualities] == [1080, 720, 480, 360, 160]

    def test_audio_only_is_not_a_quality(self) -> None:
        """Аудио-дорожка не должна стать «качеством 0p»."""
        qualities = TwitchSource().build_qualities({"formats": LIVE_FORMATS})
        assert all(q.height > 0 for q in qualities)

    def test_real_bitrates(self) -> None:
        qualities = TwitchSource().build_qualities({"formats": LIVE_FORMATS})
        by_height = {q.height: q.bitrate_kbps for q in qualities}
        assert by_height[1080] == 8928
        assert by_height[160] == 230


class TestVodQualities:
    def test_thumbnails_are_skipped(self) -> None:
        """sb0/sb1 — превью-раскадровки, не видео."""
        qualities = TwitchSource().build_qualities({"formats": VOD_FORMATS})
        assert all(q.height >= 720 for q in qualities)

    def test_same_height_takes_fat_track(self) -> None:
        """На 720p приходят 720p и 720p60 — берём ту, что жирнее."""
        qualities = TwitchSource().build_qualities({"formats": VOD_FORMATS})
        best_720 = next(q for q in qualities if q.height == 720)
        assert best_720.bitrate_kbps == 3072

    def test_heights(self) -> None:
        qualities = TwitchSource().build_qualities({"formats": VOD_FORMATS})
        assert [q.height for q in qualities] == [1080, 720]


class TestClipQualities:
    def test_clip_works_without_codecs(self) -> None:
        """Клипы Twitch не имеют vcodec/acodec — но это видео."""
        qualities = pick_clip_formats(CLIP_FORMATS)
        assert [q.height for q in qualities] == [720, 480, 360]

    def test_clip_bitrate_estimated(self) -> None:
        """tbr у клипов пустой — подставляем оценку, а не ноль (ERR-067)."""
        qualities = pick_clip_formats(CLIP_FORMATS)
        assert all(q.bitrate_kbps > 0 for q in qualities)

    def test_build_qualities_falls_back_to_clip(self) -> None:
        qualities = TwitchSource().build_qualities({"formats": CLIP_FORMATS})
        assert len(qualities) == 3


class TestResolve:
    def test_live_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tvhost2.sources.twitch.extract_info",
            fake_info({"title": "HasanAbi (live)", "is_live": True, "formats": LIVE_FORMATS}),
        )
        stream = TwitchSource().resolve("https://www.twitch.tv/hasanabi")
        assert stream.source == "Twitch"
        assert stream.is_live
        assert stream.kind is StreamKind.HLS
        assert stream.best is not None and stream.best.label == "1080p"

    def test_vod_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tvhost2.sources.twitch.extract_info",
            fake_info(
                {
                    "title": "GEN vs BLG | WORLDS 2026 | Finals",
                    "duration": 33077,
                    "is_live": False,
                    "formats": VOD_FORMATS,
                }
            ),
        )
        stream = TwitchSource().resolve("https://www.twitch.tv/videos/2928653119")
        assert not stream.is_live
        assert stream.duration_s == 33077
        assert len(stream.qualities) == 2

    def test_clip_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tvhost2.sources.twitch.extract_info",
            fake_info({"title": "bet ninja can do that", "formats": CLIP_FORMATS}),
        )
        stream = TwitchSource().resolve("https://www.twitch.tv/riotgames/clip/ObservantKawaii")
        assert stream.title == "bet ninja can do that"
        assert len(stream.qualities) == 3

    def test_empty_raises_human_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tvhost2.core import SourceFailedError

        monkeypatch.setattr("tvhost2.sources.twitch.extract_info", fake_info({"formats": []}))
        with pytest.raises(SourceFailedError) as info:
            TwitchSource().resolve("https://www.twitch.tv/videos/1")
        assert "не отдал ни одного качества" in info.value.why


class TestOfflineChannel:
    def test_offline_explains_recordings(self) -> None:
        """Канал не в эфире — это норма, а не поломка."""
        err = offline_error("https://www.twitch.tv/somechannel")
        assert "не в эфире" in err.why
        assert "somechannel" in err.why
        assert "videos" in err.fix

    def test_offline_without_name(self) -> None:
        err = offline_error("https://www.twitch.tv/videos/1")
        assert "не в эфире" in err.why


class TestRegistry:
    def test_order(self) -> None:
        """Twitch идёт после Kodik: ссылки не пересекаются, порядок стабилен."""
        register_all()
        assert registry.names()[:5] == ["Rutube", "VK Видео", "YouTube", "Kodik", "Twitch"]

    def test_finds_twitch(self) -> None:
        register_all()
        found = registry.find("https://www.twitch.tv/videos/2928653119")
        assert found is not None and found.name == "Twitch"
