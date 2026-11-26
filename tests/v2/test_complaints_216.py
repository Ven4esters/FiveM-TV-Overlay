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


# ---------------------------------------------------------------------------
#  2.1.7 (по отчёту на сборке 2.1.6): серия откатывалась, очередь команд
#  оживала после перезагрузки, «Недавние» над пультом, кэш плейлиста
# ---------------------------------------------------------------------------


def test_217_translation_change_keeps_the_episode() -> None:
    """Смена озвучки НЕ должна откидывать на первую серию.

    Живой прогон 25.11.2026: у anidub.org серия в адрес не пишется, и
    `resolver.resolve(session.url, translation_index=...)` без `episode`
    начинал с первой — с 5-й серии человек падал на 1-ю.
    """
    from tvhost2.web import server  # noqa: PLC0415

    source = __import__("inspect").getsource(server.OverlayHandler._translation)
    assert "episode=" in source, "смена озвучки не передаёт номер серии — серия откатится"
    assert "session.stream.episode" in source


def test_217_player_page_clears_the_command_queue() -> None:
    """Новая загрузка страницы = чистая очередь команд.

    Плеер начинает опрос с `lastSeq = 0`. Без сброса он получал всю прежнюю
    очередь заново — старые `mute=true` и `pause` снова срабатывали, и видео
    «само» вставало на паузу и теряло звук.
    """
    from tvhost2.web import server  # noqa: PLC0415

    source = __import__("inspect").getsource(server.OverlayHandler._player_page)
    assert "commands.clear()" in source, "после перезагрузки страницы оживут старые команды"


def test_217_reload_command_exists_in_the_player() -> None:
    """Смену серии доводит до плеера команда, а не просьба нажать F5."""
    html = _html()
    assert 'case "reload"' in html, "плеер не умеет перезагружать страницу по команде"
    block = html[html.index('case "reload"') :][:200]
    assert "location.reload()" in block


def test_217_episode_change_asks_the_player_to_reload() -> None:
    """В игре нет ни клавиатуры, ни адресной строки — F5 нажать некому."""
    from tvhost2.ui import panel  # noqa: PLC0415

    source = __import__("inspect").getsource(panel.Panel._after_episode)
    assert '"reload"' in source, "пульт не просит плеер перезагрузить страницу"
    # Совет «нажмите F5» в игре невыполним: там нет ни клавиатуры, ни
    # адресной строки. Проверяем, что он не вернулся в код показа статуса.
    status_lines = [ln for ln in source.splitlines() if "_set_remote_status" in ln]
    assert status_lines, "статус после смены серии пропал"
    for line in status_lines:
        assert "F5" not in line, "совет «нажмите F5» в игре невыполним"


def test_217_history_is_below_the_remote() -> None:
    """«Недавние» — под пультом: ошибки должны быть видны сразу."""
    from tvhost2.ui import panel  # noqa: PLC0415

    source = __import__("inspect").getsource(panel.Panel._build_remote_tab)
    order = [
        source.index(name)
        for name in (
            "_build_url_block()",
            "_build_output_block()",
            "_build_remote_block()",
            "_build_history_block()",
        )
    ]
    assert order == sorted(order), "«Недавние» снова оказались выше пульта"


def test_217_playlist_cache_can_be_dropped() -> None:
    """У каждой серии свой набор озвучек — кэш плейлиста обязан сбрасываться."""
    from tvhost2.core.cache import TTLCache  # noqa: PLC0415

    cache: TTLCache[int] = TTLCache(limit=4, ttl_s=600)
    cache.put("anidub.org:abc", 1)
    assert cache.get("anidub.org:abc") == 1
    cache.drop("anidub.org:abc")
    assert cache.get("anidub.org:abc") is None


def test_217_cvh_exposes_playlist_reset() -> None:
    from tvhost2.sources import cvh  # noqa: PLC0415

    assert "drop_playlist" in cvh.__all__


def test_217_anidub_com_reads_episodes_from_the_player() -> None:
    """У anidub.com серии лежат объектом в скрипте плеера, а не в разметке.

    Живая проверка 25.11.2026: у «Покемон. Горизонты» 157 серий, а источник
    отдавал пульту ноль — «серии нельзя переключить».
    """
    from tvhost2.sources import anidub  # noqa: PLC0415

    payload = (
        '<script>var cfg = {"locale":"ru","hasSeries":true,'
        '"episodes":[{"link":"/s2/aaa","key":"k1","label":"1 серия"},'
        '{"link":"/s2/bbb","key":"k2","label":"3 серия"}]};</script>'
    )
    episodes = anidub.playlist_episodes(payload)
    assert len(episodes) == 2
    assert episodes[1]["label"] == "3 серия"
    assert anidub.episode_number("3 серия") == 3


def test_217_anidub_com_episode_goes_into_the_player_url() -> None:
    """Номер серии — параметром `?episode=N`, как это делает сам сайт."""
    from tvhost2.sources import anidub  # noqa: PLC0415

    player = "https://player26.example/index.php?v=/s2/aaa&playlist"
    got = anidub.pick_episode_url(player, [{"label": "1 серия", "link": "/s2/aaa", "key": "k"}], 7)
    assert "episode=7" in got
    assert "playlist" in got, "playlist потерялся — плеер не покажет серии"
    assert got.count("episode=") == 1


def test_217_anidub_com_missing_episode_is_an_honest_refusal() -> None:
    """Номера, которого нет в списке, нельзя открывать молча как другой."""
    from tvhost2.sources import anidub  # noqa: PLC0415

    source = __import__("inspect").getsource(anidub.AnidubSource.resolve)
    assert "нет {wanted}-й серии" in source


def test_217_idle_client_disconnect_is_not_logged_as_a_failure() -> None:
    """Уход клиента (WinError 10053) — не сбой сервера."""
    from tvhost2.web import server  # noqa: PLC0415

    source = __import__("inspect").getsource(server)
    marker = "except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)"
    assert marker in source, "журнал снова будет краснеть на каждом закрытии вкладки"


# ---------------------------------------------------------------------------
#  2.1.8 (отчёт на сборке 2.1.7): ValueError на anidub.com, серии
#  отскакивали из-за опроса состояния, журнал молчал о нажатиях
# ---------------------------------------------------------------------------


def test_218_anidub_film_with_zero_episode_plays() -> None:
    """У фильма единственная запись подписана «0 серия» — он должен играть.

    Раньше код искал «1-ю серию», не находил и падал: `min()` по пустому
    списку давал голый ValueError, а до него — отказ «нет 1-й серии» на
    рабочей странице (жалоба на 2.1.7).
    """
    from tvhost2.sources import anidub  # noqa: PLC0415

    source = __import__("inspect").getsource(anidub.AnidubSource.resolve)
    assert "known[0]" in source, "отказ снова может упасть на пустом диапазоне"
    # Ноль — законный номер фильма, а не «нет серии».
    assert anidub.episode_number("0 серия") == 0
    assert "!= 0" not in source, "ноль отбрасывается как отсутствие номера"


def test_218_polling_does_not_revert_the_chosen_episode() -> None:
    """Опрос состояния не должен откатывать список, пока сервер думает.

    Сервер разбирает серию 2–3 секунды, а опрос идёт каждые 800 мс с ПРЕЖНИМ
    номером. Без флага выбор человека «отскакивал» обратно — снаружи это
    выглядело как «серии не переключаются вовсе» (жалоба на 2.1.7).
    """
    panel = _panel()
    state = {
        "episodes": [f"{n} серия" for n in range(1, 9)],
        "episode": 8,
        "canSwitchEpisode": True,
    }
    panel._apply_state(dict(state))
    panel.episode_box.setCurrentIndex(2)  # человек выбрал 3-ю серию
    panel._episode_busy = True  # сервер пошёл разбирать
    panel._apply_state(dict(state))  # опрос со старым номером
    assert panel.episode_box.currentText() == "3 серия", "выбор отскочил на прежнюю серию"
    panel._episode_busy = False
    panel._apply_state({**state, "episode": 3})  # сервер ответил
    assert panel.episode_box.currentText() == "3 серия"


def test_218_episode_number_comes_from_the_label() -> None:
    """Серия — из подписи строки, а не «индекс + 1»: бывают дыры в нумерации."""
    from tvhost2.core.models import episode_number  # noqa: PLC0415

    assert episode_number("7 серия") == 7
    assert episode_number("Финал") == 0


def test_218_clicks_are_written_to_the_journal() -> None:
    """Нажатие и отправка команды видны в журнале, а не только отказы."""
    from tvhost2.ui import panel  # noqa: PLC0415

    clicks = (
        "_cmd",
        "_episode_picked",
        "_translation_changed",
        "_toggle_mute",
        "_toggle_fullscreen",
    )
    for name in clicks:
        source = __import__("inspect").getsource(getattr(panel.Panel, name))
        assert "diag_log(" in source, f"{name} ничего не пишет в журнал"


def test_218_player_reports_received_commands() -> None:
    """Плеер подтверждает получение команды — иначе обрыв не найти."""
    html = _html()
    assert '"/trace/"' in html, "плеер не сообщает о полученных командах"
    assert "плеер получил" in html


def test_218_trace_route_exists() -> None:
    from tvhost2.web import server  # noqa: PLC0415

    source = __import__("inspect").getsource(server.OverlayHandler)
    assert '"/trace/"' in source
    assert "def _trace" in source


# ---------------------------------------------------------------------------
#  2.1.9 (отчёт на сборке 2.1.8, с журналом): звук затирался, серии не
#  выбрать из-за списка, ошибка декодирования при перемотке
# ---------------------------------------------------------------------------


def test_219_play_does_not_touch_the_sound() -> None:
    """Play/пауза — только про воспроизведение, звук не трогает.

    Журнал заказчика: `toggle` шёл между нажатиями «Звук», и звук пропадал.
    Причина — `v.muted = false` в `actToggle`: человек выключил звук, нажал
    Play, и звук включился сам.
    """
    html = _html()
    start = html.index('case "toggle"')
    block = html[start : html.index('case "seek"', start)]
    assert "v.muted =" not in block, "toggle снова ПРИСВАИВАЕТ звук"
    assert "startPlayback()" in block, "toggle должен запускать воспроизведение"


def test_219_player_button_toggle_does_not_touch_the_sound() -> None:
    html = _html()
    start = html.index("function actToggle()")
    block = html[start : html.index("function actMute()", start)]
    # `v.muted` в диагностической строке — можно, присваивание — нельзя.
    assert "v.muted =" not in block, "кнопка Play на самом плеере снова глушит звук"


def test_219_sound_choice_is_remembered() -> None:
    """Выбор человека по звуку живёт отдельно от `v.muted`."""
    html = _html()
    assert "var wantSound" in html, "нет памяти о желаемом состоянии звука"
    assert "function setSound(" in html, "нет единой точки правды о звуке"
    assert "function checkSound(" in html, "результат включения не проверяется"


def test_219_sound_command_goes_through_one_place() -> None:
    html = _html()
    start = html.index('case "mute"')
    block = html[start : html.index('case "normalize"', start)]
    assert "setSound(" in block, "команда звука обходит общую функцию"


def test_219_dropdowns_are_scrollable() -> None:
    """1180 серий нельзя выбрать стандартным выпадающим списком."""
    from tvhost2.ui import panel  # noqa: PLC0415

    source = __import__("inspect").getsource(panel._scrollable)
    assert "QListView" in source
    assert "setMaxVisibleItems" in source
    # Все три селектора создаются через общий помощник — значит все с прокруткой.
    build = __import__("inspect").getsource(panel.Panel._make_pick)
    assert "_scrollable(box)" in build
    builder = __import__("inspect").getsource(panel.Panel)
    # Все три селектора создаются через общий помощник — значит все с прокруткой.
    assert builder.count("_make_pick(") >= 3


def test_219_scrollable_list_limits_rows() -> None:
    panel = _panel()
    panel._apply_state(
        {
            "episodes": [f"{n} серия" for n in range(1, 1181)],
            "episode": 1,
            "canSwitchEpisode": True,
        }
    )
    assert panel.episode_box.count() == 1180
    assert panel.episode_box.maxVisibleItems() <= 25, "список занимает весь экран"
    assert panel.episode_box.isEditable(), "нет поиска по номеру серии"


def test_219_range_requests_are_forwarded() -> None:
    """Перемотка прямого mp4 невозможна без проброса Range.

    415-МБ файл anidub.org отдавался целиком и обрезался на 8 МБ — отсюда
    «ошибка декодирования» при перемотке на ±10 секунд.
    """
    from tvhost2.web import fetch as fetch_mod  # noqa: PLC0415

    source = __import__("inspect").getsource(fetch_mod.fetch)
    assert "range_header" in source
    assert '"Range"' in source


def test_219_bridge_answers_206() -> None:
    from tvhost2.web import server  # noqa: PLC0415

    source = __import__("inspect").getsource(server.OverlayHandler._bridge)
    assert "range_header=wanted" in source, "Range не уходит источнику"
    assert "HTTP_PARTIAL" in source, "ответ не помечается как частичный"
    assert "Content-Range" in source


def test_219_accept_ranges_is_always_announced() -> None:
    """Без Accept-Ranges браузер даже не пробует перематывать."""
    from tvhost2.web import server  # noqa: PLC0415

    source = __import__("inspect").getsource(server.OverlayHandler._send)
    assert 'send_header("Accept-Ranges", "bytes")' in source


def test_219_player_errors_reach_the_journal() -> None:
    """«Ошибка декодирования» должна попадать в журнал, а не в никуда."""
    html = _html()
    # Нефатальные ошибки hls.js раньше отбрасывались молча.
    start = html.index("Hls.Events.ERROR")
    block = html[start : start + 700]
    assert "trace(" in block, "ошибки потока не пишутся в журнал"
    assert "video.error" in html, "ошибка <video> не пишется в журнал"


# ---------------------------------------------------------------------------
#  2.2.0 (отчёт на 2.1.9, с журналом Kodik/yani): серия откатывалась на
#  первую, пульт не показывал номер, заголовок врал, поток ждал CDN
# ---------------------------------------------------------------------------


def test_220_polling_does_not_reset_the_open_dropdown() -> None:
    """Опрос каждые 800 мс не должен сбивать выбор в открытом списке.

    Это и была жалоба «какую бы серию ни выбрал — уходит 1-я»: `_fill_episodes`
    вызывался на каждом опросе и делал `clear()`, а `clear()` у открытого
    списка возвращает наведение на первую строку. Человек вёл мышь к 5-й
    серии, а `activated` приходил со строкой 0.
    """
    import inspect  # noqa: PLC0415

    from tvhost2.ui import panel  # noqa: PLC0415

    source = inspect.getsource(panel.Panel._fill_episodes)
    # Список пересоздаётся только при изменении содержимого.
    assert "if titles != shown:" in source
    # И позиция двигается только при реальной смене.
    assert "if found >= 0 and found != self.episode_box.currentIndex():" in source


def test_220_live_dropdown_keeps_its_row_across_polls() -> None:
    from tvhost2.ui import panel as panel_mod  # noqa: PLC0415

    window = _panel()
    box = window.episode_box
    state = {
        "episodes": [f"{n} серия" for n in range(1, 10)],
        "episode": 1,
        "canSwitchEpisode": True,
    }
    window._apply_state(state)
    box.showPopup()
    box.view().setCurrentIndex(box.model().index(4, 0))
    assert box.view().currentIndex().row() == 4
    window._apply_state(state)  # пришёл опрос — содержимое то же
    assert box.view().currentIndex().row() == 4, "опрос сбил наведение на 1-ю строку"
    box.hidePopup()
    assert panel_mod is not None


def test_220_refusal_is_not_success() -> None:
    """200 с `ok: false` — это отказ, а не успех.

    В журнале заказчика: «серия 6 не открылась», и сразу за ней «серия 6
    открыта, просим плеер перезагрузиться» — пульт принимал отказ за успех,
    потому что проверял только транспортный код.
    """
    from tvhost2.ui.api import ApiResult  # noqa: PLC0415

    refused = ApiResult(ok=True, status=200, data={"ok": False, "reason": "нет серии"})
    assert not refused.accepted
    assert refused.accepted is False
    good = ApiResult(ok=True, status=200, data={"ok": True})
    assert good.accepted
    # Старый ответ без поля `ok` считается успехом — контракт не ломаем.
    assert ApiResult(ok=True, status=200, data={}).accepted


def test_220_panel_uses_accepted_everywhere() -> None:
    import inspect  # noqa: PLC0415

    from tvhost2.ui import panel  # noqa: PLC0415

    source = inspect.getsource(panel)
    assert source.count("result.accepted") >= 3, "где-то осталась проверка только транспорта"


def test_220_status_line_shows_the_episode() -> None:
    """«В пульте не видно, какая серия» — номер живёт в строке состояния."""
    from tvhost2.ui.panel import _episode_mark  # noqa: PLC0415

    assert _episode_mark({"episode": 5}) == "5 серия · "
    assert _episode_mark({"episode": 2, "episodeSeason": 3}) == "3 сезон · 2 серия · "
    assert _episode_mark({"episode": 0}) == ""
    assert _episode_mark({}) == ""


def test_220_kodik_opens_the_first_episode_by_number() -> None:
    """Без запрошенного номера Kodik открывал не первую серию.

    Список Kodik идёт вразнобой: у `/serial/35906` первой строкой стоит
    ВОСЬМАЯ серия, и `episodes[0]` включал именно её.
    """
    from tvhost2.sources.kodik import _episode_order  # noqa: PLC0415

    episodes = [
        {"season": 1, "episode": 8, "id": "a"},
        {"season": 1, "episode": 10, "id": "b"},
        {"season": 1, "episode": 1, "id": "c"},
    ]
    assert min(episodes, key=_episode_order)["episode"] == 1


def test_220_title_carries_the_episode_it_actually_plays() -> None:
    """Заголовок обязан врать ровно настолько, насколько врёт поток."""
    import inspect  # noqa: PLC0415

    from tvhost2.sources import kodik  # noqa: PLC0415

    source = inspect.getsource(kodik.KodikSource.resolve)
    assert "shown = current_episode if episodes else 0" in source, (
        "заголовок снова строится по ЗАПРОШЕННОМУ номеру, а не по настоящему"
    )


def test_220_cvh_title_changes_with_the_episode() -> None:
    """У yani.tv заголовок не менялся при переходе по сериям."""
    import inspect  # noqa: PLC0415

    from tvhost2.sources import cvh  # noqa: PLC0415

    source = inspect.getsource(cvh)
    assert "· {current_episode} серия" in source, "серия не попала в заголовок"


def test_220_segment_cache_saves_the_second_request() -> None:
    """Сегмент с CDN идёт секунды, hls.js столько не ждёт — нужен повтор из памяти."""
    from tvhost2.web.server import SegmentCache  # noqa: PLC0415

    cache = SegmentCache()
    assert cache.get("k") is None
    cache.put("k", b"payload", "video/mp2t")
    found = cache.get("k")
    assert found is not None
    assert found[0] == b"payload"
    # Ограничение по размеру: гигантский файл (полный mp4) не кладём.
    cache.put("big", b"x" * (SegmentCache.LIMIT_BYTES // 2), "video/mp4")
    assert cache.get("big") is None


def test_220_segment_cache_evicts_the_oldest() -> None:
    from tvhost2.web.server import SegmentCache  # noqa: PLC0415

    cache = SegmentCache()
    chunk = b"x" * (SegmentCache.LIMIT_BYTES // 8)
    for n in range(9):  # 9 × 8 МБ = 72 МБ > 64 МБ
        cache.put(f"s{n}", chunk, "video/mp2t")
    assert cache.get("s0") is None, "самый старый сегмент не вытеснен"
    assert cache.get("s8") is not None


def test_220_bridge_serves_segments_from_memory() -> None:
    import inspect  # noqa: PLC0415

    from tvhost2.web import server  # noqa: PLC0415

    source = inspect.getsource(server.OverlayHandler._bridge)
    # Кэш теперь читается через отдельный метод (развилок в мосте и без
    # того много), а пишется — потоковой отдачей `_send_stream`.
    assert "self._from_cache(" in source
    assert "_send_stream(" in source
    assert "STATE.segments.get(" in inspect.getsource(server.OverlayHandler._from_cache)
    assert "STATE.segments.put(" in inspect.getsource(server.OverlayHandler._send_stream)


# ---------------------------------------------------------------------------
#  2.2.1 (отчёт на 2.2.0): поле серии пустое, Kodik не стартует, озвучка
#  «нет такой» после смены серии, таймауты сегментов
# ---------------------------------------------------------------------------


def test_221_new_answer_carries_the_episode_list() -> None:
    """`/new` обязан отдавать серии сразу, а не только через опрос.

    Иначе между открытием и первым `/state` (800 мс, на медленном разборе
    дольше) поле серии стояло пустым, и человек читал в нём серую подсказку
    «серия», принимая её за значение.
    """
    import inspect  # noqa: PLC0415

    from tvhost2.web import server  # noqa: PLC0415

    source = inspect.getsource(server.OverlayHandler._create_session)
    for key in ('"episodes"', '"episode"', '"canSwitchEpisode"'):
        assert key in source, f"в ответе /new нет {key}"


def test_221_empty_episode_box_is_never_shown() -> None:
    """Пустое поле серии не показываем: это «ещё не пришло», а не «нет серий»."""
    import inspect  # noqa: PLC0415

    from tvhost2.ui import panel  # noqa: PLC0415

    source = inspect.getsource(panel.Panel._fill_episodes)
    assert "has_rows = self.episode_box.count() > 0" in source
    assert "setVisible(bool(can) and has_rows)" in source


def test_221_episode_box_is_wide_enough() -> None:
    """«Выбор серии маленький и без числа» — поле сжималось по тексту."""
    from tvhost2.ui import panel as panel_mod  # noqa: PLC0415

    window = _panel()
    assert window.episode_box.minimumWidth() >= 132
    assert panel_mod is not None


def test_221_translation_is_found_by_title_not_index() -> None:
    """Студию ищем по названию: состав списка у anidub.org разный по сериям.

    На 1-й и 27-й серии четыре озвучки, на 500-й — три и «AniLiberty» нет
    вовсе. Индекс «0» там означает уже другого актёра, а «3» вылетает за
    пределы списка — отсюда «нет такой озвучки» при переключении.
    """
    import inspect  # noqa: PLC0415

    from tvhost2.web import server  # noqa: PLC0415

    source = inspect.getsource(server._keep_translation)
    assert "translation_wanted" in source
    assert "titles.index(wanted)" in source


def test_221_session_remembers_the_translation_name() -> None:
    from tvhost2.web.session import Session  # noqa: PLC0415

    session = Session(sid="x", url="u")
    assert session.translation_wanted == ""


def test_221_hls_timeouts_cover_a_slow_cdn() -> None:
    """Замер: первый сегмент Kodik идёт 5.3 с, штатный таймаут меньше.

    Отсюда `fragLoadTimeOut` при `t=0.0`: видео не начиналось ни разу.
    """
    html = _html()
    for setting in (
        "fragLoadingTimeOut: 60000",
        "manifestLoadingTimeOut: 30000",
        "levelLoadingTimeOut: 30000",
    ):
        assert setting in html, f"нет настройки {setting}"


def test_221_segment_cache_can_claim_a_slot() -> None:
    """Один сегмент не должен качаться дважды одновременно."""
    from tvhost2.web.server import SegmentCache  # noqa: PLC0415

    cache = SegmentCache()
    assert cache.claim("k") is True
    assert cache.claim("k") is False, "второй запрос должен ждать, а не дублировать"
    assert cache.busy("k") is True
    cache.release("k")
    assert cache.busy("k") is False
    assert cache.claim("k") is True


# ---------------------------------------------------------------------------
#  2.2.2 (отчёт на 2.2.1): 502 на сегментах, замер громкости, пропадающее
#  уведомление, одна и та же серия, «нет такой озвучки»
# ---------------------------------------------------------------------------


def test_222_segments_are_streamed_not_buffered() -> None:
    """Сегмент anidub.com (3 МБ) приходит 22 с — копить его нельзя.

    Пока копили, hls.js сдавался и просил заново, повтор лез в тот же
    медленный канал — в журнале `fragLoadError HTTP 502` и «серия не
    запускается».
    """
    import inspect  # noqa: PLC0415

    from tvhost2.web import server  # noqa: PLC0415

    source = inspect.getsource(server.OverlayHandler._send_stream)
    assert "response.read(64 * 1024)" in source, "сегмент снова читается целиком"
    assert "self.wfile.write(chunk)" in source, "байты не уходят по мере чтения"
    # И мост обязан отдавать сегменты именно этим путём.
    bridge = inspect.getsource(server.OverlayHandler._bridge)
    assert "_send_stream(" in bridge


def test_222_playlist_detection_does_not_fetch() -> None:
    """Плейлист или сегмент решаем по адресу, без скачивания."""
    from tvhost2.web.server import _looks_like_playlist  # noqa: PLC0415

    assert _looks_like_playlist("https://cdn/x/manifest.m3u8")
    assert _looks_like_playlist("https://cdn/x/720.mp4:hls:manifest.m3u8")
    assert _looks_like_playlist("file:///tmp/a/index.m3u8")
    assert not _looks_like_playlist("https://cdn/x/seg-1-v1-a1.ts")
    assert not _looks_like_playlist("https://cdn/x/master")


def test_222_loudness_says_what_is_wrong() -> None:
    """«Замер не удался» не объясняло причину.

    Две разные беды (нет ffmpeg / поток не прочитался) сливались в одно
    сообщение, и человек не знал, что чинить.
    """
    import inspect  # noqa: PLC0415

    from tvhost2.web import server  # noqa: PLC0415

    source = inspect.getsource(server.OverlayHandler._loudness)
    assert '"нужен ffmpeg"' in source
    assert '"fix"' in source, "нет подсказки, что делать"


def test_222_subtitle_notice_survives_polling() -> None:
    """Уведомление про субтитры не должно исчезать через 800 мс."""
    from tvhost2.ui import panel as panel_mod  # noqa: PLC0415

    window = _panel()
    window.btn_subs.click()
    shown = window.remote_status.text()
    assert shown, "уведомление не показалось"
    for _ in range(4):  # четыре опроса подряд
        window._apply_state({"playing": True, "position": 0, "duration": 0})
    assert window.remote_status.text() == shown, "опрос затёр уведомление"
    assert panel_mod is not None


def test_222_stream_url_is_versioned() -> None:
    """Адрес потока обязан меняться при смене серии.

    `/stream/<sid>` одинаков для всех серий, и CEF мог достать мастер
    прежней серии из кэша — «разные серии приводят к одной».
    """
    from tvhost2.web import player  # noqa: PLC0415

    assert '"/stream/" + SID + "?v=" + STREAM_VERSION' in player.SCRIPT
    assert 'var STREAM_VERSION = "__STREAM_VERSION__"' in player.SCRIPT


def test_222_playlists_forbid_caching() -> None:
    import inspect  # noqa: PLC0415

    from tvhost2.web import server  # noqa: PLC0415

    source = inspect.getsource(server.OverlayHandler._playlist)
    assert "Pragma" in source and "no-cache" in source


def test_222_busy_flag_is_cleared_on_error() -> None:
    """Одна неудача не должна навсегда отключать выбор.

    В журнале: одна ошибка «нет такой озвучки» — и дальше ни одна озвучка
    не переключалась: флаг `_translation_busy` оставался взведённым.
    """
    import inspect  # noqa: PLC0415

    from tvhost2.ui import panel  # noqa: PLC0415

    source = inspect.getsource(panel.Panel._translation_changed)
    # Флаг снимается и в ветке ApiError, и в ветке отказа.
    assert source.count("self._translation_busy = False") >= 2
    episode = inspect.getsource(panel.Panel._episode_picked)
    assert "except ApiError as exc:" in episode
    assert episode.count("self._episode_busy = False") >= 2


def test_222_missing_studio_is_reported() -> None:
    """Просили студию, которой нет на этой серии — говорим прямо."""
    import inspect  # noqa: PLC0415

    from tvhost2.web import server  # noqa: PLC0415

    source = inspect.getsource(server.OverlayHandler._translation)
    assert "missing" in source
    assert "warning" in source
    panel_source = inspect.getsource(server.OverlayHandler)
    assert "warning" in inspect.getsource(
        __import__("tvhost2.ui.panel", fromlist=["panel"]).Panel._translation_changed
    ), "пульт не показывает предупреждение"
    assert panel_source is not None


# ---------------------------------------------------------------------------
#  2.2.2 (продолжение): anidub.com открывал первую серию для всех номеров
# ---------------------------------------------------------------------------


def test_2222_anidub_requests_playlist_parameter() -> None:
    """Без `&playlist` плеер отдаёт `"episodes":[]`.

    Проверено на «Покемон. Горизонты» 26.11.2026: с параметром приходит
    полный список из 157 записей, без него — пустой, и играет та серия,
    что записана в пути (первая).
    """
    from tvhost2.sources.anidub import _with_playlist  # noqa: PLC0415

    assert "playlist" in _with_playlist("https://p/index.php?v=/s2/abc")
    assert _with_playlist("https://p/index.php?v=/s2/abc").endswith("&playlist")
    assert _with_playlist("https://p/index.php?x=1") == "https://p/index.php?x=1&playlist"
    already = "https://p/index.php?v=/s2/abc&playlist"
    assert _with_playlist(already) == already


def test_2222_episode_uses_its_own_link_not_the_number() -> None:
    """Серия задаётся ПУТЁМ `/s2/<ключ>`, а не параметром `?episode=N`.

    Плеер молча игнорирует номер и отдаёт первую — отсюда «запуск разных
    серий приводил к одной и той же серии».
    """
    from tvhost2.sources.anidub import episode_embed_url  # noqa: PLC0415

    embed = "https://p/index.php?v=/s2/first"
    episodes = [
        {"label": "1 серия", "link": "/s2/first", "key": "a"},
        {"label": "3 серия", "link": "/s2/third", "key": "b"},
        {"label": "4 серия", "link": "/s2/fourth", "key": "c"},
    ]
    url = episode_embed_url(embed, episodes, 3)
    assert "third" in url, "не подставился путь нужной серии"
    assert "%2Fs2%2Fthird" in url or "/s2/third" in url
    assert "fourth" not in url
    # Номера, которого нет в списке, — оставляем адрес как есть.
    assert episode_embed_url(embed, episodes, 99) == embed


def test_2222_anidub_resolve_passes_playlist_and_link() -> None:
    import inspect  # noqa: PLC0415

    from tvhost2.sources import anidub  # noqa: PLC0415

    source = inspect.getsource(anidub.AnidubSource.resolve)
    assert "_with_playlist(embed)" in source
    assert "episode_embed_url(embed, episodes, wanted)" in source


# ---------------- 2.2.3: ffmpeg в собранном .exe ----------------
# Жалоба: «почему не находит ffmpeg, если он в зависимостях и должен идти
# в компилируемый .exe». В исходниках ffmpeg находился, в .exe — нет.


def test_2223_spec_puts_ffmpeg_into_build() -> None:
    """ffmpeg должен лежать в данных сборки, а не «где-то в site-packages»."""
    import re  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    spec = Path(__file__).resolve().parents[2] / "tvhost.spec"
    text = spec.read_text(encoding="utf-8")
    # Собираем функцию из спеки, чтобы проверить, что она вообще даёт файл.
    assert "_ffmpeg_binaries()" in text, "спека не кладёт ffmpeg в binaries"
    assert "binaries=_ffmpeg_binaries()" in text
    # И что она не «заглушка» с пустым списком.
    start = text.find("def _ffmpeg_binaries")
    body = text[start : text.find("# Данные повторяют", start)]
    assert "imageio_ffmpeg" in body
    assert "binaries" in body
    assert not re.search(r"def _ffmpeg_binaries.*?return \[\]\s*\n\s*\n", body, re.S)


def test_2223_scan_bundle_finds_renamed_ffmpeg(tmp_path, monkeypatch) -> None:
    """Поиск по маске: имя бинарника зависит от версии imageio-ffmpeg.

    `get_ffmpeg_exe()` сверяет файл с жёстко заданным именем платформы
    (`ffmpeg-win-x86_64-v9.0.2.exe`). Если в сборку попала другая версия,
    он отвечает «нет» — и человек видит «замер не удался» при живом ffmpeg.
    """
    from tvhost2.core import ffmpeg_bin  # noqa: PLC0415

    folder = tmp_path / "imageio_ffmpeg" / "binaries"
    folder.mkdir(parents=True)
    fake = folder / "ffmpeg-win-x86_64-v9.9.9.exe"
    fake.write_bytes(b"MZ")

    monkeypatch.setattr(ffmpeg_bin, "_CACHE", {}, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(ffmpeg_bin, "app_dir", lambda: tmp_path / " рядом-нет")

    assert ffmpeg_bin._scan_bundle() == str(fake)


def test_2223_find_ffmpeg_uses_bundle_when_imageio_fails(monkeypatch) -> None:
    """Если imageio-ffmpeg не нашёл файл — выручает поиск по сборке."""
    from tvhost2.core import ffmpeg_bin  # noqa: PLC0415

    monkeypatch.setattr(ffmpeg_bin, "_CACHE", {}, raising=False)
    monkeypatch.setattr(ffmpeg_bin.shutil, "which", lambda _n: None)
    monkeypatch.setattr(ffmpeg_bin, "app_dir", lambda: ffmpeg_bin.Path("/нет-такой-папки"))
    monkeypatch.setattr(ffmpeg_bin, "_scan_bundle", lambda: "C:/распакован/ffmpeg.exe")

    import imageio_ffmpeg  # noqa: PLC0415

    def _boom(_self=None):  # imageio-ffmpeg в этой версии поднимает RuntimeError
        raise RuntimeError("ffmpeg not found")

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", _boom)

    assert ffmpeg_bin.find_ffmpeg() == "C:/распакован/ffmpeg.exe"


def test_2223_diag_ffmpeg_route_exists() -> None:
    """Маршрут /diag/ffmpeg: спросить у собранной программы, что она видит."""
    import inspect  # noqa: PLC0415

    from tvhost2.web import server  # noqa: PLC0415

    source = inspect.getsource(server)
    assert '"/diag/ffmpeg": self._diag_ffmpeg' in source
    assert "def _diag_ffmpeg" in source
    assert '"runs"' in source
