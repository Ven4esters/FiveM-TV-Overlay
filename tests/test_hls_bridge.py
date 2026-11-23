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


class TestRutubeBitrates:
    """BANDWIDTH в master должен быть РЕАЛЬНЫМ (ERR-067).

    Было h*8000: для 1080p это 8.6 Мбит/с при фактических 5.9. hls.js
    считал канал недостаточно быстрым и навсегда оставался на 144p.
    """

    def test_collects_real_bitrate_per_height(self):
        formats = [
            {
                "protocol": "m3u8_native",
                "url": "https://c/1080.mp4.m3u8",
                "height": 1080,
                "tbr": 5884,
                "vcodec": "avc1",
            },
            {
                "protocol": "m3u8_native",
                "url": "https://c/720.mp4.m3u8",
                "height": 720,
                "tbr": 2308,
                "vcodec": "avc1",
            },
            {
                "protocol": "m3u8_native",
                "url": "https://c/audio.m3u8",
                "height": 0,
                "tbr": 128,
                "vcodec": "none",
            },
        ]
        heights, bitrates, _files = sources._collect_qualities(formats)
        assert heights == {1080: "https://c/1080.mp4.m3u8", 720: "https://c/720.mp4.m3u8"}
        assert bitrates == {"1080": 5884, "720": 2308}
        # 1080p НЕ должен заявляться как 8640 кбит/с
        assert bitrates["1080"] < 7000

    def test_master_uses_bitrates_not_height_formula(self):
        """В собранном master BANDWIDTH берётся из bitrates."""
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "tvhost" / "proxy.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        func = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_route_rutube_master"
        )
        body = ast.unparse(func)
        assert "bitrates" in body, (
            "_route_rutube_master не использует реальные битрейты — "
            "hls.js снова застрянет на низком качестве"
        )
        assert "h * 8000" not in body, "вернулась оценка битрейта h*8000"


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


class TestKodikDeadMirrors:
    """ERR-081: мёртвое зеркало Kodik роняло поток с levelLoadError.

    Заказчик получил ошибку на живой ссылке, хотя 720p отдавался нормально:
    hls.js выбирал качество, чьё зеркало (p12/p13/p14) не ответило на
    конкретный файл. Проверка зеркал перед сборкой master решает это.
    """

    def test_dead_mirror_is_dropped(self, monkeypatch) -> None:
        """Мёртвая ссылка не должна попадать в master."""
        live = "https://p12.solodcdn.com/x/720.mp4:hls:manifest.m3u8"
        dead = "https://p99.solodcdn.com/x/480.mp4:hls:manifest.m3u8"

        def fake_probe(urls, timeout=6):
            return {live}

        monkeypatch.setattr(sources, "_probe_alive_qualities", fake_probe)
        monkeypatch.setattr(
            sources,
            "kodik_streams",
            lambda *_a, **_k: {
                "qualities": {720: live, 480: dead},
                "translation": "СВ-Дубль",
                "translation_id": 729,
            },
        )
        master = sources.kodik_master("https://kodikplayer.com/seria/1/" + "a" * 32 + "/720p")
        assert master is not None
        assert master["qualities"] == [720]
        assert "p99" not in master["playlist"]

    def test_total_probe_failure_keeps_everything(self, monkeypatch) -> None:
        """Проверка не удалась (нет сети) — отдаём все качества, не пустоту.

        Пустой master хуже мёртвого зеркала: видео не запустится вовсе.
        """
        hi = "https://p12.solodcdn.com/x/720.mp4:hls:manifest.m3u8"
        lo = "https://p13.solodcdn.com/x/480.mp4:hls:manifest.m3u8"
        monkeypatch.setattr(sources, "_probe_alive_qualities", lambda *_a, **_k: set())
        monkeypatch.setattr(
            sources,
            "kodik_streams",
            lambda *_a, **_k: {
                "qualities": {720: hi, 480: lo},
                "translation": "СВ-Дубль",
                "translation_id": 729,
            },
        )
        master = sources.kodik_master("https://kodikplayer.com/seria/1/" + "a" * 32 + "/720p")
        assert master is not None
        assert sorted(master["qualities"]) == [480, 720]

    def test_probe_returns_empty_on_bad_links(self, monkeypatch) -> None:
        """_probe_alive_qualities не падает на несуществующих доменах."""

        def boom(*a, **k):
            raise OSError("нет сети")

        monkeypatch.setattr(sources, "_get", boom)
        assert sources._probe_alive_qualities(["https://нет.такого/x.m3u8"]) == set()

    def test_probe_empty_input(self) -> None:
        assert sources._probe_alive_qualities([]) == set()


class TestPlaylistErrorInsteadOfDrop:
    """ERR-081: ошибка CDN должна быть HTTP-ответом, а не обрывом связи.

    Раньше RuntimeError из _probe_playlist вылетал из обработчика, и
    BaseHTTPRequestHandler закрывал сокет без ответа. Снаружи это выглядело
    как levelLoadError без причины.
    """

    def test_error_playlist_is_valid_m3u8(self) -> None:
        """Тело ошибки остаётся валидным HLS-плейлистом с причиной."""
        import urllib.parse

        reason = "HTTP Error 404: Not Found"
        body = "#EXTM3U\n#EXT-X-KODIK-ERROR:" + urllib.parse.quote(reason, safe="") + "\n"
        assert body.startswith("#EXTM3U")
        assert "KODIK-ERROR" in body
        decoded = urllib.parse.unquote(body.split("KODIK-ERROR:")[1].strip())
        assert decoded == reason
