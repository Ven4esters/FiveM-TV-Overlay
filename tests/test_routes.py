"""Тесты маршрутизации прокси.

После разбиения 458-строчного do_GET на таблицу маршрутов главный риск —
перепутать адреса местами: /pin/status должен попадать в свой обработчик,
а не в /pin, /player/<sid> — не в /p/<id>. Эти тесты фиксируют порядок.
"""

from __future__ import annotations

import pytest

from tvhost import proxy


@pytest.fixture()
def handler():
    """
    Обработчик без сокета: проверяем только выбор маршрута.

    __new__ обходит BaseHTTPRequestHandler.__init__, который сразу читает
    из сокета и ждёт запрос.
    """

    class Probe(proxy.ProxyHandler):
        def __init__(self):  # noqa: D107 — намеренно не вызываем super
            pass

    return Probe()


class TestExactRoutes:
    """Точные адреса находят свой обработчик."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/", "_route_index"),
            ("/index.html", "_route_index"),
            ("/new", "_route_new_session"),
            ("/diag", "_route_diagnostics"),
            ("/diag/clear", "_route_diagnostics_clear"),
            ("/pin", "_route_pin"),
            ("/pins", "_route_pins"),
            ("/unpin", "_route_unpin"),
            ("/direct", "_route_hls_bridge"),
            ("/hls", "_route_hls_bridge"),
            ("/rutube-master", "_route_rutube_master"),
            ("/yt-master", "_route_yt_master"),
            ("/kodik-master", "_route_kodik_master"),
            ("/twitch-master", "_route_twitch_master"),
            ("/sessions", "_route_sessions"),
            ("/subs", "_route_subs"),
            ("/resolve", "_route_api"),
            ("/list", "_route_api"),
        ],
    )
    def test_dispatches_to_expected_handler(self, handler, path, expected):
        routes = handler._routes(path)
        assert routes.__name__ == expected


class TestPrefixRoutes:
    """Адреса с идентификатором внутри разбираются по префиксу."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/player/abc123", "_route_player_page"),
            ("/cmd/abc123", "_route_commands"),
            ("/report/abc123", "_route_report"),
            ("/state/abc123", "_route_state"),
            ("/control/abc123", "_route_control"),
            ("/static/hls.min.js", "_route_static"),
            ("/trans/abc123", "_route_translation"),
            ("/local/file.mp4", "_route_local_folder"),
            ("/v/abc.mp4", "_route_local_file"),
        ],
    )
    def test_dispatches_to_expected_handler(self, handler, path, expected):
        routes = handler._routes(path)
        assert routes.__name__ == expected


class TestRouteOrdering:
    """
    Порядок важен: более конкретный адрес должен проверяться раньше.

    Именно здесь ломается маршрутизация, когда добавляют новый маршрут.
    """

    def test_pin_status_is_not_caught_by_pin(self, handler):
        """/pin/status не должен попадать в обработчик /pin."""
        assert handler._routes("/pin/status").__name__ == "_route_pin_status"

    def test_pin_log_is_not_caught_by_pin(self, handler):
        assert handler._routes("/pin/log").__name__ == "_route_pin_log"

    def test_diag_clear_is_not_caught_by_diag(self, handler):
        assert handler._routes("/diag/clear").__name__ == "_route_diagnostics_clear"

    def test_player_is_not_caught_by_short_link(self, handler):
        """/player/ — префикс, но /p/<id> — регулярка: не должны мешать друг другу."""
        assert handler._routes("/player/xyz").__name__ == "_route_player_page"

    def test_short_link_is_fallback(self, handler):
        """Неизвестный адрес уходит в короткие ссылки, а не в 500."""
        assert handler._routes("/p/abc").__name__ == "_route_short_link"

    def test_unknown_path_returns_handler_not_list(self, handler):
        """
        Таблица отдаёт РОВНО ОДИН обработчик.

        Раньше отдавался кортеж, а диспетчер шёл по нему циклом и считал
        None признаком «не обработал» — из-за этого на каждый запрос
        уходило два ответа: нормальный и 404 поверх него (ERR-056).
        """
        route = handler._routes("/совсем/неизвестный/адрес")
        assert callable(route)
        assert route.__name__ == "_route_short_link"


class TestComplexityRefactor:
    """
    Регресс на рефакторинг: диспетчер обязан оставаться маленьким.

    Если кто-то снова начнёт складывать логику в do_GET, тест это заметит
    раньше ревьюера.
    """

    def test_do_get_is_small(self):
        import inspect

        source = inspect.getsource(proxy.ProxyHandler.do_GET)
        assert len(source.splitlines()) < 40

    def test_prepare_source_is_a_dispatcher(self):
        """_prepare_source только перебирает обработчики источников."""
        import inspect

        source = inspect.getsource(proxy.ProxyHandler._prepare_source)
        assert len(source.splitlines()) < 40

    def test_each_source_has_its_handler(self):
        """На каждый поддерживаемый источник есть свой обработчик."""
        for name in (
            "_prepare_anime",
            "_prepare_kodik",
            "_prepare_twitch",
            "_prepare_rutube",
            "_prepare_generic_page",
            "_prepare_vk",
            "_prepare_youtube",
        ):
            assert hasattr(proxy.ProxyHandler, name), f"нет обработчика {name}"


class TestSingleResponsePerRequest:
    """
    На один запрос — ровно один ответ (регресс на ERR-056).

    Раньше таблица маршрутов отдавала кортеж, диспетчер шёл по нему циклом
    и считал None признаком «маршрут не подошёл». Но обработчики возвращают
    None, УЖЕ отправив ответ. Итог: каждый GET получал два ответа —
    нормальный, а поверх него 404. В журнале было две строки на запрос,
    соединение ломалось, плеер мог не получить страницу.
    """

    def test_routes_returns_single_handler(self, handler):
        route = handler._routes("/")
        assert callable(route)
        assert not isinstance(route, tuple)

    @pytest.mark.parametrize(
        "path",
        ["/", "/diag", "/list", "/player/x", "/p/abc", "/static/a.js", "/v/abc"],
    )
    def test_every_known_path_has_one_handler(self, handler, path):
        route = handler._routes(path)
        assert callable(route) and not isinstance(route, tuple), path

    def test_short_link_returns_false_for_foreign_path(self, handler):
        """False = «адрес не мой», диспетчер тогда отдаёт 404."""
        assert handler._route_short_link("/совсем/неизвестно", {}, "") is False
