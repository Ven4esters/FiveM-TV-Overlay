"""Маршрут /loudness и формат ответа.

История бага (24.11.2026): маршрут отвечал 500 «expected str, bytes or
os.PathLike object, not dict». Причин было две, и обе прятались друг за
другом:

1. `measure()` ждёт (адрес, путь к ffmpeg, заголовки), а сервер передавал
   (адрес, заголовки) — ffmpeg собирался с словарём вместо исполняемого
   файла.
2. `Loudness` не имел `as_dict()`, хотя сервер и интерфейс его звали.

Ни один тест этого не ловил: маршрут не вызывался ни разу, а у `Loudness`
проверялись только расчёты усиления. Кнопка «выровнять громкость» в
интерфейсе не работала никогда — просто никто не нажал её при включённом
журнале. Теперь проверяем контракт целиком: и формат, и маршрут, и то,
что ffmpeg реально доходит до `measure()`.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from tvhost2.audio.loudness import Loudness


class TestAsDict:
    """Формат ответа — общий контракт сервера, панели и игрового плеера."""

    def test_has_gain_db(self):
        """Панель читает именно `gainDb`: data.get("gainDb")."""
        assert "gainDb" in Loudness(lufs=-19.0, lra=6.0, peak_dbfs=-3.0).as_dict()

    def test_has_gain_linear(self):
        """Плеер читает `gainLinear`: Number(d.gainLinear)."""
        assert "gainLinear" in Loudness(lufs=-19.0, lra=6.0, peak_dbfs=-3.0).as_dict()

    def test_values_match_properties(self):
        loud = Loudness(lufs=-19.0, lra=6.0, peak_dbfs=-3.0)
        data = loud.as_dict()
        assert data["gainDb"] == loud.gain_db
        assert data["gainLinear"] == loud.gain_linear

    def test_all_values_are_json_safe(self):
        """Ответ уходит в JSON: ни объектов, ни None там быть не может."""
        data = Loudness(lufs=-19.0, lra=6.0, peak_dbfs=-3.0).as_dict()
        json.dumps(data)  # упадёт, если внутри не сериализуемое

    def test_note_is_human_text(self):
        data = Loudness(lufs=-19.0, lra=6.0, peak_dbfs=-3.0).as_dict()
        assert isinstance(data["note"], str) and data["note"]

    def test_quiet_source_gets_boost_in_linear(self):
        """Тихий звук: усиление больше единицы, и в дБ, и в разах."""
        data = Loudness(lufs=-28.0, lra=5.0, peak_dbfs=-8.0).as_dict()
        assert data["gainDb"] > 0
        assert data["gainLinear"] > 1.0


class TestServerRoute:
    """Сам маршрут: он и падал с 500."""

    @pytest.fixture()
    def server(self):
        from tvhost2.web import server as server_module

        # Кэш замеров живёт на весь процесс: без очистки второй тест
        # получил бы ответ первого и проверял бы не то.
        server_module.STATE.loudness._data.clear()
        httpd = server_module.serve("127.0.0.1", 9415)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        yield "http://127.0.0.1:9415"
        httpd.shutdown()
        worker.join(timeout=5)
        httpd.server_close()

    def test_missing_session_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(f"{server}/loudness/nosuchsid", timeout=5)  # noqa: S310
        assert info.value.code == 404

    def test_route_exists_and_answers_json(self, server, monkeypatch):
        """Маршрут должен отвечать JSON, а не 500 с текстом исключения.

        Замер подменяем: сеть и ffmpeg в unit-тестах не нужны. Важно, что
        запрос доходит до measure() с правильными аргументами и что ответ
        собирается через as_dict(), а не падает на нём.
        """
        from tvhost2.web import server as server_module

        created = server_module.STATE.sessions.create("https://example.com/stream.m3u8")
        created.stream = _fake_stream()

        seen: dict = {}

        def fake_measure(url: str, ffmpeg_exe: str, headers: dict | None = None):
            seen["url"] = url
            seen["ffmpeg"] = ffmpeg_exe
            seen["headers"] = headers
            return Loudness(lufs=-24.0, lra=5.0, peak_dbfs=-2.0)

        monkeypatch.setattr(server_module.loudness, "measure", fake_measure)
        monkeypatch.setattr(server_module, "_ffmpeg_exe", lambda: "/usr/bin/ffmpeg")

        with urllib.request.urlopen(f"{server}/loudness/{created.sid}", timeout=10) as resp:  # noqa: S310
            assert resp.status == 200
            data = json.loads(resp.read())

        assert data["gainDb"] is not None
        assert data["gainLinear"] > 0
        # Главное: в measure() уехал ИМЕННО путь к ffmpeg, а не заголовки.
        assert seen["ffmpeg"] == "/usr/bin/ffmpeg"
        assert seen["url"] == "https://example.com/high.m3u8"
        assert isinstance(seen["headers"], dict)

    def test_no_ffmpeg_gives_clear_note(self, server, monkeypatch):
        """Нет ffmpeg — честная причина в ответе, а не 500."""
        from tvhost2.web import server as server_module

        created = server_module.STATE.sessions.create("https://example.com/stream.m3u8")
        created.stream = _fake_stream()
        monkeypatch.setattr(server_module, "_ffmpeg_exe", lambda: None)

        with urllib.request.urlopen(f"{server}/loudness/{created.sid}", timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read())

        assert data["gainDb"] is None
        assert "ffmpeg" in data["note"]


def _fake_stream():
    """Поток-пустышка: маршруту нужен только адрес лучшего качества."""
    from tvhost2.core.models import Quality, Stream, StreamKind

    return Stream(
        source="тест",
        title="тест",
        kind=StreamKind.HLS,
        qualities=(
            Quality(height=720, width=1280, bitrate_kbps=2000, url="https://example.com/high.m3u8"),
        ),
    )
