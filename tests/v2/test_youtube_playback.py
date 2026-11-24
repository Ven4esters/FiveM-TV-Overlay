"""Живая проверка YouTube-сценария в настоящем Chromium.

Бот-чек YouTube не пускает автоматические запросы, поэтому сам YouTube здесь
не используется. Вместо него — поток с той же структурой, что отдаёт YouTube:
отдельный плейлист видео и отдельный плейлист звука, склеенные мастером с
аудиогруппой `EXT-X-MEDIA`.

Зачем целый браузер: главный вопрос не «отдал ли прокси плейлист», а «играет
ли звук». Проверено, что hls.js декодирует сырой AAC в аудиогруппе, но это
утверждение живёт в комментарии, пока его не подтвердит реальный движок.
Здесь подтверждает: Chromium, hls.js, замер по `video.buffered`.

Тест помечен `browser`: он поднимает браузер и требует ffmpeg для подготовки
потока. Запуск:

    pytest tests/v2/test_youtube_playback.py -m browser -v
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

playwright = pytest.importorskip("playwright.sync_api")

from tvhost2.core import ffmpeg_bin  # noqa: E402
from tvhost2.core.models import Quality, Stream, StreamKind  # noqa: E402
from tvhost2.web import player, server  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ffmpeg() -> str:
    found = ffmpeg_bin.find_ffmpeg()
    if not found:
        pytest.skip("нет ffmpeg — нечем подготовить тестовый поток")
    return found


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Два качества видео без звука + отдельная аудиодорожка.

    Ровно структура YouTube: видео и звук живут в разных плейлистах.
    """
    folder = tmp_path_factory.mktemp("media")
    ff = _ffmpeg()
    jobs = [
        ("v720", ["-vf", "scale=1280:720"]),
        ("v360", ["-vf", "scale=640:360"]),
    ]
    for name, extra in jobs:
        subprocess.run(  # noqa: S603
            [
                ff, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
                *extra, "-t", "24", "-c:v", "libx264", "-preset", "ultrafast",
                "-g", "25", "-an", "-f", "hls", "-hls_time", "4",
                "-hls_playlist_type", "vod",
                "-hls_segment_filename", str(folder / f"{name}_%03d.ts"),
                str(folder / f"{name}.m3u8"),
            ],
            check=True,
            capture_output=True,
        )  # fmt: skip
    subprocess.run(  # noqa: S603
        [
            ff, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=24",
            "-c:a", "aac", "-b:a", "128k", "-f", "hls", "-hls_time", "4",
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(folder / "a_%03d.ts"),
            str(folder / "audio.m3u8"),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    # Субтитры: обычный WEBVTT, как отдаёт YouTube после конвертации.
    (folder / "sub_ru.vtt").write_text(
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:04.000\nПервая строка субтитров\n\n"
        "00:00:05.000 --> 00:00:09.000\nВторая строка субтитров\n",
        encoding="utf-8",
    )
    return folder


@pytest.fixture(scope="module")
def proxy(media: Path):  # noqa: ANN201
    """Прокси v2, отдающий наш локальный «YouTube» через настоящий мост."""
    port = _free_port()
    httpd = server.serve("127.0.0.1", port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    session = server.STATE.sessions.create("https://youtube.com/watch?v=test")
    session.title = "Проверка звука"
    session.source_name = "YouTube"
    session.stream = Stream(
        source="YouTube",
        title="Проверка звука",
        kind=StreamKind.HLS,
        qualities=(
            Quality(
                height=720,
                width=1280,
                bitrate_kbps=2900,
                url=f"{base}/hls?url=file://{media}/v720.m3u8",
            ),
            Quality(
                height=360,
                width=640,
                bitrate_kbps=1200,
                url=f"{base}/hls?url=file://{media}/v360.m3u8",
            ),
        ),
        audio_url=f"{base}/hls?url=file://{media}/audio.m3u8",
        duration_s=24,
        is_live=False,
        subtitles=({"lang": "ru", "title": "Русские", "url": f"file://{media}/sub_ru.vtt"},),
    )
    yield base, session
    httpd.shutdown()


def test_master_has_audio_group(proxy) -> None:  # noqa: ANN001
    """Мастер собран с аудиогруппой: без неё звука не будет вообще."""
    import urllib.request

    base, session = proxy
    with urllib.request.urlopen(f"{base}/stream/{session.sid}", timeout=10) as response:
        master = response.read().decode()

    assert "#EXT-X-MEDIA:TYPE=AUDIO" in master
    assert 'GROUP-ID="audio"' in master
    assert master.count("#EXT-X-STREAM-INF") == 2


def test_chromium_plays_video_and_audio(proxy) -> None:  # noqa: ANN001
    """Главная проверка: в Chromium идёт и картинка, и звук.

    Смотрим не на «нет ошибок», а на буферы: `video.buffered` для картинки,
    `audioTracks`/`webkitAudioDecodedByteCount` для звука. Нулевой аудиобуфер
    при живом видеобуфере — ровно та поломка, ради которой этот тест и нужен.
    """
    base, session = proxy
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_page()
        errors: list[str] = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.goto(f"{base}/player/{session.sid}", wait_until="load")
        # Кнопка лежит поверх видео и в headless может быть не кликабельна
        # (нулевой размер кадра до старта) — жмём напрямую через DOM.
        page.evaluate("() => document.getElementById('bigplay').click()")

        played = None
        for _ in range(60):
            time.sleep(0.5)
            played = page.evaluate(
                """() => {
                    const v = document.getElementById('v');
                    // video.audioBuffered в этой сборке Chromium нет (только
                    // в старых), поэтому звук считаем по декодированным байтам:
                    // webkitAudioDecodedByteCount > 0 значит «звук реально
                    // раскодирован», а не «дорожка объявлена в мастере».
                    let buffered = 0;
                    for (let i = 0; i < v.buffered.length; i++) {
                        buffered += v.buffered.end(i) - v.buffered.start(i);
                    }
                    return {
                        time: v.currentTime,
                        buffered: buffered,
                        decodedAudio: v.webkitAudioDecodedByteCount || 0,
                        decodedVideo: v.webkitVideoDecodedByteCount || 0,
                        paused: v.paused,
                        error: v.error ? v.error.code : 0
                    };
                }"""
            )
            if played["decodedAudio"] > 0 and played["decodedVideo"] > 0:
                break

        browser.close()

    assert played is not None
    assert played["error"] == 0, f"плеер сообщил ошибку {played['error']}: {errors}"
    assert played["decodedVideo"] > 0, f"видео не декодируется: {played}"
    assert played["decodedAudio"] > 0, f"звук не декодируется: {played}"
    # Порог низкий намеренно: в headless без экрана время идёт рывками, но
    # ненулевое время при декодированных байтах означает «видео реально
    # поехало», а не «плейлист разобран».
    assert played["time"] > 0, f"воспроизведение не пошло: {played}"
    assert played["buffered"] > 1, f"буфер пуст: {played}"


def test_player_page_knows_live_flag() -> None:
    """Страница эфира получает LIVE=true, обычного видео — false."""
    from tvhost2.web.session import Session

    session = Session(sid="abc", url="https://youtube.com/live/x", title="Эфир")
    session.stream = Stream(
        source="YouTube", title="Эфир", kind=StreamKind.HLS, is_live=True, duration_s=0
    )
    page = player.render(session, "http://127.0.0.1:9001", "/static/hls.min.js")
    assert "var LIVE = true;" in page

    session.stream = Stream(source="YouTube", title="Обычное", kind=StreamKind.HLS, is_live=False)
    page = player.render(session, "http://127.0.0.1:9001", "/static/hls.min.js")
    assert "var LIVE = false;" in page


def test_live_session_state_has_flag() -> None:
    """Пульт узнаёт про эфир из /state, иначе рисует нерабочий таймер."""
    from tvhost2.web.session import Session

    session = Session(sid="abc", url="https://youtube.com/live/x")
    session.stream = Stream(source="YouTube", title="", kind=StreamKind.HLS, is_live=True)
    assert session.state()["isLive"] is True

    session.stream = Stream(source="YouTube", title="", kind=StreamKind.HLS, is_live=False)
    assert session.state()["isLive"] is False


def test_ffmpeg_available_or_skip() -> None:
    """Если ffmpeg нет — тесты потока пропускаются, а не падают."""
    if shutil.which("ffmpeg") is None and ffmpeg_bin.find_ffmpeg() is None:
        pytest.skip("ffmpeg не найден")


def test_live_stream_plays_in_chromium(tmp_path: Path) -> None:
    """Прямой эфир: плейлист без ENDLIST, звук отдельно — играет в Chromium.

    У эфира нет конца плейлиста и нет длительности. Раньше YouTube-модуль
    такие ссылки отклонял целиком. Теперь отдаёт, но плеер обязан вести себя
    иначе: перемотка не работает, длительность не рисуется, hls.js держится
    живого края.
    """
    ff = _ffmpeg()
    subprocess.run(  # noqa: S603
        [
            ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc2=size=1280x720:rate=25", "-t", "20",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-g", "25", "-an",
            "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event",
            "-hls_segment_filename", str(tmp_path / "live_%03d.ts"),
            str(tmp_path / "live.m3u8"),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    subprocess.run(  # noqa: S603
        [
            ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=20", "-c:a", "aac", "-b:a", "128k",
            "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event",
            "-hls_segment_filename", str(tmp_path / "la_%03d.ts"),
            str(tmp_path / "live_audio.m3u8"),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    # У настоящего эфира ENDLIST нет — убираем, иначе это уже запись.
    for name in ("live.m3u8", "live_audio.m3u8"):
        path = tmp_path / name
        path.write_text(
            "\n".join(line for line in path.read_text().splitlines() if "ENDLIST" not in line)
            + "\n"
        )

    port = _free_port()
    httpd = server.serve("127.0.0.1", port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    session = server.STATE.sessions.create("https://youtube.com/live/test")
    session.title = "Эфир"
    session.source_name = "YouTube"
    session.stream = Stream(
        source="YouTube",
        title="Эфир",
        kind=StreamKind.HLS,
        qualities=(
            Quality(
                height=720,
                width=1280,
                bitrate_kbps=2900,
                url=f"{base}/hls?url=file://{tmp_path}/live.m3u8",
            ),
        ),
        audio_url=f"{base}/hls?url=file://{tmp_path}/live_audio.m3u8",
        duration_s=0,
        is_live=True,
    )
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
            page = browser.new_page()
            page.goto(f"{base}/player/{session.sid}", wait_until="load")
            page.evaluate("() => document.getElementById('bigplay').click()")

            played = None
            for _ in range(60):
                time.sleep(0.5)
                played = page.evaluate(
                    """() => {
                        const v = document.getElementById('v');
                        return {
                            time: v.currentTime,
                            decodedAudio: v.webkitAudioDecodedByteCount || 0,
                            decodedVideo: v.webkitVideoDecodedByteCount || 0,
                            error: v.error ? v.error.code : 0
                        };
                    }"""
                )
                if played["decodedAudio"] > 0 and played["decodedVideo"] > 0:
                    break
            browser.close()
    finally:
        httpd.shutdown()

    assert played is not None
    assert played["error"] == 0, played
    assert played["decodedVideo"] > 0, f"эфир: видео не идёт — {played}"
    assert played["decodedAudio"] > 0, f"эфир: звук не идёт — {played}"


def test_mux_glues_video_and_audio(media: Path) -> None:
    """Аварийный путь: видео-плейлист + отдельный аудиофайл → один поток.

    Так спасаются видео, у которых YouTube не отдал HLS-аудио: звук лежит
    прямым файлом, в мастер его не вставить. ffmpeg склеивает, причём видео
    копируется без перекодирования — проверяем кодек в готовом сегменте.
    """
    from tvhost2.core import mux

    if not mux.available():
        pytest.skip("нет ffmpeg — аварийный путь недоступен")

    ff = _ffmpeg()
    audio_file = media / "audio140.m4a"
    subprocess.run(  # noqa: S603
        [
            ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=24", "-c:a", "aac", "-b:a", "128k",
            str(audio_file),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip

    result = mux.muxed_for(f"file://{media}/v720.m3u8", f"file://{audio_file}")
    assert result is not None
    assert mux.wait_ready(result, timeout=60), "ffmpeg не выдал ни одного сегмента"

    segments = sorted(result.folder.glob("seg_*.ts"))
    assert segments, "нет сегментов"

    # В сегменте должны быть ОБА потока: видео и звук. Проверяем ffprobe-ом.
    probe = subprocess.run(  # noqa: S603
        [ff, "-hide_banner", "-i", str(segments[0])],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    output = probe.stderr
    assert "Video: h264" in output, f"видео не скопировано: {output[:300]}"
    assert "Audio: aac" in output, f"звук не вклеен: {output[:300]}"
    assert "1280x720" in output, "видео потеряло разрешение"


def test_mux_reuses_cache(media: Path) -> None:
    """Повторный запрос той же пары не пересобирает поток заново."""
    from tvhost2.core import mux

    if not mux.available():
        pytest.skip("нет ffmpeg")

    first = mux.muxed_for(f"file://{media}/v360.m3u8", f"file://{media}/audio.m3u8")
    assert first is not None and mux.wait_ready(first, timeout=60)
    stamp = first.playlist.stat().st_mtime

    second = mux.muxed_for(f"file://{media}/v360.m3u8", f"file://{media}/audio.m3u8")
    assert second is not None
    assert second.playlist == first.playlist
    assert second.playlist.stat().st_mtime == stamp, "поток пересобран вместо кеша"


def test_mux_without_ffmpeg_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет ffmpeg — не падаем, а честно сообщаем, что склейка недоступна."""
    from tvhost2.core import mux

    monkeypatch.setattr(mux.ffmpeg_bin, "find_ffmpeg", lambda: None)
    assert mux.muxed_for("file:///a.m3u8", "file:///b.m4a") is None


def test_subtitles_load_and_switch(proxy) -> None:  # noqa: ANN001
    """Субтитры: дорожки доезжают до браузера и включаются командой.

    До этой правки кнопка «Субтитры» на пульте не была подключена ни к чему:
    нажималась и молчала. Проверяем весь путь — тег <track> в странице,
    загрузку VTT через мост, режим `showing` после команды и наличие
    активной реплики (то есть текст реально виден, а не просто дорожка есть).
    """
    base, session = proxy
    vtt = session.stream.subtitles[0]["url"] if session.stream.subtitles else ""
    if not vtt:
        pytest.skip("в фикстуре нет субтитров")
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
            page = browser.new_page()
            page.goto(f"{base}/player/{session.sid}", wait_until="load")
            page.evaluate("() => document.getElementById('bigplay').click()")
            time.sleep(2.5)

            count = page.evaluate("() => document.getElementById('v').textTracks.length")
            assert count == 1, f"дорожек в браузере {count}, ждали 1"

            import urllib.request

            urllib.request.urlopen(  # noqa: S310 — свой локальный адрес
                f"{base}/control/{session.sid}?cmd=subs&value=1", data=b""
            )
            time.sleep(2.5)

            # Ставим позицию внутрь первой реплики (1–4 с): проверяем не
            # «дорожка включена», а «текст виден в этот момент».
            page.evaluate("() => { document.getElementById('v').currentTime = 2; }")
            time.sleep(1.5)
            state = page.evaluate(
                """() => {
                    const v = document.getElementById('v');
                    const tr = v.textTracks[0];
                    return {
                        mode: tr.mode,
                        cues: tr.activeCues ? tr.activeCues.length : 0,
                        time: v.currentTime,
                        total: tr.cues ? tr.cues.length : 0
                    };
                }"""
            )
            browser.close()
    finally:
        pass

    assert state["mode"] == "showing", f"дорожка не включилась: {state}"
    assert state["cues"] >= 1, f"реплика не показана: {state}"


def test_subtitle_route_serves_vtt(proxy) -> None:  # noqa: ANN001
    """Мост отдаёт субтитры с типом text/vtt — иначе браузер их не примет."""
    import urllib.request

    base, session = proxy
    if not session.stream.subtitles:
        pytest.skip("в фикстуре нет субтитров")
    with urllib.request.urlopen(f"{base}/subs/{session.sid}?i=0", timeout=10) as response:  # noqa: S310
        ctype = response.headers.get("Content-Type", "")
        body = response.read().decode()
    assert "text/vtt" in ctype, ctype
    assert body.startswith("WEBVTT")


def test_session_state_lists_subtitles(proxy) -> None:  # noqa: ANN001
    """Пульт узнаёт названия дорожек из /state."""
    from tvhost2.web.session import Session

    session = Session(sid="abc", url="u")
    session.stream = Stream(
        source="YouTube",
        title="",
        kind=StreamKind.HLS,
        subtitles=(
            {"lang": "ru", "title": "Русские", "url": "file:///a"},
            {"lang": "en", "title": "English", "url": "file:///b"},
        ),
    )
    assert session.state()["subtitles"] == ["Русские", "English"]

    session.stream = Stream(source="YouTube", title="", kind=StreamKind.HLS)
    assert session.state()["subtitles"] == []


def test_mux_plays_in_chromium(tmp_path: Path) -> None:
    """Склейка играет в браузере: и картинка, и звук.

    Отдельный тест, потому что здесь ломалось дважды: сначала качество
    указывало на исходный плейлист вместо склейки, потом мастер отдавал
    file:// прямо в браузер. Оба раза поток молчал без ошибок — ловится
    только реальным воспроизведением.
    """
    from tvhost2.core import mux

    if not mux.available():
        pytest.skip("нет ffmpeg")

    ff = _ffmpeg()
    folder = tmp_path / "mux"
    folder.mkdir()
    subprocess.run(  # noqa: S603
        [
            ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc2=size=640x360:rate=25", "-t", "12",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-an",
            "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(folder / "v_%03d.ts"),
            str(folder / "video.m3u8"),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    subprocess.run(  # noqa: S603
        [
            ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=12", "-c:a", "aac", "-b:a", "128k",
            str(folder / "sound.m4a"),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip

    muxed = mux.muxed_for(f"file://{folder}/video.m3u8", f"file://{folder}/sound.m4a")
    assert muxed is not None and mux.wait_ready(muxed, timeout=60)

    port = _free_port()
    httpd = server.serve("127.0.0.1", port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    session = server.STATE.sessions.create("u")
    session.stream = Stream(
        source="YouTube",
        title="Склейка",
        kind=StreamKind.HLS,
        qualities=(
            Quality(
                height=360,
                width=640,
                bitrate_kbps=1200,
                url=mux.local_playlist_path(muxed),
            ),
        ),
        duration_s=12,
    )
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
            page = browser.new_page()
            page.goto(f"{base}/player/{session.sid}", wait_until="load")
            page.evaluate("() => document.getElementById('bigplay').click()")
            played = None
            for _ in range(60):
                time.sleep(0.5)
                played = page.evaluate(
                    """() => { const v = document.getElementById('v');
                        return {t: v.currentTime,
                                a: v.webkitAudioDecodedByteCount || 0,
                                vid: v.webkitVideoDecodedByteCount || 0,
                                err: v.error ? v.error.code : 0}; }"""
                )
                if played["a"] > 0 and played["vid"] > 0:
                    break
            browser.close()
    finally:
        httpd.shutdown()

    assert played is not None
    assert played["err"] == 0, played
    assert played["vid"] > 0, f"склейка: видео не идёт — {played}"
    assert played["a"] > 0, f"склейка: звук не идёт — {played}"
