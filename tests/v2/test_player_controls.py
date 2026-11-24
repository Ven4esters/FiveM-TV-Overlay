"""Панель управления в игровом плеере.

Зачем тесты именно на это: в v1 элементы управления были, при переписывании
в v2 потерялись — в игре осталась картинка без паузы и перемотки, а в CEF
нет ни клавиатуры, ни браузерных кнопок. Проверяем не «нарисовалось ли»,
а то, что панель управляет видео.

Три бага, пойманных живым прогоном в Chromium, закреплены тестами — они
тихие и без тестов вернутся:

1. **Синтаксическая ошибка JS.** SVG-иконки подставлялись прямо в текст
   скрипта, внутри литерала "…", и двойные кавычки разрывали строку:
   `SyntaxError: Unexpected number`, скрипт не выполнялся целиком, панель
   молча не работала.
2. **Видео перехватывало клики.** Панель была видимой, но `pointer-events`
   не наследовались на дочерние элементы, и `<video>` забирал нажатия.
3. **Стилей «во весь экран» для CEF не было** — класс `clean` добавлялся,
   а правил к нему ноль.

Сети и браузера здесь нет: проверяем HTML и JS как текст.
"""

from __future__ import annotations

import re

import pytest

from tvhost2.core.models import Quality, Stream, StreamKind
from tvhost2.web import player
from tvhost2.web.session import Session


def _session(live: bool = False) -> Session:
    session = Session(sid="ABCD1234", url="https://example.com/video")
    session.stream = Stream(
        source="тест",
        title="Проверка панели",
        kind=StreamKind.HLS,
        qualities=(Quality(height=720, width=1280, bitrate_kbps=2000, url="https://e.com/a.m3u8"),),
        is_live=live,
    )
    return session


@pytest.fixture(scope="module")
def page() -> str:
    return player.render(_session(), "http://127.0.0.1:9001", "hls.min.js")


@pytest.fixture(scope="module")
def script(page: str) -> str:
    """Только последний <script> — это наш код, а не hls.js."""
    found = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert found, "в странице нет встроенного скрипта"
    return found[-1]


class TestScriptIsValid:
    """Баг 1: скрипт не парсился, и панель не работала вообще."""

    def test_no_svg_inside_string_literal(self, script: str) -> None:
        """SVG в JS-литерале рвёт строку по двойной кавычке.

        Проверяем именно причину: внутри `"…"` не должно быть `<svg`.
        """
        for match in re.finditer(r'"[^"\n]*<svg', script):
            pytest.fail(f"SVG внутри строкового литерала: {match.group(0)[:80]}")

    def test_script_parses(self, script: str) -> None:
        """Синтаксис проверяется Node, если он есть. Нет Node — пропуск."""
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        node = shutil.which("node")
        if not node:
            pytest.skip("node не установлен — синтаксис JS не проверить")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as file:
            file.write(script)
            path = Path(file.name)
        try:
            result = subprocess.run(  # noqa: S603
                [node, "--check", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == 0, f"JS не парсится:\n{result.stderr[:500]}"
        finally:
            path.unlink(missing_ok=True)


class TestPanelMarkup:
    def test_panel_exists(self, page: str) -> None:
        assert 'id="ui"' in page

    def test_all_buttons_present(self, page: str) -> None:
        """Пульта без этих кнопок не бывает."""
        for act in ("toggle", "back", "fwd", "mute", "full"):
            assert f'data-act="{act}"' in page, f"нет кнопки {act}"

    def test_seek_bar_exists(self, page: str) -> None:
        assert 'id="bar"' in page and 'id="played"' in page

    def test_volume_slider(self, page: str) -> None:
        assert 'id="vol"' in page and 'type="range"' in page

    def test_icons_in_templates_not_in_script(self, page: str) -> None:
        """Иконки лежат в <template data-icon>: оттуда их берёт JS.

        Так SVG не попадает в строковый литерал скрипта — см. баг 1.
        """
        for icon in ("play", "pause", "mute", "vol"):
            assert f'data-icon="{icon}"' in page, f"нет шаблона {icon}"

    def test_no_unreplaced_placeholders(self, page: str) -> None:
        """@PLAY@ и прочие заглушки обязаны исчезнуть при рендере."""
        leftovers = re.findall(r"@[A-Z]+@", page)
        assert not leftovers, f"не подставились: {leftovers}"

    def test_skip_values_are_numbers(self, page: str) -> None:
        """Шаг перемотки подставлен числом, а не текстом-заглушкой."""
        assert f"v.currentTime - {player.SKIP_BACK_S}" in page
        assert f"v.currentTime + {player.SKIP_FWD_S}" in page


class TestPanelBehaviour:
    """Требования к поведению, каждое — из живого опыта."""

    def test_panel_hides_itself(self, script: str) -> None:
        """Панель не должна закрывать кино: прячется сама через 2.5 с."""
        assert "setTimeout" in script and "2500" in script

    def test_panel_stays_while_hovering(self, script: str) -> None:
        assert "hovering" in script

    def test_pointer_events_enabled_on_children(self, page: str) -> None:
        """Баг 2: без этого видео перехватывало клики по кнопкам."""
        assert "#ui.on *" in page and "pointer-events: auto" in page

    def test_clean_mode_has_styles(self, page: str) -> None:
        """Баг 3: класс clean добавлялся, стилей не было."""
        assert "body.clean #ui" in page
        assert "body.clean #wrap" in page

    def test_click_on_video_toggles(self, script: str) -> None:
        """Клик по картинке — пауза, как у телевизора."""
        assert 'v.addEventListener("click"' in script

    def test_seek_keeps_dragging_outside_bar(self, script: str) -> None:
        """Перетаскивание не обрывается, если увести курсор с полосы."""
        assert 'document.addEventListener("mousemove", onMove)' in script
        assert 'document.addEventListener("mouseup", onUp)' in script

    def test_live_has_no_seek(self, script: str) -> None:
        """В прямом эфире перемотки нет — говорим это, а не молчим."""
        assert "В прямом эфире перемотки нет" in script

    def test_keyboard_shortcuts(self, script: str) -> None:
        """В CEF клавиатуры нет, но при отладке в браузере удобно."""
        assert "ArrowLeft" in script and "ArrowRight" in script and "Space" in script

    def test_volume_changes_video(self, script: str) -> None:
        assert "v.volume" in script and "volInput" in script


class TestLiveMode:
    def test_live_marks_bar(self) -> None:
        """У эфира полоса превращается в метку «ПРЯМОЙ ЭФИР»."""
        page = player.render(_session(live=True), "http://127.0.0.1:9001", "hls.min.js")
        assert 'class="live"' in page or "LIVE = true" in page

    def test_live_flag_passed(self) -> None:
        vod = player.render(_session(live=False), "http://127.0.0.1:9001", "hls.min.js")
        assert "LIVE = false" in vod
