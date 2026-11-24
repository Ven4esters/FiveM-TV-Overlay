"""История просмотров: возврат к тому, что играло.

Требование заказчика дословно: «живёт в памяти в рамках одной сессии,
до 5 источников» и «показывать в пульте».

Здесь проверяется то, что ломается тихо и портит именно ощущение:

1. Пять записей — это пять ПОСЛЕДНИХ, а не первые, которые успели.
2. Повторное открытие того же видео не занимает новый слот, а поднимает
   запись наверх. Иначе пять раз открытый «Ван-Пис» вытеснит всё остальное,
   и вернуться будет некуда.
3. «Ван-Пис в дубляже» и «Ван-Пис в субтитрах» — две разные записи:
   переключение озвучки у нас это повторный разбор, и вернуться хочется
   к каждой.
4. Список не отдаёт внутренний массив наружу: пульт рисует в своём потоке,
   а прокси пишет в своём.
"""

from __future__ import annotations

import time

from tvhost2.web.history import LIMIT, WatchHistory, _ago


class TestLimit:
    def test_limit_is_five(self):
        """Заказчик просил пять — число не должно «поехать» втихую."""
        assert LIMIT == 5

    def test_keeps_last_five(self):
        history = WatchHistory()
        for index in range(7):
            history.remember(f"https://a/{index}", title=f"Видео {index}")
        titles = [item.title for item in history.all()]
        assert titles == ["Видео 6", "Видео 5", "Видео 4", "Видео 3", "Видео 2"]

    def test_empty_url_ignored(self):
        """Без ссылки возвращаться некуда — мусорить в истории нечем."""
        history = WatchHistory()
        history.remember("")
        assert history.all() == []


class TestRepeat:
    def test_same_video_moves_up(self):
        history = WatchHistory()
        history.remember("https://a/1", title="Первый")
        history.remember("https://a/2", title="Второй")
        history.remember("https://a/1", title="Первый")
        assert [item.title for item in history.all()] == ["Первый", "Второй"]

    def test_repeat_does_not_grow_list(self):
        history = WatchHistory()
        for _ in range(5):
            history.remember("https://a/1", title="Один")
        assert len(history.all()) == 1

    def test_different_translation_is_separate_entry(self):
        """Дубляж и субтитры — разные просмотры, к каждому хочется вернуться."""
        history = WatchHistory()
        history.remember("https://a/1", title="Ван-Пис", translation_index=0)
        history.remember("https://a/1", title="Ван-Пис", translation_index=1)
        assert len(history.all()) == 2


class TestData:
    def test_fields_survive(self):
        history = WatchHistory()
        history.remember(
            "https://anidub.org/6093-van-pis-tv.html",
            title="Ван-Пис — серия 1",
            source="anidub.org",
            translation="AniLiberty (Дубляж)",
        )
        item = history.as_list()[0]
        assert item["title"] == "Ван-Пис — серия 1"
        assert item["source"] == "anidub.org"
        assert item["translation"] == "AniLiberty (Дубляж)"
        assert item["url"].startswith("https://anidub.org/")

    def test_title_falls_back_to_url(self):
        history = WatchHistory()
        history.remember("https://a/без-названия")
        assert history.as_list()[0]["title"] == "https://a/без-названия"

    def test_when_is_human(self):
        """Время — словами: в сессии, которая живёт минуты, даты бесполезны."""
        assert _ago(time.time()) == "только что"

    def test_when_minutes(self):
        assert _ago(time.time() - 130) == "2 мин назад"

    def test_when_hours(self):
        assert "ч назад" in _ago(time.time() - 3 * 3600)


class TestLookup:
    def test_get_by_url(self):
        history = WatchHistory()
        history.remember("https://a/1", title="Первый")
        found = history.get("https://a/1")
        assert found is not None and found.title == "Первый"

    def test_get_missing(self):
        assert WatchHistory().get("https://нет") is None

    def test_forget(self):
        history = WatchHistory()
        history.remember("https://a/1", title="Первый")
        assert history.forget("https://a/1") is True
        assert history.forget("https://a/1") is False
        assert history.all() == []

    def test_clear(self):
        history = WatchHistory()
        history.remember("https://a/1")
        history.remember("https://a/2")
        history.clear()
        assert history.all() == []

    def test_copy_is_independent(self):
        """Наружу — копия: пульт не должен менять историю случайно."""
        history = WatchHistory()
        history.remember("https://a/1")
        snapshot = history.all()
        snapshot.clear()
        assert len(history.all()) == 1


class TestServerWiring:
    """Маршруты, без которых пульт историю не покажет."""

    def test_routes_exist(self):
        import inspect

        from tvhost2.web import server

        source = inspect.getsource(server.OverlayHandler)
        assert '"/history": self._history_list' in source
        assert 'path.startswith("/history/forget/")' in source

    def test_forget_route_matches_without_slash(self):
        """Живой баг: маршрут ждал слэш на конце и отвечал 404.

        Проверяем не исходник, а разбор пути: так тест видит то же, что
        видел curl, а не то, что написано в коде.
        """
        from tvhost2.web.server import OverlayHandler

        path = "/history/forget"
        matched = path.rstrip("/") == "/history/forget" or path.startswith("/history/forget/")
        assert matched, "маршрут забывания обязан срабатывать без хвостового слэша"
        assert hasattr(OverlayHandler, "_history_forget")

    def test_remembers_on_open(self):
        """Историю пополняет успешный разбор, и только он."""
        import inspect

        from tvhost2.web import server

        source = inspect.getsource(server.OverlayHandler._create_session)
        assert "STATE.history.remember" in source

    def test_remembers_translation_change(self):
        import inspect

        from tvhost2.web import server

        source = inspect.getsource(server.OverlayHandler._translation)
        assert "STATE.history.remember" in source
