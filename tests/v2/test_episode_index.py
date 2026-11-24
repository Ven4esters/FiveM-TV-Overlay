"""Выбор строки в списке серий: по НОМЕРУ, а не по «номер минус один».

Найдено при сверке планов 24.11.2026. Пульт ставил `setCurrentIndex(episode - 1)`,
и это верно только для сплошной нумерации. У anidub.org встречаются дыры
(серии 1, 3, 5), и тогда третья строка списка — это 5-я серия: пульт
показывал не ту и следующее переключение шло от неё.

Тест лежит отдельно от `test_ui_regressions`, потому что ему не нужен
PySide6: логика вынесена в `core/models.py` именно ради этого.
"""

from __future__ import annotations

from typing import ClassVar

from tvhost2.core.models import episode_index


class TestSolidNumbering:
    def test_finds_episode(self) -> None:
        titles = ["1 серия", "2 серия", "3 серия"]
        assert episode_index(titles, 2) == 1

    def test_first_episode(self) -> None:
        assert episode_index(["1 серия", "2 серия"], 1) == 0


class TestGaps:
    """Дыры в нумерации — главный случай, ради которого всё это."""

    TITLES: ClassVar[list[str]] = ["1 серия", "3 серия", "5 серия"]

    def test_exact_episode_found(self) -> None:
        assert episode_index(self.TITLES, 3) == 1

    def test_last_episode_found(self) -> None:
        assert episode_index(self.TITLES, 5) == 2

    def test_missing_episode_takes_below(self) -> None:
        """Серии 4 нет — показываем 3-ю: ровно её и откроет источник."""
        assert episode_index(self.TITLES, 4) == 1

    def test_never_shows_wrong_episode(self) -> None:
        """Худшее, что можно сделать, — показать соседнюю ВЫШЕ по номеру.

        Человек увидел бы «5 серия» на месте «4 серия» и решил, что
        переключение не работает.
        """
        index = episode_index(self.TITLES, 4)
        assert self.TITLES[index] != "5 серия"


class TestEdgeCases:
    def test_episode_beyond_list(self) -> None:
        """Номер вне списка — первая строка.

        Так же поступает источник: если серии нет, он отдаёт первую, и
        пульт обязан показывать именно её, а не выдумывать номер.
        """
        assert episode_index(["1 серия"], 1180) == 0

    def test_empty_list(self) -> None:
        assert episode_index([], 3) == -1

    def test_zero_number(self) -> None:
        """`episode=0` значит «номер неизвестен», а не «серия 0»."""
        assert episode_index(["1 серия", "2 серия"], 0) == -1

    def test_unparsed_row_skipped(self) -> None:
        """Строка без номера не считается «нулевой серией»."""
        assert episode_index(["мусор", "3 серия"], 2) == 1


class TestLabels:
    """Подписи у разных источников разные — номер должен читаться."""

    def test_episode_label(self) -> None:
        assert episode_index(["Эпизод 1", "Эпизод 2"], 2) == 1

    def test_series_label(self) -> None:
        assert episode_index(["Серия 7", "Серия 8"], 8) == 1

    def test_bare_number(self) -> None:
        assert episode_index(["1", "2", "3"], 3) == 2
