"""Тесты ядра «закрепления» ссылок (core/pins.py).

Проверяем то, что легко сломать и трудно заметить: генерацию id,
определение расширения файла, хранение записей и заголовки браузера.
"""

from __future__ import annotations

import pytest

from tvhost.core import pins


class TestPinId:
    """Идентификатор закрепления должен быть стабильным и безопасным."""

    def test_same_url_gives_same_id(self, isolated_pins):
        """Один и тот же адрес — один и тот же id (идемпотентность)."""
        first = isolated_pins._pid_for("https://example.com/a.m3u8?token=1")
        second = isolated_pins._pid_for("https://example.com/a.m3u8?token=1")
        assert first == second

    def test_different_urls_give_different_ids(self, isolated_pins):
        """Разные адреса не должны сливаться в один id."""
        first = isolated_pins._pid_for("https://example.com/a.m3u8")
        second = isolated_pins._pid_for("https://example.com/b.m3u8")
        assert first != second

    def test_id_is_filesystem_safe(self, isolated_pins):
        """id попадает в имя файла — только буквы и цифры."""
        pid = isolated_pins._pid_for("https://example.com/путь?q=значение&x=1")
        assert pid.isalnum()


class TestFileExtension:
    """Расширение выходного файла берётся из адреса, но безопасно."""

    def test_mp4_kept(self, isolated_pins):
        assert isolated_pins._file_ext("https://cdn/x/movie.mp4?sig=abc") == ".mp4"

    def test_m3u8_becomes_mp4(self, isolated_pins):
        """Плейлист скачивается в mp4: телевизор не умеет открывать .m3u8 как файл."""
        assert isolated_pins._file_ext("https://cdn/x/index.m3u8") == ".mp4"

    def test_no_extension_defaults_to_mp4(self, isolated_pins):
        assert isolated_pins._file_ext("https://cdn/x/video/") == ".mp4"

    def test_query_does_not_confuse_extension(self, isolated_pins):
        """`?file=video.webm` в параметрах не должен считаться расширением пути."""
        assert isolated_pins._file_ext("https://cdn/x/stream?file=a.webm") == ".mp4"


class TestPinStorage:
    """Добавление, чтение и удаление записей."""

    def test_add_returns_record_with_required_fields(self, isolated_pins):
        record = isolated_pins.add(
            "https://cdn/x/index.m3u8",
            referer="https://site/",
            user_agent="UA",
            title="Фильм",
        )
        assert record["id"]
        assert record["url"] == "https://cdn/x/index.m3u8"
        assert record["title"] == "Фильм"
        assert record["state"]

    def test_add_is_idempotent(self, isolated_pins):
        """Повторное закрепление того же адреса не создаёт дубль."""
        first = isolated_pins.add("https://cdn/x/index.m3u8")
        second = isolated_pins.add("https://cdn/x/index.m3u8")
        assert first["id"] == second["id"]
        assert len(isolated_pins.all()) == 1

    def test_get_returns_none_for_unknown(self, isolated_pins):
        assert isolated_pins.get("нет-такого") is None

    def test_remove_deletes_record(self, isolated_pins):
        record = isolated_pins.add("https://cdn/x/index.m3u8")
        assert isolated_pins.remove(record["id"]) is True
        assert isolated_pins.get(record["id"]) is None

    def test_remove_unknown_returns_false(self, isolated_pins):
        assert isolated_pins.remove("нет-такого") is False


class TestHeadersFor:
    """Заголовки запроса восстанавливаются из записи закрепления."""

    def test_referer_and_user_agent_present(self, isolated_pins):
        record = isolated_pins.add(
            "https://cdn/x/index.m3u8",
            referer="https://site/page",
            user_agent="TestAgent/1.0",
        )
        headers = isolated_pins.headers_for(record)
        assert headers["Referer"] == "https://site/page"
        assert headers["User-Agent"] == "TestAgent/1.0"

    def test_cookies_restored(self, isolated_pins):
        record = isolated_pins.add("https://cdn/x/i.m3u8", cookies="a=1; b=2")
        assert isolated_pins.headers_for(record)["Cookie"] == "a=1; b=2"

    def test_default_user_agent_when_empty(self, isolated_pins):
        """Пустой UA — подставляем браузерный, иначе CDN откажет."""
        record = isolated_pins.add("https://cdn/x/i.m3u8")
        headers = isolated_pins.headers_for(record)
        assert "Mozilla" in headers["User-Agent"]


class TestRangeParsing:
    """
    Разбор заголовка Range (RFC 7233).

    Багов было два, и оба ломали перемотку:
      1) `bytes=-500` (последние 500 байт) читался как «от 0 до 500» —
         плеер получал начало файла вместо хвоста MP4 (moov-atom);
      2) `bytes=2000-3000` при файле 1000 байт давал диапазон (2000, 999) —
         начало больше конца, вместо честного 416.
    """

    def test_simple_range(self):
        assert pins._parse_range("bytes=0-100", 1000) == (0, 100)

    def test_suffix_range_means_last_bytes(self):
        # именно этот случай был сломан: -500 = ПОСЛЕДНИЕ 500 байт
        assert pins._parse_range("bytes=-500", 1000) == (500, 999)

    def test_suffix_range_larger_than_file(self):
        assert pins._parse_range("bytes=-5000", 1000) == (0, 999)

    def test_open_ended_range(self):
        assert pins._parse_range("bytes=500-", 1000) == (500, 999)

    def test_end_beyond_file_is_clamped(self):
        assert pins._parse_range("bytes=900-5000", 1000) == (900, 999)

    def test_single_byte(self):
        assert pins._parse_range("bytes=0-0", 1000) == (0, 0)

    @pytest.mark.parametrize("header", [None, ""])
    def test_no_header_means_whole_file(self, header):
        assert pins._parse_range(header, 1000) is None

    @pytest.mark.parametrize(
        "header",
        [
            "bytes=2000-3000",  # начало за пределами файла
            "bytes=1000-",  # ровно за концом
            "bytes=300-200",  # перевёрнутый диапазон
            "bytes=-",  # без чисел
            "мусор",  # не по формату
        ],
    )
    def test_impossible_range_raises(self, header):
        with pytest.raises(ValueError):
            pins._parse_range(header, 1000)


class TestRangeHeaders:
    """Проверка заголовков ответа для 200/206/416 — без сети, на фейковом handler."""

    class _Handler:
        def __init__(self):
            self.status = None
            self.headers: dict[str, str] = {}
            self.command = "GET"

        def send_response(self, code):
            self.status = code

        def send_header(self, name, value):
            self.headers[name] = value

        def end_headers(self):
            pass

    def test_whole_file_is_200(self):
        h = self._Handler()
        assert pins._send_file_headers(h, 1000, "video/mp4", None) is True
        assert h.status == 200
        assert h.headers["Content-Length"] == "1000"
        assert h.headers["Accept-Ranges"] == "bytes"

    def test_partial_is_206_with_range_header(self):
        h = self._Handler()
        assert pins._send_file_headers(h, 1000, "video/mp4", (500, 999)) is True
        assert h.status == 206
        assert h.headers["Content-Range"] == "bytes 500-999/1000"
        assert h.headers["Content-Length"] == "500"

    def test_out_of_bounds_is_416(self):
        h = self._Handler()
        assert pins._send_file_headers(h, 1000, "video/mp4", (2000, 999)) is False
        assert h.status == 416
        assert h.headers["Content-Range"] == "bytes */1000"

    def test_suffix_range_end_to_end(self):
        """Сквозная проверка: заголовок «последние 500» → правильный ответ."""
        h = self._Handler()
        byte_range = pins._parse_range("bytes=-500", 1000)
        assert pins._send_file_headers(h, 1000, "video/mp4", byte_range) is True
        assert h.status == 206
        assert h.headers["Content-Range"] == "bytes 500-999/1000"
