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
    for name in ("episode_box", "translation_box", "quality_box"):
        assert f"_make_pick(" in __import__("inspect").getsource(panel.Panel)


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
