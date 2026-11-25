"""Восемь жалоб с живого прогона в сборке 2.1.4 — по тесту на каждую.

Зачем отдельный файл, а не дописать в существующие: жалобы пришли одним
списком, и проверять их удобно вместе — видно, что закрыто всё, а не
«семь из восьми». Каждый тест назван по номеру жалобы.

Главная общая причина: `_apply_state` падал на `subtitles` (сервер отдавал
список названий, а `player.report()` перезаписывал его ЧИСЛОМ дорожек) —
`TypeError: 'int' object is not iterable` каждые 800 мс. Всё, что в этом
методе шло ниже строки падения (серии, иконка звука, ползунок громкости),
не применялось вообще. Отсюда сразу пять жалоб из восьми.
"""

from __future__ import annotations

import pytest

from tvhost2.audio import loudness
from tvhost2.core.models import Quality, Stream, StreamKind
from tvhost2.sources import vk as vk_source
from tvhost2.web import player
from tvhost2.web.session import Session

# ---------------------------------------------------------------------------
#                         общие помощники
# ---------------------------------------------------------------------------


def _session() -> Session:
    session = Session(sid="ABCD1234", url="https://example.com/video", title="Проверка")
    session.stream = Stream(
        source="тест",
        title="Проверка",
        kind=StreamKind.HLS,
        qualities=(Quality(height=720, width=1280, bitrate_kbps=2000, url="https://e.com/a.m3u8"),),
    )
    return session


def _html() -> str:
    return player.render(_session(), "http://127.0.0.1:9000", "http://x/hls.js")


def _panel():  # noqa: ANN202 — возвращаем Panel, но импорт Qt отложен
    """Пульт без показа на экране: нужен только `_apply_state`.

    Qt импортируется внутри: модуль обязан остаться работоспособным там,
    где PySide6 нет вовсе (обычный `python -m pytest` без qtvenv).
    """
    pytest.importorskip("PySide6", reason="PySide6 не установлен")
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from tvhost2.ui.panel import Panel  # noqa: PLC0415

    app = QApplication.instance() or QApplication([])
    assert app is not None  # держит ссылку: без него виджеты падают
    return Panel()


# ---------------------------------------------------------------------------
#  жалоба 1: «Звук» ставит видео на паузу
# ---------------------------------------------------------------------------


def test_1_mute_command_does_not_touch_playback() -> None:
    """Команда `mute` меняет только `v.muted` — ни play(), ни pause()."""
    html = _html()
    start = html.index('case "mute"')
    # Весь блок команды до следующего case: в нём есть комментарий-пояснение.
    end = html.index('case "', start + len('case "mute"'))
    block = html[start:end]
    assert "v.muted" in block, "обработчик mute не трогает звук"
    for forbidden in ("v.pause", "startPlayback", "v.play"):
        assert forbidden not in block, (
            f"mute не должен звать {forbidden}: это ставит видео на паузу"
        )


def test_1_sound_command_does_not_pause() -> None:
    """`sound` включает звук и при необходимости запускает, но не паузит."""
    html = _html()
    assert 'case "sound"' in html, "нет команды включения звука"
    block = html[html.index('case "sound"') : html.index('case "sound"') + 400]
    assert "v.muted = false" in block
    assert "v.pause()" not in block


def test_1_sound_clears_the_wait_for_click() -> None:
    """В игре нет первого клика: ожидание клика снимается командой `sound`."""
    block = _html()
    start = block.index('case "sound"')
    region = block[start : start + 400]
    assert "unmuteOnClick = false" in region, (
        "без сброса флага звук в CEF остаётся выключенным до перезапуска"
    )


# ---------------------------------------------------------------------------
#  жалоба 2: «Развернуть» ничего не делает
# ---------------------------------------------------------------------------


def test_2_fullscreen_command_adds_clean_class() -> None:
    """Fullscreen API в CEF не работает — вид меняет класс `clean`."""
    html = _html()
    start = html.index('case "fullscreen"')
    block = html[start : start + 900]
    assert 'classList.add("clean")' in block, "без clean разворот в игре не виден"
    assert 'classList.remove("clean")' in block
    assert "showUI()" in block, "панель должна показаться: иначе непонятно, что нажатие сработало"


def test_2_fullscreen_uses_the_same_path_as_the_player_button() -> None:
    """Кнопка в плеере работает — команда с пульта обязана делать то же."""
    html = _html()
    start = html.index("function actFull()")
    act_full = html[start : start + 700]
    assert 'classList.add("clean")' in act_full
    # Обе ветки должны менять один и тот же признак.
    cmd = html[html.index('case "fullscreen"') :][:900]
    assert 'classList.add("clean")' in cmd and 'classList.remove("clean")' in cmd


# ---------------------------------------------------------------------------
#  жалоба 3: «Замер не удался» на VK
# ---------------------------------------------------------------------------


def test_3_vk_stream_carries_chrome_user_agent() -> None:
    """CDN VK подписан `srcAg/CHROME` и без Chrome-UA отвечает 400.

    Проверено живым запросом 23.11.2026: с UA документа сегмент отдаётся
    200, с умолчальным «Lavf/…» от ffmpeg — 400 Bad Request. Отсюда
    «Замер не удался» без объяснения причины.
    """
    assert "User-Agent" in vk_source.VK_CDN_HEADERS
    assert "Chrome" in vk_source.VK_CDN_HEADERS["User-Agent"]
    assert vk_source.VK_CDN_HEADERS["User-Agent"].lower().startswith("mozilla/")


def test_3_stream_declares_headers_for_the_measurer() -> None:
    """`Stream.headers` — единственный путь заголовков к ffmpeg."""
    source = __import__("inspect").getsource(vk_source.VkSource.resolve)
    assert "headers=" in source, "поток VK не отдаёт заголовки — замер снова сломается"


def test_3_measurement_reports_the_reason() -> None:
    """Причина отказа обязана попадать в журнал целиком, а не хвостом."""
    source = __import__("inspect").getsource(loudness.measure)
    assert "returncode" in source, "код возврата ffmpeg нужен: без него причина не видна"
    assert "stderr" in source


# ---------------------------------------------------------------------------
#  жалоба 4: стартовая громкость 100 % била по ушам
# ---------------------------------------------------------------------------


def test_4_start_volume_is_twenty_percent() -> None:
    assert pytest.approx(0.2) == player.START_VOLUME


def test_4_start_volume_lands_in_the_page() -> None:
    html = _html()
    assert 'value="20" aria-label="Громкость"' in html, "ползунок в браузере стартует со 100 %"
    assert "Number(0.20)" in html, "скрипт не выставляет громкость видео"


def test_4_start_volume_leaves_no_placeholders() -> None:
    html = _html()
    for marker in ("__START_VOL__", "__START_VOL_PCT__"):
        assert marker not in html, f"{marker} не подставлен — в браузере будет мусор"


def test_4_session_default_matches_the_player() -> None:
    assert Session(sid="X", url="https://e.com").volume == pytest.approx(0.2)


# ---------------------------------------------------------------------------
#  жалоба 5: ползунок пульта не двигался от громкости в браузере
# ---------------------------------------------------------------------------


def test_5_state_reports_volume_for_the_slider() -> None:
    session = _session()
    session.volume = 0.55
    assert session.state()["volume"] == pytest.approx(0.55)


def test_5_panel_applies_volume_from_state() -> None:
    """Пульт двигает ползунок по громкости из браузера — и не спорит с ним."""
    panel = _panel()
    panel._apply_state({"volume": 0.55, "subtitles": 0})
    assert panel.volume.value() == 55, "ползунок пульта не повторяет громкость из браузера"
    panel._apply_state({"volume": 0.0, "subtitles": 0})
    assert panel.volume.value() == 0
    panel._apply_state({"volume": 1.0, "subtitles": 0})
    assert panel.volume.value() == 100


def test_5_volume_is_clamped_and_ignores_garbage() -> None:
    """Мусор в поле громкости не должен ломать опрос состояния."""
    panel = _panel()
    panel._apply_state({"volume": 7.5, "subtitles": 0})
    assert panel.volume.value() == 100, "громкость больше единицы должна упираться в 100 %"
    panel._apply_state({"volume": "громко", "subtitles": 0})
    panel._apply_state({"volume": None, "subtitles": 0})
    assert panel.volume.value() == 100


# ---------------------------------------------------------------------------
#  жалобы 6, 7, 8: озвучка не переключается, серии не листаются
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        0,
        2,
        ["Русские"],
        (),
        None,
        "ерунда",
        True,
        {"сломано": 1},
    ],
    ids=["int0", "int2", "list", "tuple", "none", "str", "bool", "dict"],
)
def test_6_apply_state_survives_any_subtitles_type(payload: object) -> None:
    """`subtitles` приходит то списком, то числом — падать нельзя ни на чём.

    Именно это падение (`'int' object is not iterable`, каждые 800 мс)
    убивало всё, что идёт ниже: серии, озвучки, иконку звука и ползунок.
    """
    panel = _panel()
    state = {
        "subtitles": payload,
        "volume": 0.2,
        "translations": ["A", "B"],
        "translationIndex": 1,
        "episodes": ["1 серия", "2 серия"],
        "canSwitchEpisode": True,
    }
    panel._apply_state(state)  # не должно бросить
    assert panel.volume.value() == 20


def test_6_state_carries_the_chosen_translation_index() -> None:
    """Пульт не может показать «какая озвучка играет», не зная номера."""
    session = _session()
    session.translation_index = 2
    state = session.state()
    assert state["translationIndex"] == 2


def test_6_state_lists_translations_and_episodes() -> None:
    session = _session()
    session.stream = Stream(
        source="Kodik",
        title="Сериал",
        kind=StreamKind.HLS,
        audio_tracks=(),
        episodes=({"season": "", "episode": "1", "title": "1 серия"},),
        can_switch_episode=True,
    )
    state = session.state()
    assert state["episodes"] == ["1 серия"]
    assert state["canSwitchEpisode"] is True
    assert "translations" in state


def test_6_translation_index_is_validated_by_the_server() -> None:
    """Чужой номер — отказ, а не тихое «ok» с прежней озвучкой."""
    from tvhost2.web import server  # noqa: PLC0415

    source = __import__("inspect").getsource(server.OverlayHandler._translation)
    assert "0 <= index < len(tracks)" in source
    assert "нет такой озвучки" in source


def test_6_panel_fills_translation_list_idempotently() -> None:
    """Список озвучек не пересоздаётся на каждом опросе — иначе мигает."""
    from tvhost2.ui import panel  # noqa: PLC0415

    source = __import__("inspect").getsource(panel.Panel._fill_translations)
    assert "current" in source and "if titles != current" in source
