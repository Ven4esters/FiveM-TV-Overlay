"""Тесты ядра «закрепления» ссылок (core/pins.py).

Проверяем то, что легко сломать и трудно заметить: генерацию id,
определение расширения файла, хранение записей и заголовки браузера.
"""

from __future__ import annotations


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
