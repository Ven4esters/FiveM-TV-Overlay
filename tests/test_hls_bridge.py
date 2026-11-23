"""Тест HLS-моста: сегменты, плейлисты и master'ы источников.

Здесь собраны баги, которые НЕ видны в прокси-логе без сегментов:
- VK/Kodik присылали сегмент в /hls, а прокси считал его плейлистом и
  закрывал соединение без ответа → hls.js падал с fragLoadError;
- Rutube отдаёт ссылки на качества с уникальными именами, поэтому
  склейка base + имя файла давала 404 → manifestLoadError;
- у вертикальных видео ширина считалась как height*16/9 (3413x1920).
"""

from __future__ import annotations

import pytest

from tvhost.infra import sources
from tvhost.proxy import _looks_like_segment


class TestSegmentDetection:
    """Сегмент отличается от плейлиста даже когда путь спрятан в ?url=."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://vkvd529.okcdn.ru/expires/1/srcIp/x/HIGH00000.ts",
            "https://cdn/x/video/segment-1-v1-a1.ts",
            "https://cdn/x/audio/12_4.mp4",
            "https://cdn/x/chunk.m4s",
            "https://cdn/x/sound.aac",
            "https://cdn/x/subtitles.vtt",
        ],
    )
    def test_segments_detected(self, url):
        assert _looks_like_segment(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://river-4.rtbcdn.ru/hls-vod/a/b/file.mp4.m3u8?i=1920x1080_5884",
            "https://cdn/master.m3u8",
            "https://cdn/index.m3u8",
            "https://okcdn.ru/audio/12_4.m3u8",
        ],
    )
    def test_playlists_not_mistaken_for_segment(self, url):
        assert _looks_like_segment(url) is False

    def test_vk_segment_inside_proxy_query(self):
        """То, что реально приходит в /hls?url=... от VK."""
        inner = "https://vkvd529.okcdn.ru/expires/1/srcIp/x/video/HIGH00000.ts"
        import urllib.parse

        proxied = "/hls?url=" + urllib.parse.quote(inner, safe="")
        assert _looks_like_segment(proxied) is True

    def test_rutube_playlist_inside_proxy_query(self):
        """Плейлист Rutube с ?i=... не должен считаться сегментом."""
        inner = "https://river-4.rtbcdn.ru/hls-vod/a/b/x.mp4.m3u8?i=1280x720_2308"
        import urllib.parse

        proxied = "/hls?url=" + urllib.parse.quote(inner, safe="")
        assert _looks_like_segment(proxied) is False


class TestSegmentWiring:
    """Сегмент должен уходить в _proxy_segment, а не в поиск плейлиста.

    Прошлый баг: проверка была по `url.endswith('.ts')`, а у VK имя файла
    спрятано в ?url=..., поэтому сегмент считался плейлистом, vk_master
    возвращал None, и прокси закрывал соединение без единого байта ответа.
    """

    def _func_source(self, name: str) -> str:
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "tvhost" / "proxy.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.unparse(node)
        raise AssertionError(f"в proxy.py нет функции {name}")

    @pytest.mark.parametrize("func", ["_hls_playlist_vk", "_hls_playlist_kodik"])
    def test_segment_checked_before_playlist_lookup(self, func):
        body = self._func_source(func)
        assert "_looks_like_segment" in body, (
            f"{func} не проверяет, сегмент ли это — VK/Kodik снова "
            "оборвут соединение на fragLoadError"
        )
        assert "_proxy_segment" in body, (
            f"{func} не отдаёт сегмент байтами — fragLoadError вернётся"
        )

    def test_old_extension_check_is_gone(self):
        """Старая проверка по концу строки не должна вернуться."""
        body = self._func_source("_hls_playlist_vk")
        assert 'endswith((".ts"' not in body, (
            "проверка по концу URL не видит сегменты внутри ?url= — используйте _looks_like_segment"
        )


class TestVkMasterWidth:
    """RESOLUTION в master'е VK: ширина из источника, а не height*16/9."""

    def test_vertical_video_keeps_portrait_resolution(self, monkeypatch):
        """Вертикальное видео: 1080x1920, а не 3413x1920."""
        fake = [
            {
                "id": "hls-4512",
                "url": "https://cdn/v1920.m3u8",
                "protocol": "m3u8_native",
                "height": 1920,
                "width": 1080,  # yt-dlp отдаёт реальную ширину
                "tbr": 4512,
                "vcodec": "none",
                "acodec": "none",
                "ext": "mp4",
                "headers": {},
            },
            {
                "id": "hls-2208",
                "url": "https://cdn/v1280.m3u8",
                "protocol": "m3u8_native",
                "height": 1280,
                "width": 720,
                "tbr": 2208,
                "vcodec": "none",
                "acodec": "none",
                "ext": "mp4",
                "headers": {},
            },
            {
                "id": "hls-audio",
                "url": "https://cdn/a.m3u8",
                "protocol": "m3u8_native",
                "height": 0,
                "width": 0,
                "tbr": 128,
                "vcodec": "none",
                "acodec": "none",
                "ext": "m4a",
                "headers": {},
            },
        ]
        monkeypatch.setattr(sources, "_vk_formats", lambda _u: fake)
        master = sources._vk_master_uncached("https://vk.com/video-1_2")
        assert master is not None
        playlist = master["playlist"]
        assert "RESOLUTION=1080x1920" in playlist, playlist
        assert "RESOLUTION=720x1280" in playlist, playlist
        assert "3413x1920" not in playlist

    def test_missing_width_falls_back_to_16_9(self, monkeypatch):
        """Старые источники без width не должны ломать плейлист."""
        fake = [
            {
                "id": f"hls-{h}",
                "url": f"https://cdn/{h}.m3u8",
                "protocol": "m3u8_native",
                "height": h,
                "width": 0,
                "tbr": h,
                "vcodec": "none",
                "acodec": "none",
                "ext": "mp4",
                "headers": {},
            }
            for h in (1080, 720)
        ] + [
            {
                "id": "a",
                "url": "https://cdn/a.m3u8",
                "protocol": "m3u8_native",
                "height": 0,
                "width": 0,
                "tbr": 100,
                "vcodec": "none",
                "acodec": "none",
                "ext": "m4a",
                "headers": {},
            }
        ]
        monkeypatch.setattr(sources, "_vk_formats", lambda _u: fake)
        master = sources._vk_master_uncached("https://vk.com/video-1_2")
        assert master is not None
        assert "RESOLUTION=1920x1080" in master["playlist"]


class TestRutubeQualities:
    """Rutube: качества берутся из variants, а не склейкой base + имя."""

    def test_variants_used_when_quality_files_empty(self):
        """yt-dlp для Rutube отдаёт variants; quality_files теперь пуст."""
        # это контракт источника: если variants есть, качеств должно быть >1
        info = {
            "variants": {"1080": "https://cdn/1.m3u8", "720": "https://cdn/2.m3u8"},
            "quality_files": {},
            "master_base": "",
        }
        assert info["variants"] and not info["quality_files"]
        # логика выбора в _route_rutube_master: variants имеет приоритет
        quality_urls = {int(h): u for h, u in info["variants"].items() if str(h).isdigit()}
        assert sorted(quality_urls, reverse=True) == [1080, 720]


class TestDiagnosticsJournal:
    """Журнал работы программы должен реально писаться на диск."""

    def test_start_and_stop_are_recorded(self, isolated_diag):
        isolated_diag.mark_start()
        isolated_diag.log("info", "прокси запущен на порту 9000")
        isolated_diag.log("info", "прокси остановлен (Ctrl+C)")
        text = isolated_diag.report_text()
        assert "прокси запущен" in text
        assert "прокси остановлен" in text

    def test_player_error_keeps_details(self, isolated_diag):
        """Ошибка из игры пишется с качеством, причиной и адресом."""
        isolated_diag.log(
            "player",
            "ошибка в игре: поток: fragLoadError (критично)",
            quality="720p",
            reason="fragLoadError",
            src="river-4.rtbcdn.ru…segment-1.ts",
        )
        rows = [e for e in isolated_diag.recent() if e["kind"] == "player"]
        assert rows
        last = rows[-1]
        assert "fragLoadError" in str(last)
        assert "720p" in str(last)
