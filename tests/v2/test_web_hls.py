"""HLS-мост v2: переписывание плейлистов и защита от молчаливого обрыва.

Главный урок v1 (ERR-081): обрыв соединения hls.js видит как
`levelLoadError` без причины и не может переключиться на другое
качество. Поэтому здесь проверяется, что ошибка ВСЕГДА превращается в
понятный ответ.
"""

from __future__ import annotations

import socket

from tvhost2.core.models import Quality, Stream, StreamKind
from tvhost2.web import hls


def _free_port() -> int:
    """Свободный порт: тесты не должны падать из-за занятого 9001."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


KODIK_MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=2765000,RESOLUTION=1280x720
https://p12.solodcdn.com/s/m/abc/720.mp4:hls:manifest.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1228000,RESOLUTION=852x480
https://p14.solodcdn.com/s/m/abc/480.mp4:hls:manifest.m3u8"""

KODIK_VARIANT = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-PLAYLIST-TYPE:VOD
#EXTINF:6.0,
00000.ts
#EXTINF:6.0,
00001.ts
#EXT-X-ENDLIST"""


class TestPlaylistDetection:
    def test_extm3u_is_playlist(self) -> None:
        assert hls.is_playlist(b"#EXTM3U\n#EXT-X-VERSION:3\n") is True

    def test_leading_whitespace_ok(self) -> None:
        assert hls.is_playlist(b"\n\n  #EXTM3U\n") is True

    def test_mpeg_ts_is_not_playlist(self) -> None:
        """Сегмент начинается с 0x47 — это не плейлист."""
        assert hls.is_playlist(b"\x47\x40\x11\x10\x00\x00\xb0") is False

    def test_empty_is_not_playlist(self) -> None:
        assert hls.is_playlist(b"") is False

    def test_our_error_is_detected(self) -> None:
        assert hls.is_error_playlist(hls.error_playlist("нет")) is True

    def test_normal_playlist_is_not_error(self) -> None:
        assert hls.is_error_playlist(KODIK_VARIANT.encode()) is False


class TestProxify:
    def test_absolute_becomes_proxied(self) -> None:
        got = hls.proxify("https://p12.solodcdn.com/x.ts", "https://src/")
        assert got.startswith("/hls?url=")
        assert "p12.solodcdn.com" in got

    def test_url_is_encoded(self) -> None:
        """Внутри url= не должно быть сырых ? и & — иначе параметры порвутся."""
        got = hls.proxify("https://cdn/x.m3u8?token=a&b=c", "https://src/")
        inside = got.split("url=", 1)[1]
        assert "?" not in inside and "&" not in inside

    def test_relative_resolved_against_base(self) -> None:
        got = hls.proxify("00001.ts", "https://p12.solodcdn.com/s/m/abc/480.mp4:hls:manifest.m3u8")
        assert "p12.solodcdn.com" in got and "00001.ts" in got

    def test_already_proxied_not_wrapped_twice(self) -> None:
        """Двойная обёртка ломает разбор: второй уровень не понимает url=."""
        once = hls.proxify("https://cdn/x.ts", "https://src/")
        twice = hls.proxify(once, "https://src/")
        assert twice == once

    def test_empty_stays_empty(self) -> None:
        assert hls.proxify("", "https://src/") == ""


class TestRewrite:
    def test_variant_segments_rewritten(self) -> None:
        out = hls.rewrite(
            KODIK_VARIANT, "https://p14.solodcdn.com/s/m/abc/480.mp4:hls:manifest.m3u8"
        )
        assert "#EXTINF:6.0," in out
        assert "/hls?url=" in out
        assert "\n00000.ts" not in out

    def test_master_links_rewritten(self) -> None:
        out = hls.rewrite(KODIK_MASTER, "https://kodikplayer.com/seria/1/abc/720p")
        assert out.count("/hls?url=") == 2

    def test_tags_preserved(self) -> None:
        out = hls.rewrite(KODIK_VARIANT, "https://cdn/x.m3u8")
        assert "#EXT-X-TARGETDURATION:6" in out
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in out
        assert "#EXT-X-ENDLIST" in out

    def test_audio_group_uri_rewritten(self) -> None:
        """У YouTube звук лежит в URI="" аудиогруппы — без переписывания
        hls.js пойдёт напрямую и получит 403."""
        playlist = (
            '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Звук",'
            'DEFAULT=YES,URI="https://rr1.googlevideo.com/a.m3u8"\n'
        )
        out = hls.rewrite(playlist, "https://rr1.googlevideo.com/v.m3u8")
        assert 'URI="/hls?url=' in out
        assert "TYPE=AUDIO" in out and 'NAME="Звук"' in out

    def test_key_uri_rewritten(self) -> None:
        playlist = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://cdn/key.bin"\n'
        out = hls.rewrite(playlist, "https://cdn/x.m3u8")
        assert 'URI="/hls?url=' in out
        assert "METHOD=AES-128" in out


class TestBuildMaster:
    def test_sorted_best_first(self) -> None:
        m = hls.build_master(
            [
                (360, 640, 691, "https://cdn/360.m3u8"),
                (1080, 1920, 5884, "https://cdn/1080.m3u8"),
                (720, 1280, 2765, "https://cdn/720.m3u8"),
            ]
        )
        order = [line for line in m.splitlines() if line.startswith("https://")]
        assert order[0].endswith("1080.m3u8")
        assert order[-1].endswith("360.m3u8")

    def test_resolution_from_real_width(self) -> None:
        """Вертикальное видео 1080x1920 не должно стать 1920x1080."""
        m = hls.build_master([(1920, 1080, 4000, "https://cdn/v.m3u8")])
        assert "RESOLUTION=1080x1920" in m

    def test_bandwidth_is_real_bitrate(self) -> None:
        """Не выдуманный: завышение ломает выбор ABR (ERR-067)."""
        m = hls.build_master([(720, 1280, 2765, "https://cdn/720.m3u8")])
        assert "BANDWIDTH=2765000" in m

    def test_audio_group_added(self) -> None:
        m = hls.build_master(
            [(720, 1280, 2000, "https://cdn/v.m3u8")], audio_url="https://cdn/a.m3u8"
        )
        assert "TYPE=AUDIO" in m and 'GROUP-ID="audio"' in m
        assert 'AUDIO="audio"' in m

    def test_no_audio_group_by_default(self) -> None:
        m = hls.build_master([(720, 1280, 2000, "https://cdn/v.m3u8")])
        assert "EXT-X-MEDIA" not in m

    def test_empty_qualities_still_valid(self) -> None:
        assert hls.build_master([]).startswith("#EXTM3U")

    def test_unknown_height_omits_resolution(self) -> None:
        """Высота неизвестна (0x0) — `RESOLUTION` не пишем вовсе.

        `RESOLUTION=0x0` невалиден и роняет разбор у hls.js. Такое качество
        появляется у tvigle, когда мастер не скачался (честный фолбэк).
        """
        m = hls.build_master([(0, 0, 0, "https://cdn/m.m3u8")])
        assert "RESOLUTION" not in m
        assert "BANDWIDTH=1000" in m  # не 0 — ноль тоже невалиден
        assert "https://cdn/m.m3u8" in m


class TestErrorPlaylist:
    def test_is_valid_m3u8(self) -> None:
        body = hls.error_playlist("HTTP 404: Not Found")
        assert body.startswith(b"#EXTM3U")

    def test_reason_roundtrip(self) -> None:
        reason = "зеркало p14 не ответило"
        assert hls.decode_error(hls.error_playlist(reason)) == reason

    def test_long_reason_truncated(self) -> None:
        body = hls.error_playlist("x" * 500)
        assert len(body) < 400

    def test_decode_on_normal_playlist(self) -> None:
        assert hls.decode_error(KODIK_VARIANT.encode()) == ""


class TestMasterUsesBridgeLinks:
    """Мастер сессии не должен отдавать браузеру сырые адреса источника.

    Это была настоящая поломка: `/stream/<sid>` собирал мастер с прямыми
    ссылками CDN и полагался на «мост перепишет». Переписывать было некому —
    мастер уходит браузеру как есть, поэтому ссылки на чужие домены (и тем
    более file:// у склейки) просто не открывались. Внешне это выглядело как
    «видео не играет», без единой ошибки в журнале.
    """

    def test_stream_rewrites_quality_links(self) -> None:
        from tvhost2.web import server

        session = server.STATE.sessions.create("u")
        session.stream = Stream(
            source="Kodik",
            title="Тест",
            kind=StreamKind.HLS,
            qualities=(
                Quality(
                    height=720,
                    width=1280,
                    bitrate_kbps=2900,
                    url="https://p12.solodcdn.com/x/720.m3u8",
                ),
            ),
        )
        try:
            import threading
            import urllib.request

            port = _free_port()
            httpd = server.serve("127.0.0.1", port)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen(  # noqa: S310
                    f"http://127.0.0.1:{port}/stream/{session.sid}", timeout=10
                ) as response:
                    master = response.read().decode()
            finally:
                httpd.shutdown()
        finally:
            server.STATE.sessions.delete(session.sid)

        # Ссылка обязана быть обёрнута в мост. Внутри url= адрес источника
        # присутствует (закодированный) — это нормально и нужно мосту.
        links = [line for line in master.splitlines() if line and not line.startswith("#")]
        assert links, f"в мастере нет ссылок:\n{master}"
        for link in links:
            assert link.startswith("/hls?url="), f"ссылка мимо моста: {link}"

    def test_stream_rewrites_local_mux_links(self) -> None:
        """Склейка ffmpeg отдаётся как file:// — мастер обязан обернуть и её."""
        from tvhost2.web import server

        session = server.STATE.sessions.create("u")
        session.stream = Stream(
            source="YouTube",
            title="Склейка",
            kind=StreamKind.HLS,
            qualities=(
                Quality(
                    height=720,
                    width=1280,
                    bitrate_kbps=2900,
                    url="file:///tmp/mux/index.m3u8",
                ),
            ),
        )
        try:
            import threading
            import urllib.request

            port = _free_port()
            httpd = server.serve("127.0.0.1", port)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen(  # noqa: S310
                    f"http://127.0.0.1:{port}/stream/{session.sid}", timeout=10
                ) as response:
                    master = response.read().decode()
            finally:
                httpd.shutdown()
        finally:
            server.STATE.sessions.delete(session.sid)

        assert "file://" not in master, f"file:// утёк в браузер:\n{master}"
        assert "/hls?url=" in master
