"""Этап 4: YouTube.

Данные — реальный ответ yt-dlp на `watch?v=aqz-KE-bpKQ` (Big Buck Bunny 4K),
сокращённый до нужных полей. Главное, что проверяем: берём только H.264
(браузер игры не умеет VP9/AV1) и всегда находим аудио-дорожку.
"""

from __future__ import annotations

import pytest

from tvhost2.core import SourceState, StreamKind, registry
from tvhost2.sources import YouTubeSource, register_all
from tvhost2.sources.youtube import is_live_link, video_id

# Реальная нарезка YouTube: H.264 в HLS (itag 269…270).
H264_HLS = [
    ("269", 256, 144, 169.6),
    ("229", 426, 240, 310.6),
    ("230", 640, 360, 813.2),
    ("231", 854, 480, 1291.4),
    ("232", 1280, 720, 4553.9),
    ("270", 1920, 1080, 7417.8),  # реальный tbr
]


def h264_formats() -> list[dict]:
    return [
        {
            "format_id": itag,
            "protocol": "m3u8_native",
            "width": width,
            "height": height,
            "tbr": tbr,
            "vcodec": "avc1.4D401F",
            "acodec": "none",
            "url": f"https://manifest.googlevideo.com/itag/{itag}/index.m3u8",
        }
        for itag, width, height, tbr in H264_HLS
    ]


def vp9_formats() -> list[dict]:
    """VP9-дорожки: их браузер игры воспроизвести не сможет."""
    return [
        {
            "format_id": "248",
            "protocol": "m3u8_native",
            "width": 1920,
            "height": 1080,
            "tbr": 5022.0,
            "vcodec": "vp09.00.40",
            "acodec": "none",
            "url": "https://manifest.googlevideo.com/itag/248/index.m3u8",
        },
        {
            "format_id": "399",
            "protocol": "m3u8_native",
            "width": 1920,
            "height": 1080,
            "tbr": 1568.0,
            "vcodec": "av01.0.09M",
            "acodec": "none",
            "url": "https://manifest.googlevideo.com/itag/399/index.m3u8",
        },
    ]


def audio_hls(itag: str = "233") -> dict:
    return {
        "format_id": itag,
        "protocol": "m3u8_native",
        "vcodec": "none",
        "acodec": None,
        "ext": "mp4",
        "url": f"https://manifest.googlevideo.com/itag/{itag}/index.m3u8",
    }


def fake_info(payload: dict):
    def _inner(_url: str, **_kw: object) -> dict:
        return payload

    return _inner


class TestVideoId:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=xbeH0wcBwjw",
            "https://youtu.be/xbeH0wcBwjw",
            "https://www.youtube.com/shorts/xbeH0wcBwjw",
            "https://www.youtube.com/live/xbeH0wcBwjw",
            "https://m.youtube.com/watch?v=xbeH0wcBwjw&t=30",
        ],
    )
    def test_extracts(self, url: str) -> None:
        assert video_id(url) == "xbeH0wcBwjw"

    def test_no_id(self) -> None:
        assert video_id("https://www.youtube.com/") == ""


class TestLiveLink:
    def test_live_forms(self) -> None:
        assert is_live_link("https://www.youtube.com/live/abc")
        assert is_live_link("https://youtube.com/watch?v=x&is_live=1")

    def test_normal_video(self) -> None:
        assert not is_live_link("https://www.youtube.com/watch?v=xbeH0wcBwjw")


class TestBuildQualities:
    def test_only_h264(self) -> None:
        """VP9/AV1 отбрасываем: браузер игры их не проиграет."""
        qualities = YouTubeSource().build_qualities({"formats": h264_formats() + vp9_formats()})
        assert len(qualities) == 6
        assert all("avc1" not in q.url for q in qualities)  # url не содержит кодек
        assert [q.height for q in qualities] == [1080, 720, 480, 360, 240, 144]

    def test_vp9_alone_gives_nothing(self) -> None:
        """Если H.264 нет — честно пусто, а не «работает» с неиграбельным кодеком."""
        assert YouTubeSource().build_qualities({"formats": vp9_formats()}) == ()

    def test_labels(self) -> None:
        qualities = YouTubeSource().build_qualities({"formats": h264_formats()})
        assert qualities[0].label == "1080p"
        assert qualities[-1].label == "144p"

    def test_real_bitrate(self) -> None:
        """ERR-067: битрейт из tbr, а не выдуманный."""
        qualities = YouTubeSource().build_qualities({"formats": h264_formats()})
        assert qualities[0].bitrate_kbps == 7417  # округление вниз от 7417.8

    def test_highest_bitrate_wins_per_height(self) -> None:
        """На одну высоту бывает несколько H.264-дорожек — берём жирнее."""
        formats = [
            *h264_formats(),
            {
                "format_id": "270-extra",
                "protocol": "m3u8_native",
                "width": 1920,
                "height": 1080,
                "tbr": 3248.0,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "url": "https://manifest.googlevideo.com/itag/299/index.m3u8",
            },
        ]
        qualities = YouTubeSource().build_qualities({"formats": formats})
        best = next(q for q in qualities if q.height == 1080)
        assert best.bitrate_kbps == 7417


class TestAudioTrack:
    def test_itag_233_found(self) -> None:
        audio = YouTubeSource()._audio_hls({"formats": [*h264_formats(), audio_hls("233")]})
        assert audio is not None
        assert "/233/" in str(audio["url"])

    def test_itag_234_fallback(self) -> None:
        audio = YouTubeSource()._audio_hls({"formats": [audio_hls("234")]})
        assert audio is not None

    def test_direct_file_audio_is_not_used(self) -> None:
        """Аудио прямым mp4 (itag 140) в HLS-мастер не вставить — не берём."""
        direct = {
            "format_id": "140",
            "protocol": "https",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "url": "https://rr2---sn.googlevideo.com/videoplayback?id=140",
        }
        assert YouTubeSource()._audio_hls({"formats": [direct]}) is None


class TestResolve:
    def test_vod_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tvhost2.sources.youtube.extract_info",
            fake_info(
                {
                    "title": "Big Buck Bunny 60fps 4K",
                    "duration": 635,
                    "is_live": False,
                    "formats": [*h264_formats(), audio_hls()],
                }
            ),
        )
        stream = YouTubeSource().resolve("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
        assert stream.source == "YouTube"
        assert stream.kind is StreamKind.HLS_SPLIT
        assert stream.duration_s == 635
        assert stream.audio_url.endswith("233/index.m3u8")
        assert len(stream.qualities) == 6
        assert stream.qualities[0].label == "1080p"

    def test_live_is_refused_politely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Живой эфир пока не поддерживаем — говорим прямо, а не «ошибка»."""
        from tvhost2.core import SourceFailedError

        monkeypatch.setattr(
            "tvhost2.sources.youtube.extract_info",
            fake_info({"title": "LIVE", "is_live": True, "formats": h264_formats()}),
        )
        with pytest.raises(SourceFailedError) as info:
            YouTubeSource().resolve("https://www.youtube.com/watch?v=xbeH0wcBwjw")
        assert "прямой эфир" in info.value.why.lower()
        assert info.value.state is SourceState.PARTIAL

    def test_no_audio_is_explicit_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tvhost2.core import SourceFailedError

        monkeypatch.setattr(
            "tvhost2.sources.youtube.extract_info",
            fake_info({"title": "Без звука", "formats": h264_formats()}),
        )
        with pytest.raises(SourceFailedError) as info:
            YouTubeSource().resolve("https://www.youtube.com/watch?v=x")
        assert "аудио" in info.value.why.lower()

    def test_only_vp9_is_explicit_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tvhost2.core import SourceFailedError

        monkeypatch.setattr(
            "tvhost2.sources.youtube.extract_info",
            fake_info({"title": "VP9", "formats": [*vp9_formats(), audio_hls()]}),
        )
        with pytest.raises(SourceFailedError) as info:
            YouTubeSource().resolve("https://www.youtube.com/watch?v=x")
        assert "H.264" in info.value.why


class TestMatches:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=xbeH0wcBwjw",
            "https://youtu.be/xbeH0wcBwjw",
            "https://www.youtube.com/shorts/abc",
            "https://m.youtube.com/watch?v=abc",
        ],
    )
    def test_recognised(self, url: str) -> None:
        assert YouTubeSource().matches(url)

    def test_foreign_rejected(self) -> None:
        assert not YouTubeSource().matches("https://vkvideo.ru/video-1_2")
        assert not YouTubeSource().matches("https://rutube.ru/video/abc/")


class TestRegistry:
    def test_order(self) -> None:
        register_all()
        assert registry.names()[:3] == ["Rutube", "VK Видео", "YouTube"]

    def test_finds_youtube(self) -> None:
        register_all()
        found = registry.find("https://youtu.be/xbeH0wcBwjw")
        assert found is not None and found.name == "YouTube"
