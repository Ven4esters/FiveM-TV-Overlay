"""Тесты журнала диагностики (infra/diagnostics.py).

Этот модуль — единственный способ понять, что произошло на машине
пользователя. Если он теряет события или врёт в отчёте, разбираться
с «у меня не работает» снова придётся наугад. Поэтому проверяем:
запись в память и файл, восстановление после перезапуска, ротацию,
устойчивость к битым строкам и отсутствие секретов в отчёте.
"""

from __future__ import annotations

from pathlib import Path


class TestRecording:
    """Событие попадает и в память, и в файл."""

    def test_log_appends_to_memory(self, isolated_diag):
        isolated_diag.log("req", "200 playlist.m3u8", code=200)
        events = isolated_diag.recent()
        assert len(events) == 1
        assert events[0]["kind"] == "req"
        assert events[0]["text"] == "200 playlist.m3u8"
        assert events[0]["code"] == 200

    def test_log_writes_to_file(self, isolated_diag):
        isolated_diag.log("err", "CDN не отдал плейлист", code=403)
        content = Path(isolated_diag.LOG_PATH).read_text(encoding="utf-8")
        assert "CDN не отдал плейлист" in content
        assert "code=403" in content

    def test_file_line_has_time_prefix(self, isolated_diag):
        isolated_diag.log("new", "готово")
        first_line = Path(isolated_diag.LOG_PATH).read_text(encoding="utf-8").splitlines()[0]
        # формат: ЧЧ:ММ:СС [kind] текст
        assert first_line[2] == ":" and first_line[5] == ":"
        assert "[new]" in first_line

    def test_recent_respects_limit(self, isolated_diag):
        for i in range(10):
            isolated_diag.log("req", f"запрос {i}")
        assert len(isolated_diag.recent(limit=3)) == 3

    def test_recent_returns_last_events(self, isolated_diag):
        for i in range(5):
            isolated_diag.log("req", f"запрос {i}")
        assert isolated_diag.recent(limit=2)[-1]["text"] == "запрос 4"

    def test_clear_empties_memory(self, isolated_diag):
        isolated_diag.log("req", "что-то")
        isolated_diag.clear()
        assert isolated_diag.recent() == []


class TestRobustness:
    """Журнал не должен падать от неожиданных данных."""

    def test_long_text_is_truncated(self, isolated_diag):
        """Огромный текст ошибки не должен раздувать файл."""
        isolated_diag.log("err", "x" * 5000)
        assert len(isolated_diag.recent()[0]["text"]) <= 400

    def test_long_extra_value_is_truncated(self, isolated_diag):
        isolated_diag.log("err", "ошибка", detail="y" * 2000)
        assert len(str(isolated_diag.recent()[0]["detail"])) <= 300

    def test_numbers_stay_numbers(self, isolated_diag):
        """Числа в отчёте должны остаться числами, а не строками."""
        isolated_diag.log("new", "готово", sec=8.2)
        assert isolated_diag.recent()[0]["sec"] == 8.2

    def test_broken_file_line_is_skipped(self, isolated_diag):
        """Битая строка в файле не должна ронять восстановление."""
        Path(isolated_diag.LOG_PATH).write_text(
            "мусор без формата\n16:02:37 [req] нормальная строка  code=200\n",
            encoding="utf-8",
        )
        isolated_diag._restore_from_file()
        texts = [e["text"] for e in isolated_diag.recent()]
        assert any("нормальная строка" in t for t in texts)


class TestRotation:
    """Файл журнала не растёт бесконечно."""

    def test_rotation_creates_backup(self, isolated_diag, monkeypatch):
        monkeypatch.setattr(isolated_diag, "_MAX_FILE_BYTES", 100)
        Path(isolated_diag.LOG_PATH).write_text("z" * 200, encoding="utf-8")
        isolated_diag.log("req", "после ротации")
        backup = Path(isolated_diag.LOG_PATH + ".1")
        assert backup.is_file()
        assert "после ротации" in Path(isolated_diag.LOG_PATH).read_text(encoding="utf-8")


class TestReport:
    """Итоговый отчёт пригоден для пересылки."""

    def test_report_has_both_sections(self, isolated_diag):
        isolated_diag.log("err", "тестовая ошибка")
        report = isolated_diag.report_text()
        assert "СНИМОК ОКРУЖЕНИЯ" in report
        assert "ЖУРНАЛ СОБЫТИЙ" in report

    def test_report_contains_logged_event(self, isolated_diag):
        isolated_diag.log("err", "CDN отказал 403")
        assert "CDN отказал 403" in isolated_diag.report_text()

    def test_report_marks_errors(self, isolated_diag):
        """Ошибку должно быть видно глазами среди сотни строк."""
        isolated_diag.log("err", "не отдал плейлист")
        assert "ОШИБКА" in isolated_diag.report_text()

    def test_empty_report_does_not_crash(self, isolated_diag):
        report = isolated_diag.report_text()
        assert isinstance(report, str)
        assert len(report) > 0

    def test_report_has_no_secrets(self, isolated_diag):
        """В отчёте не должно быть cookie и токенов — его пересылают людям."""
        isolated_diag.log("req", "200 cdn/x.m3u8?token=SECRET123&sig=ABC")
        report = isolated_diag.report_text()
        assert "SECRET123" not in report
        assert "sig=ABC" not in report


class TestShortUrl:
    """Адреса в журнале должны быть короткими, но узнаваемыми."""

    def test_keeps_host_and_file(self, isolated_diag):
        short = isolated_diag.short_url("https://cdn.example.com/very/long/path/x.m3u8")
        assert "cdn.example.com" in short
        assert "x.m3u8" in short

    def test_is_short(self, isolated_diag):
        long_url = "https://cdn.example.com/" + "a" * 300 + "/x.m3u8?q=1"
        assert len(isolated_diag.short_url(long_url)) <= 90

    def test_empty_url_is_safe(self, isolated_diag):
        assert isolated_diag.short_url("") == ""
