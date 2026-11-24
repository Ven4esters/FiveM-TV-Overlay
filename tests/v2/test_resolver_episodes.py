"""Резолвер и серии: ОБЩИЙ механизм, а не kodik-овский.

Требование заказчика: «все правки должны работать на всех источниках, а не
только в Kodik; при этом не везде есть озвучки и серии». Отсюда два правила,
которые здесь и проверяются:

  1. `resolve(url, episode=N)` не падает у источников, которые про серии
     ничего не знают (Rutube, VK, YouTube, Twitch, OK.ru, Tvigle) — они
     просто не принимают параметр.
  2. Источник, который серии умеет (Kodik, anidub.org), получает номер.
"""

from __future__ import annotations

import inspect
from typing import ClassVar

import pytest

from tvhost2.core import Stream, StreamKind
from tvhost2.sources import (
    AnidubOrgSource,
    KodikEmbedSource,
    KodikSource,
    OkRuSource,
    RutubeSource,
    TvigleSource,
    TwitchSource,
    VkSource,
    YouTubeSource,
    YummyAnimeSource,
)
from tvhost2.web import resolver

ALL_SOURCES = (
    RutubeSource,
    VkSource,
    YouTubeSource,
    KodikSource,
    KodikEmbedSource,
    YummyAnimeSource,
    TvigleSource,
    TwitchSource,
    OkRuSource,
    AnidubOrgSource,
)


class TestSignatures:
    def test_knows_which_sources_take_episode(self) -> None:
        """Список источников с сериями — факт, а не догадка."""
        takes = {
            cls.__name__
            for cls in ALL_SOURCES
            if "episode" in inspect.signature(cls.resolve).parameters
        }
        assert takes == {
            "KodikSource",
            "KodikEmbedSource",
            "YummyAnimeSource",
            "AnidubOrgSource",
        }

    def test_every_source_accepts_url(self) -> None:
        """Минимум, который обязан быть у всех: ссылка."""
        for cls in ALL_SOURCES:
            params = inspect.signature(cls.resolve).parameters
            assert "url" in params, cls.__name__


class TestAsk:
    """`_ask` подбирает набор параметров под подпись источника."""

    class _NoEpisodes:
        """Источник без серий: принимает только ссылку и озвучку."""

        name = "Тестовый"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def resolve(self, url: str, translation_index: int = 0) -> Stream:
            self.calls.append({"translation_index": translation_index})
            return Stream(source="Тестовый", title=url, kind=StreamKind.HLS)

    class _WithEpisodes:
        """Источник с сериями: принимает всё."""

        name = "Тестовый-серии"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def resolve(
            self, url: str, translation_index: int = 0, episode: int | None = None
        ) -> Stream:
            self.calls.append({"translation_index": translation_index, "episode": episode})
            return Stream(source="Тестовый-серии", title=url, kind=StreamKind.HLS)

    def test_passes_episode(self) -> None:
        source = self._WithEpisodes()
        resolver._ask(source, "u", 2, 5)
        assert source.calls == [{"translation_index": 2, "episode": 5}]

    def test_falls_back_without_episode(self) -> None:
        """Источник серий не умеет — получает озвучку, и всё работает."""
        source = self._NoEpisodes()
        stream = resolver._ask(source, "u", 2, 5)
        assert source.calls == [{"translation_index": 2}]
        assert stream.title == "u"

    def test_episode_only(self) -> None:
        class _OnlyEpisode:
            name = "Тестовый"

            def __init__(self) -> None:
                self.calls: list[dict] = []

            def resolve(self, url: str, episode: int | None = None) -> Stream:
                self.calls.append({"episode": episode})
                return Stream(source="Тестовый", title=url, kind=StreamKind.HLS)

        source = _OnlyEpisode()
        resolver._ask(source, "u", None, 4)
        assert source.calls == [{"episode": 4}]

    def test_no_parameters_at_all(self) -> None:
        class _Plain:
            name = "Тестовый"

            def resolve(self, url: str) -> Stream:
                return Stream(source="Тестовый", title=url, kind=StreamKind.HLS)

        assert resolver._ask(_Plain(), "u", 1, 2).title == "u"

    def test_signature_error_is_visible(self) -> None:
        """Ни один набор не подошёл — это баг в источнике, а не в ссылке."""

        class _Broken:
            name = "Сломанный"

            def resolve(self) -> Stream:  # нет url
                return Stream(source="Сломанный", title="", kind=StreamKind.HLS)

        with pytest.raises(TypeError):
            resolver._ask(_Broken(), "u", 1, 2)


class TestStreamContract:
    def test_no_episodes_by_default(self) -> None:
        """Фильм: серий нет, переключать нечего, вопросов человеку не задаём."""
        stream = Stream(source="X", title="фильм", kind=StreamKind.HLS)
        assert stream.episodes == ()
        assert stream.episode == 0
        assert not stream.has_episodes
        assert not stream.can_switch_episode

    def test_series_can_switch(self) -> None:
        stream = Stream(
            source="X",
            title="сериал",
            kind=StreamKind.HLS,
            episodes=(
                {"season": "1", "episode": "1", "title": "1 серия"},
                {"season": "1", "episode": "2", "title": "2 серия"},
            ),
            episode=1,
            can_switch_episode=True,
        )
        assert stream.has_episodes
        assert stream.episode == 1

    def test_episodes_but_no_switch(self) -> None:
        """Список есть, а переключать не умеем — честно говорим «нет»."""
        stream = Stream(
            source="X",
            title="сериал",
            kind=StreamKind.HLS,
            episodes=({"season": "", "episode": "1", "title": "1 серия"},),
            can_switch_episode=False,
        )
        assert stream.episodes
        assert not stream.has_episodes


class TestAnidubPick:
    """anidub.org: серия из общего списка, дыры в нумерации не ломают."""

    PLAYLIST: ClassVar[dict] = {
        "items": [
            {"episode": 1, "vkId": "1", "voiceType": "voice", "name": "AniDUB"},
            {"episode": 1, "vkId": "2", "voiceType": "voice", "name": "StudioBand"},
            {"episode": 3, "vkId": "3", "voiceType": "voice", "name": "AniDUB"},
        ]
    }

    def test_first_episode_by_default(self) -> None:
        from tvhost2.sources.anidub_org import pick

        assert pick(self.PLAYLIST)["vkId"] == "1"

    def test_explicit_episode(self) -> None:
        from tvhost2.sources.anidub_org import pick

        assert pick(self.PLAYLIST, 0, 3)["vkId"] == "3"

    def test_missing_episode_falls_back(self) -> None:
        """Серии 2 нет — берём первую, а не пустоту и не падение."""
        from tvhost2.sources.anidub_org import pick

        assert pick(self.PLAYLIST, 0, 2)["vkId"] == "1"

    def test_translation_within_episode(self) -> None:
        from tvhost2.sources.anidub_org import pick

        assert pick(self.PLAYLIST, 1, 1)["vkId"] == "2"

    def test_episode_list_shape(self) -> None:
        from tvhost2.sources.anidub_org import tracks_by_episode

        assert sorted(tracks_by_episode(self.PLAYLIST)) == [1, 3]


class TestEpisodeUrlBySource:
    """Номер серии в ссылке запоминает ИСТОЧНИК, а не список имён в сервере.

    Зачем так: раньше сессия знала наизусть «Kodik» и «YummyAnime». Новый
    сериальный сайт пришлось бы дописывать в веб-слой, хотя это дело
    источника. Теперь достаточно, чтобы у источника был метод
    `url_with_episode`.
    """

    def test_kodik_remembers_number(self) -> None:
        from tvhost2.sources.kodik import KodikSource

        source = KodikSource()
        url = "https://kodikplayer.com/serial/35906/10ea0c/720p"
        assert source.url_with_episode(url, 5).endswith("episode=5")

    def test_yummy_uses_page_url(self) -> None:
        """У yummy своей серии нет — номер идёт в адрес страницы сайта."""
        from tvhost2.sources.yummy import YummyAnimeSource

        source = YummyAnimeSource()
        url = "https://yummyanime.tv/catalog/item/123"
        assert "episode=3" in source.url_with_episode(url, 3)

    def test_sources_without_episodes_have_no_method(self) -> None:
        """Не «заглушка, возвращающая ссылку», а отсутствие метода.

        Так сессия отличает «умею» от «не умею» одним `getattr` и не
        заводит пустых обещаний.
        """
        from tvhost2.core import registry
        from tvhost2.sources import register_all

        register_all()
        for url in (
            "https://rutube.ru/video/1/",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://vk.com/video-1_2",
        ):
            source = registry.find(url)
            assert source is not None, url
            assert not hasattr(source, "url_with_episode"), url
