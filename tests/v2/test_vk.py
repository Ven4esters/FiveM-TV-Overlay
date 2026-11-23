"""Этап 3: VK Видео.

Данные — реальный ответ yt-dlp на ссылку заказчика
`vkvideo.ru/video-220020068_456260705` («Очень странные дела»),
сокращённый до нужных полей.
"""

from __future__ import annotations

import pytest

from tvhost2.core import StreamKind, registry
from tvhost2.sources import VkSource, nominal_from_url, register_all
from tvhost2.sources.vk import nominal_for_frame

# Настоящая нарезка VK: «паспортное» качество → реальный кадр.
REAL_FRAMES = [(256, 128), (426, 212), (640, 320), (852, 426), (1280, 640), (1920, 960)]
REAL_TBR = [213.955, 295.684, 434.336, 589.036, 987.456, 1654.93]


def fake_info(payload: dict):
    """Подмена extract_info: возвращает заданный ответ, не ходит в сеть."""

    def _inner(_url: str, **_kw: object) -> dict:
        return payload

    return _inner


def formats_multiplex() -> list[dict]:
    """Мультиплексные дорожки `hls-NNN`: видео и звук вместе."""
    return [
        {
            "format_id": f"hls-{int(tbr)}",
            "protocol": "m3u8_native",
            "width": width,
            "height": height,
            "tbr": tbr,
            "vcodec": None,
            "acodec": None,
            "url": f"https://vk6-10.vkuser.net/expires/1/src/ms/{height}/index.m3u8",
        }
        for (width, height), tbr in zip(REAL_FRAMES, REAL_TBR, strict=True)
    ]


def formats_split() -> list[dict]:
    """Раздельные дорожки: видео без звука + аудио отдельно."""
    video = [
        {
            "format_id": f"hls_fmp4-{int(tbr)}",
            "protocol": "m3u8_native",
            "width": width,
            "height": height,
            "tbr": tbr,
            "vcodec": "avc1.4D40",
            "acodec": "none",
            "url": f"https://vk6-10.vkuser.net/ms/{height}/index.m3u8",
        }
        for (width, height), tbr in zip(REAL_FRAMES, REAL_TBR, strict=True)
    ]
    audio = [
        {
            "format_id": "hls_fmp4-12_0-Audio",
            "protocol": "m3u8_native",
            "vcodec": "none",
            "tbr": 128.0,
            "url": "https://vk6-10.vkuser.net/audio-low/index.m3u8",
        },
        {
            "format_id": "hls_fmp4-12_4-Audio",
            "protocol": "m3u8_native",
            "vcodec": "none",
            "tbr": 265.457,
            "url": "https://vk6-10.vkuser.net/audio-high/index.m3u8",
        },
    ]
    return video + audio


class TestNominalQuality:
    @pytest.mark.parametrize(
        ("width", "height", "expected"),
        [
            (256, 128, 144),
            (426, 212, 240),
            (640, 320, 360),
            (852, 426, 480),
            (1280, 640, 720),
            (1920, 960, 1080),
        ],
    )
    def test_frame_maps_to_vk_label(self, width: int, height: int, expected: int) -> None:
        """ERR-072: VK зовёт 1080p то, что реально 1920x960."""
        assert nominal_for_frame(width, height) == expected

    def test_unknown_frame_is_not_guessed(self) -> None:
        """Незнакомый кадр — честно 0, а не выдуманное «1080p»."""
        assert nominal_for_frame(1998, 1080) == 0

    def test_nominal_from_url(self) -> None:
        assert nominal_from_url("https://x/url1080.mp4") == 1080
        assert nominal_from_url("https://x/no-id.mp4") == 0


class TestBuildQualities:
    def test_multiplex_preferred(self) -> None:
        """Если есть дорожки со звуком — берём их, склеивать не надо."""
        qualities = VkSource().build_qualities({"formats": formats_multiplex() + formats_split()})
        assert len(qualities) == 6
        # мультиплексные дорожки не содержат 'fmp4' в пути
        assert all("fmp4" not in q.url for q in qualities)

    def test_labels_are_vk_names(self) -> None:
        qualities = VkSource().build_qualities({"formats": formats_multiplex()})
        assert [q.label for q in qualities] == [
            "1080p",
            "720p",
            "480p",
            "360p",
            "240p",
            "144p",
        ]

    def test_real_frame_is_kept(self) -> None:
        """Подпись «1080p», но кадр остаётся настоящим 1920x960."""
        best = VkSource().build_qualities({"formats": formats_multiplex()})[0]
        assert best.label == "1080p"
        assert (best.width, best.height) == (1920, 960)
        assert best.label_height == 1080

    def test_letterbox_detected(self) -> None:
        """2:1 кадр — это фильм с полосами, а не квадрат."""
        best = VkSource().build_qualities({"formats": formats_multiplex()})[0]
        assert best.is_letterboxed

    def test_split_formats_used_when_no_multiplex(self) -> None:
        qualities = VkSource().build_qualities({"formats": formats_split()})
        assert len(qualities) == 6
        assert all("fmp4" in q.url or "/ms/" in q.url for q in qualities)

    def test_audio_tracks_are_not_qualities(self) -> None:
        qualities = VkSource().build_qualities({"formats": formats_split()})
        assert all(q.height > 0 for q in qualities)

    def test_real_bitrates(self) -> None:
        """ERR-067: битрейты — из tbr, а не выдуманные."""
        qualities = VkSource().build_qualities({"formats": formats_multiplex()})
        assert qualities[0].bitrate_kbps == 1654
        assert qualities[-1].bitrate_kbps == 213


class TestVerticalVideo:
    """ERR-062: у вертикального видео большая сторона — это ШИРИНА."""

    @staticmethod
    def vertical_formats() -> list[dict]:
        # Реальный клип VK: кадр 1080x1920, качества по ширине (1080/720/…)
        return [
            {
                "format_id": f"hls-{br}",
                "protocol": "m3u8_native",
                "width": w,
                "height": h,
                "tbr": br,
                "url": f"https://vk6-10.vkuser.net/ms/{w}/index.m3u8",
            }
            for w, h, br in [
                (1080, 1920, 4512.28),
                (720, 1280, 2208.03),
                (480, 852, 1036.73),
                (360, 640, 656.49),
                (240, 426, 332.79),
                (144, 256, 178.52),
            ]
        ]

    def test_portrait_flag(self) -> None:
        qualities = VkSource().build_qualities({"formats": self.vertical_formats()})
        assert all(q.is_portrait for q in qualities)

    def test_label_says_vertical_with_both_sides(self) -> None:
        """У вертикального видео подпись показывает кадр целиком.

        «1080p» для клипа 1080x1920 путает: он выглядит как обычный 1080p.
        """
        qualities = VkSource().build_qualities({"formats": self.vertical_formats()})
        assert qualities[0].label == "1080x1920 (верт.)"
        assert qualities[-1].label == "144x256 (верт.)"

    def test_width_is_the_small_side(self) -> None:
        qualities = VkSource().build_qualities({"formats": self.vertical_formats()})
        best = qualities[0]
        assert best.width == 1080
        assert best.height == 1920


class TestAudio:
    def test_audio_url_taken_from_best_track(self) -> None:
        """Звук — самая полная дорожка (265 kbps AAC), а не 128 kbps."""
        source = VkSource()
        info = {"formats": formats_split()}
        assert "audio-high" in source.pick_audio(info)

    def test_no_audio_needed_for_multiplex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Мультиплекс уже содержит звук — отдельная дорожка не нужна."""
        monkeypatch.setattr(
            "tvhost2.sources.vk.extract_info",
            fake_info({"title": "Фильм", "formats": formats_multiplex()}),
        )
        stream = VkSource().resolve("https://vkvideo.ru/video-1_2")
        assert stream.audio_url == ""

    def test_split_gets_audio_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tvhost2.sources.vk.extract_info",
            fake_info({"title": "Фильм", "formats": formats_split()}),
        )
        stream = VkSource().resolve("https://vkvideo.ru/video-1_2")
        assert "audio-high" in stream.audio_url


class TestMatches:
    @pytest.mark.parametrize(
        "url",
        [
            "https://vkvideo.ru/video-220020068_456260705",
            "https://vk.com/video-22822305_456239018",
            "https://vk.ru/video-1_2",
            "https://m.vkvideo.ru/video-1_2",
        ],
    )
    def test_recognised(self, url: str) -> None:
        assert VkSource().matches(url)

    def test_foreign_rejected(self) -> None:
        assert not VkSource().matches("https://rutube.ru/video/abc/")
        assert not VkSource().matches("https://youtube.com/watch?v=x")


class TestResolve:
    def test_full_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tvhost2.sources.vk.extract_info",
            fake_info(
                {
                    "title": "Очень странные дела. 1 сезон. 1 серия",
                    "duration": 2908,
                    "is_live": False,
                    "formats": formats_multiplex(),
                }
            ),
        )
        stream = VkSource().resolve("https://vkvideo.ru/video-220020068_456260705")
        assert stream.source == "VK Видео"
        assert stream.title.startswith("Очень странные дела")
        assert stream.kind is StreamKind.HLS
        assert stream.duration_s == 2908
        assert len(stream.qualities) == 6
        assert stream.best is not None and stream.best.label == "1080p"

    def test_empty_formats_raises_human_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tvhost2.core import SourceFailedError

        monkeypatch.setattr("tvhost2.sources.vk.extract_info", fake_info({"formats": []}))
        with pytest.raises(SourceFailedError) as info:
            VkSource().resolve("https://vkvideo.ru/video-1_2")
        assert "не отдал ни одного потока" in info.value.why


class TestRegistry:
    def test_registered_after_rutube(self) -> None:
        """Порядок: сначала специфичные сайты, потом общие."""
        register_all()
        assert registry.names()[:2] == ["Rutube", "VK Видео"]

    def test_matches_its_link(self) -> None:
        register_all()
        found = registry.find("https://vkvideo.ru/video-220020068_456260705")
        assert found is not None and found.name == "VK Видео"
