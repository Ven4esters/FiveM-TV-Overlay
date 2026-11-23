"""Тесты инфраструктурных модулей: кеш, имена файлов, размеры, ffmpeg.

Это «сантехника»: ошибки здесь проявляются как «файл не найден»,
«странное имя» или «занято 4 ГБ при пустой папке» — то есть далеко
от места, где сделана ошибка.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tvhost.infra import cache, cleanup, ffmpeg_bin


class TestCacheFilenames:
    """Имя файла в кеше должно быть уникальным и безопасным для диска."""

    def test_same_url_same_name(self):
        first = cache._build_filename("https://cdn/x/movie.mp4")
        second = cache._build_filename("https://cdn/x/movie.mp4")
        assert first == second

    def test_different_urls_different_names(self):
        assert cache._build_filename("https://cdn/x/a.mp4") != cache._build_filename(
            "https://cdn/x/b.mp4"
        )

    def test_name_is_filesystem_safe(self):
        """В имени не должно быть /, ?, : и прочих запрещённых символов."""
        name = cache._build_filename("https://cdn/x/фильм:2?.mp4?token=1")
        forbidden = set('/\\:*?"<>|')
        assert not (forbidden & set(name))

    def test_secret_token_does_not_leak_into_name(self):
        """Токен в адресе не должен попадать в имя файла на диске."""
        name = cache._build_filename("https://cdn/x/m.mp4?token=SECRET123")
        assert "SECRET123" not in name

    def test_name_is_not_too_long(self):
        """Слишком длинное имя не создастся на Windows (лимит пути)."""
        name = cache._build_filename("https://cdn/" + "a" * 300 + "/movie.mp4")
        assert len(name) < 120


class TestCacheExtension:
    """Расширение файла в кеше: телевизор определяет формат по нему."""

    def test_mp4_kept(self):
        assert cache._ext_for("https://cdn/x/m.mp4", None) == ".mp4"

    def test_content_type_used_when_no_extension(self):
        assert cache._ext_for("https://cdn/x/stream", "video/webm") == ".webm"

    def test_default_is_mp4(self):
        assert cache._ext_for("https://cdn/x/stream", None) == ".mp4"

    def test_html_content_type_not_used(self):
        """HTML вместо видео не должен сохраняться как .html — это не видео."""
        ext = cache._ext_for("https://cdn/x/stream", "text/html; charset=utf-8")
        assert ext == ".mp4"


class TestHumanSize:
    """Размер в отчёте должен читаться человеком."""

    @pytest.mark.parametrize(
        ("size", "expected_part"),
        [
            (0, "0"),
            (512, "Б"),
            (2048, "КБ"),
            (5 * 1024 * 1024, "МБ"),
            (3 * 1024**3, "ГБ"),
        ],
    )
    def test_formats_with_unit(self, size, expected_part):
        assert expected_part in cleanup._human(size)

    def test_negative_is_safe(self):
        """Отрицательный размер не должен ломать вывод."""
        assert cleanup._human(-1)


class TestCleanupPaths:
    """Уборщик смотрит в папку проекта, а не внутрь пакета (ERR-059).

    Пока BASE_DIR указывал на src/tvhost/infra, cleanup искал videos/cache
    внутри пакета, не находил и молча ничего не чистил — автoочистка при
    старте прокси не работала вовсе.
    """

    def test_cache_dirs_point_to_project_root(self):
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[1]
        for path in cleanup.CACHE_DIRS:
            got = Path(path).resolve()
            assert got.parent.parent == project_root.resolve(), (
                f"уборщик смотрит в {got}, а должен в <корень проекта>/videos/*"
            )
            assert got.name in {"cache", "tmp"}

    def test_base_dir_is_not_inside_package(self):
        from pathlib import Path

        base = Path(cleanup.BASE_DIR).resolve()
        assert base.name != "infra", f"BASE_DIR всё ещё указывает на пакет: {base}"
        assert (base / "src" / "tvhost").is_dir(), f"BASE_DIR не корень проекта: {base}"

    def test_dry_run_finds_files_in_given_dir(self, tmp_path, monkeypatch):
        """--dry-run видит файлы и ничего не удаляет."""
        import os
        import time

        cache_dir = tmp_path / "videos" / "cache"
        cache_dir.mkdir(parents=True)
        old_file = cache_dir / "old.mp4"
        old_file.write_bytes(b"x" * 100)
        # делаем файл старым на 10 дней
        past = time.time() - 10 * 86400
        os.utime(old_file, (past, past))

        monkeypatch.setattr(cleanup, "CACHE_DIRS", [str(cache_dir)])
        found = cleanup._cache_files(max_age_days=1)
        assert any(p == str(old_file) for p, _s, _a in found)

        freed = cleanup.cleanup(max_age_days=1, dry_run=True)
        assert freed > 0, "dry-run должен посчитать освобождаемые байты"
        assert old_file.exists(), "dry-run не имеет права удалять файлы"


class TestCachePath:
    """Путь к файлу кеша строится только для существующего файла."""

    def test_unknown_returns_none(self):
        assert cache.pathy_for("нет-такого-файла.mp4") is None

    def test_existing_file_returned(self, tmp_path, monkeypatch):
        """Имя передаётся БЕЗ расширения — функция сама перебирает допустимые."""
        monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
        target = tmp_path / "movie.mp4"
        target.write_bytes(b"data")
        assert cache.pathy_for("movie") == target

    def test_any_supported_extension_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
        target = tmp_path / "clip.webm"
        target.write_bytes(b"data")
        assert cache.pathy_for("clip") == target

    def test_filename_helper_matches_pathy_for(self, tmp_path, monkeypatch):
        """Имя из _build_filename должно находиться через pathy_for."""
        monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
        url = "https://cdn/x/movie.mp4"
        name = cache._build_filename(url)
        (tmp_path / f"{name}.mp4").write_bytes(b"data")
        assert cache.pathy_for(name) is not None


class TestFfmpeg:
    """ffmpeg ищется один раз и даёт понятный результат."""

    def test_find_returns_path_or_none(self):
        result = ffmpeg_bin.find_ffmpeg()
        assert result is None or isinstance(result, str)

    @pytest.mark.skipif(ffmpeg_bin.find_ffmpeg() is None, reason="ffmpeg не установлен")
    def test_require_returns_existing_file(self):
        path = ffmpeg_bin.require_ffmpeg()
        assert Path(path).exists()
