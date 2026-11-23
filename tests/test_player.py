"""Тесты страницы-плеера и хранилища сессий (web/player.py).

Страница отдаётся в браузер телевизора, где нет отладчика: ошибку в ней
не увидеть. Поэтому проверяем статически то, что ломает воспроизведение:
настройки hls.js, ES5-совместимость и подстановку данных сессии.
"""

from __future__ import annotations

import re

import pytest

from tvhost.web import player


@pytest.fixture()
def session():
    """Минимальная сессия для рендера страницы."""
    store = player.SessionStore()
    return store.create(
        url="https://cdn.example/x.m3u8", mode="r", title="Тестовое видео", sub="подзаголовок"
    )


class TestSessionStore:
    """Хранилище сессий: создание, поиск, удаление, команды."""

    def test_create_returns_session_with_id(self):
        store = player.SessionStore()
        created = store.create(url="https://x/y.m3u8", mode="r")
        assert created.sid
        assert store.get(created.sid) is created

    def test_session_ids_are_unique(self):
        store = player.SessionStore()
        ids = {store.create(url=f"https://x/{i}.m3u8", mode="r").sid for i in range(20)}
        assert len(ids) == 20

    def test_get_unknown_returns_none(self):
        assert player.SessionStore().get("нет") is None

    def test_delete_removes_session(self):
        store = player.SessionStore()
        created = store.create(url="https://x/y.m3u8", mode="r")
        store.delete(created.sid)
        assert store.get(created.sid) is None

    def test_state_is_stored_per_session(self):
        """Состояние одной сессии не должно протекать в другую."""
        store = player.SessionStore()
        first = store.create(url="https://x/1.m3u8", mode="r")
        second = store.create(url="https://x/2.m3u8", mode="r")
        store.set_state(first.sid, {"playing": True, "currentTime": 42})
        assert store.get(second.sid).state.get("currentTime", 0) == 0

    def test_commands_are_deduplicated_by_seq(self):
        """Страница опрашивает команды с since — старые она получать не должна."""
        store = player.SessionStore()
        created = store.create(url="https://x/y.m3u8", mode="r")
        store.push_command(created.sid, "pause")
        seq1, cmds1 = store.poll_commands(created.sid, since=0)
        seq2, cmds2 = store.poll_commands(created.sid, since=seq1)
        assert len(cmds1) == 1
        assert cmds2 == []
        assert seq2 == seq1


class TestRenderedPage:
    """Что именно попадает в браузер телевизора."""

    def test_page_contains_session_id(self, session):
        page = player.render_player(session.sid, session, "http://127.0.0.1:9000")
        assert session.sid in page

    def test_page_contains_title(self, session):
        page = player.render_player(session.sid, session, "http://127.0.0.1:9000")
        assert "Тестовое видео" in page

    def test_hls_timeouts_are_generous(self, session):
        """
        Регресс на реальный баг: дефолтные таймауты hls.js (10 с манифест,
        20 с сегмент) роняли воспроизведение на медленном CDN Rutube.
        Меньше 30/60 секунд ставить нельзя.
        """
        page = player.render_player(session.sid, session, "http://127.0.0.1:9000")
        manifest = int(re.search(r"manifestLoadingTimeOut:\s*(\d+)", page).group(1))
        fragment = int(re.search(r"fragLoadingTimeOut:\s*(\d+)", page).group(1))
        assert manifest >= 30000
        assert fragment >= 60000

    def test_hls_retries_are_set(self, session):
        page = player.render_player(session.sid, session, "http://127.0.0.1:9000")
        assert int(re.search(r"manifestLoadingMaxRetry:\s*(\d+)", page).group(1)) >= 3
        assert int(re.search(r"fragLoadingMaxRetry:\s*(\d+)", page).group(1)) >= 4

    def test_start_level_is_auto(self, session):
        """startLevel: -1 — hls.js сам выберет качество по скорости."""
        page = player.render_player(session.sid, session, "http://127.0.0.1:9000")
        assert "startLevel: -1" in page

    def test_title_is_html_escaped(self):
        """Название приходит с сайта — его нельзя вставлять как HTML."""
        store = player.SessionStore()
        unsafe = store.create(
            url="https://x/y.m3u8", mode="r", title='<script>alert("xss")</script>'
        )
        page = player.render_player(unsafe.sid, unsafe, "http://127.0.0.1:9000")
        assert "<script>alert" not in page

    def test_page_reports_boot_status(self, session):
        """Страница обязана сообщить о себе: иначе в журнале не видно ТВ."""
        page = player.render_player(session.sid, session, "http://127.0.0.1:9000")
        assert "bootReport" in page

    def test_page_sends_error_details(self, session):
        """В отчёт должны идти HTTP-код и URL ошибки, а не только «ошибка»."""
        page = player.render_player(session.sid, session, "http://127.0.0.1:9000")
        assert "readyState=" in page
        assert "data.response.code" in page


class TestEs5Compatibility:
    """
    Старые ТВ-браузеры (Tizen 2.x, старый CEF) не понимают ES6+.

    Один `=>` или `const` — и страница не запустится вообще, без сообщений.
    Этот тест ловит случайное попадание современного синтаксиса.
    """

    @pytest.fixture()
    def page(self, session):
        return player.render_player(session.sid, session, "http://127.0.0.1:9000")

    def test_no_arrow_functions(self, page):
        assert "=>" not in page

    def test_no_const_or_let(self, page):
        # в CSS/HTML слова допустимы — проверяем только по JS-границам
        assert not re.search(r"\b(const|let)\s+[A-Za-z_$]", page)

    def test_no_optional_chaining(self, page):
        assert not re.search(r"\?\.[A-Za-z_$(]", page)

    def test_no_nullish_coalescing(self, page):
        assert "??" not in page

    def test_no_template_literals_in_script(self, page):
        script = page[page.index("<script>") :]
        assert "`" not in script


class TestAutoplayFallback:
    """
    CEF в GTA 5 может отклонить play() со звуком — это не «жест пользователя».

    Приём из receml/player: повторяем запуск БЕЗ ЗВУКА. Немое видео лучше
    чёрного экрана, а звук вернёт первый клик или команда volume с пульта.
    """

    @pytest.fixture()
    def page(self, session):
        return player.render_player(session.sid, session, "http://127.0.0.1:9000")

    def test_play_failure_is_retried_muted(self, page):
        assert "function startPlayback" in page
        # внутри обработчика отказа звук выключается и запуск повторяется
        i = page.index("function startPlayback")
        body = page[i : i + 900]
        assert "v.muted = true" in body
        assert "v.play()" in body

    def test_click_restores_sound(self, page):
        i = page.index("function startPlayback")
        assert 'addEventListener("click"' in page[i : i + 900]

    def test_muted_mode_does_not_loop_forever(self, page):
        # повторная попытка допускается только ОДИН раз, иначе бесконечный цикл
        i = page.index("function startPlayback")
        body = page[i : i + 900]
        assert body.count("v.play()") == 2

    def test_bigplay_button_clears_forced_mute(self, page):
        i = page.index('bigplay.addEventListener("click"')
        assert "unmuteOnClick" in page[i : i + 400]

    def test_startup_uses_start_playback(self, page):
        i = page.index("// Стартуем сразу")
        assert "startPlayback()" in page[i : i + 300]


class TestErrorVisibility:
    """
    Ошибка потока не должна исчезать через 1.1 с: иначе пользователь видит
    секунду текста, потом чёрный экран — и не знает, ждать или нажимать.
    """

    @pytest.fixture()
    def page(self, session):
        return player.render_player(session.sid, session, "http://127.0.0.1:9000")

    def test_show_error_exists(self, page):
        assert "function showError" in page

    def test_error_is_pinned_not_autohidden(self, page):
        # вырезаем ИМЕННО тело функции, иначе окно захватывает соседний showUI
        i = page.index("function showError")
        body = page[i : page.index("\n  }", i)]
        # закрепление: таймер обнуляется вместо установки нового
        assert "toastTimer = null" in body
        assert "= setTimeout(" not in body

    def test_fatal_errors_use_show_error(self, page):
        assert 'showError("Поток: "' in page

    def test_error_cleared_on_play(self, page):
        i = page.index('v.addEventListener("play"')
        assert "toastTimer === null" in page[i : i + 400]


class TestSubtitleStyling:
    """Субтитры по умолчанию ~5% высоты — с дивана не читаются."""

    @pytest.fixture()
    def page(self, session):
        return player.render_player(session.sid, session, "http://127.0.0.1:9000")

    def test_cue_style_enlarges_subtitles(self, page):
        assert "video::cue" in page

    def test_cue_uses_large_font(self, page):
        i = page.index("video::cue")
        rule = page[i : i + 120]
        assert "font-size:150%" in rule

    def test_cue_has_backdrop(self, page):
        i = page.index("video::cue")
        assert "background:rgba(0,0,0,.6)" in page[i : i + 120]
