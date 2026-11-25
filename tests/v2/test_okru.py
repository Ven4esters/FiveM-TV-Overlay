"""OK.ru: разбор ссылок и отбор качеств.

Главная ловушка этого источника: yt-dlp отдаёт `vcodec=None` у HLS-дорожек,
и общая функция `pick_hls_qualities()` считает такие дорожки «без видео» —
на выходе пустой список, источник выглядит сломанным. Здесь проверяется,
что свой сборщик этого не делает.
"""

from __future__ import annotations

import pytest

from tvhost2.core.models import SourceState, StreamKind
from tvhost2.sources.okru import OkRuSource, is_okru, video_id

H264_TBR = [
    # (height, width, tbr) — значения с живой ссылки OK.ru 24.11.2026:
    #   id=hls-5641 height=1080 width=1920 res=1920x1080
    # vcodec и acodec у этих дорожек не заполнены вообще (None).
    (1080, 1920, 5641.0),
    (720, 1280, 2684.0),
    (480, 852, 1277.0),
    (360, 640, 780.0),
    (240, 426, 390.0),
    (144, 256, 201.0),
]


def hls_formats() -> list[dict]:
    """HLS-дорожки OK.ru: vcodec и acodec не заполнены вообще."""
    return [
        {
            "format_id": f"hls-{h}",
            "protocol": "m3u8_native",
            "height": h,
            "width": w,
            "tbr": tbr,
            "vcodec": None,
            "acodec": None,
            "url": f"https://vd673.okcdn.ru/hls/{h}.m3u8",
        }
        for h, w, tbr in H264_TBR
    ]


def vp9_direct() -> list[dict]:
    """Прямые файлы VP9 — браузер игры их не воспроизводит."""
    return [
        {
            "format_id": f"webm-{i}",
            "protocol": "https",
            "height": h,
            "width": w,
            "tbr": tbr,
            "vcodec": "vp9",
            "acodec": "none",
            "url": f"https://vd673.okcdn.ru/webm/{h}.webm",
        }
        for i, (h, w, tbr) in enumerate(H264_TBR)
    ]


def opus_audio() -> list[dict]:
    """Аудио Opus — CEF его не декодирует, брать нельзя."""
    return [
        {
            "format_id": "webm-7",
            "protocol": "https",
            "height": None,
            "vcodec": "none",
            "acodec": "opus",
            "tbr": 131.0,
            "url": "https://vd673.okcdn.ru/audio/opus.webm",
        }
    ]


def fake_info(payload: dict):
    def _inner(_url: str, **_kw: object) -> dict:
        return payload

    return _inner


class TestVideoId:
    @pytest.mark.parametrize(
        "url",
        [
            "https://ok.ru/video/7475662490142",
            "https://ok.ru/video/9701219043910?fromTime=0",
            "http://ok.ru/web_video/123456789",
            "https://odnoklassniki.ru/video/555",
        ],
    )
    def test_extracts(self, url: str) -> None:
        assert video_id(url), f"id не найден в {url}"

    def test_profile_is_not_video(self) -> None:
        """Ссылка на профиль или группу — не видео, источник не должен браться."""
        assert not is_okru("https://ok.ru/profile/123")
        assert not is_okru("https://ok.ru/group/abc")
        assert is_okru("https://ok.ru/video/123")


class TestQualities:
    def test_hls_with_empty_vcodec_is_not_dropped(self) -> None:
        """Ключевая проверка: vcodec=None не значит «без видео».

        Общая pick_hls_qualities() вернула бы здесь пустой кортеж — и OK.ru
        выглядел бы сломанным на всех видео подряд.
        """
        source = OkRuSource()
        qualities = source.build_qualities({"formats": hls_formats()})
        assert len(qualities) == 6, f"потерялись дорожки: {len(qualities)}"
        heights = [q.height for q in qualities]
        assert heights[0] == 1080, "первым должно быть лучшее качество"
        assert heights[-1] == 144
        # Проверка размеров кадра: 1080p — это 1920x1080, а не наоборот.
        best = qualities[0]
        assert (best.width, best.height) == (1920, 1080), f"кадр {best.width}x{best.height}"

    def test_shared_function_would_drop_everything(self) -> None:
        """Доказательство, почему сборщик свой, а не общий.

        Если этот тест начнёт падать, значит общую функцию починили и
        локальный сборщик можно убрать. Пока она возвращает пусто — оставляем.
        """
        from tvhost2.sources.base import pick_hls_qualities

        shared = pick_hls_qualities(hls_formats())
        own = OkRuSource().build_qualities({"formats": hls_formats()})
        assert len(shared) == 0, "общая функция научилась — можно упростить"
        assert len(own) == 6

    def test_vp9_and_opus_are_ignored(self) -> None:
        """VP9 и Opus в CEF не играют: берём только HLS."""
        source = OkRuSource()
        qualities = source.build_qualities(
            {"formats": [*hls_formats(), *vp9_direct(), *opus_audio()]}
        )
        assert all("m3u8" in q.url for q in qualities), "в качества попал прямой файл"
        assert len(qualities) == 6

    def test_best_bitrate_wins_per_height(self) -> None:
        """На одну высоту — дорожка с большим битрейтом (ERR-067)."""
        formats = [
            *hls_formats(),
            {
                "protocol": "m3u8_native",
                "height": 720,
                "width": 1280,
                "tbr": 5000.0,
                "vcodec": None,
                "url": "https://x/best-720.m3u8",
            },
        ]
        qualities = OkRuSource().build_qualities({"formats": formats})
        best_720 = next(q for q in qualities if q.height == 720)
        assert best_720.bitrate_kbps == 5000


class TestResolve:
    def test_normal_video(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tvhost2.sources.okru.extract_info",
            fake_info(
                {
                    "title": "ОКнутые люди",
                    "duration": 2927,
                    "formats": [*hls_formats(), *vp9_direct()],
                }
            ),
        )
        stream = OkRuSource().resolve("https://ok.ru/video/7475662490142")
        assert stream.source == "OK.ru"
        assert stream.title == "ОКнутые люди"
        assert stream.duration_s == 2927
        assert stream.kind is StreamKind.HLS
        assert len(stream.qualities) == 6
        assert not stream.is_live

    def test_stream_carries_user_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Поток обязан нести UA: без него okcdn отвечает 400.

        Проверено живьём 25.11.2026: тот же адрес `manifest_url` с заголовком
        `User-Agent` отдаёт `200` и плейлист, без заголовка — `400` с телом
        `10`. Сессия при этом создавалась успешно, поэтому сломанный источник
        выглядел рабочим до первого сегмента.
        """
        monkeypatch.setattr(
            "tvhost2.sources.okru.extract_info",
            fake_info(
                {
                    "title": "ОКнутые люди",
                    "duration": 2927,
                    "formats": [*hls_formats(), *vp9_direct()],
                }
            ),
        )
        stream = OkRuSource().resolve("https://ok.ru/video/7475662490142")
        assert stream.headers, "заголовки не переданы — okcdn ответит 400"
        assert "User-Agent" in stream.headers
        assert stream.headers["User-Agent"].startswith("Mozilla/5.0")
        assert stream.headers.get("Referer", "").startswith("https://ok.ru")

    def test_live_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Прямые эфиры OK.ru не поддерживаются — говорим прямо."""
        from tvhost2.core.errors import SourceFailedError

        monkeypatch.setattr(
            "tvhost2.sources.okru.extract_info",
            fake_info({"title": "Эфир", "is_live": True, "formats": hls_formats()}),
        )
        with pytest.raises(SourceFailedError) as info:
            OkRuSource().resolve("https://ok.ru/video/1")
        assert "эфир" in info.value.why.lower()
        assert info.value.state is SourceState.PARTIAL

    def test_only_vp9_gives_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tvhost2.core.errors import SourceFailedError

        monkeypatch.setattr(
            "tvhost2.sources.okru.extract_info",
            fake_info({"title": "Только VP9", "formats": vp9_direct()}),
        )
        with pytest.raises(SourceFailedError) as info:
            OkRuSource().resolve("https://ok.ru/video/1")
        assert "HLS" in info.value.why

    def test_retry_helps_when_first_attempt_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Вторая попытка спасает: yt-dlp у OK.ru падает через раз.

        Проверено живьём: одна и та же ссылка дала «ошибка, ошибка, успех,
        успех» в четырёх запросах. Поэтому попыток три и по разным адресам.
        """
        calls: list[str] = []

        def flaky(url: str, **_kw: object) -> dict:
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError("Unable to extract player")
            return {"title": "Со второго раза", "formats": hls_formats()}

        monkeypatch.setattr("tvhost2.sources.okru.extract_info", flaky)
        stream = OkRuSource().resolve("https://ok.ru/video/7475662490142")
        assert stream.title == "Со второго раза"
        assert len(calls) == 2, f"попыток {len(calls)}, ждали 2"


class TestRegistry:
    def test_registered(self) -> None:
        from tvhost2.core import registry
        from tvhost2.sources import register_all

        register_all()
        assert "OK.ru" in registry.names()

    def test_finds_okru_links(self) -> None:
        from tvhost2.core import registry
        from tvhost2.sources import register_all

        register_all()
        source = registry.find("https://ok.ru/video/7475662490142")
        assert source is not None
        assert source.name == "OK.ru"
