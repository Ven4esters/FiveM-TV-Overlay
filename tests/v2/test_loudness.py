"""Нормализация громкости (EBU R128 → -16 LUFS).

Все числа — из живых замеров 23.11.2026 на четырёх источниках.
Проблема заказчика: «даже на маленькой громкости звучит громко» —
потому что разброс между источниками 19 дБ.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tvhost2.audio import (
    MAX_GAIN_DB,
    MIN_GAIN_DB,
    TARGET_LUFS,
    Loudness,
    LoudnessCache,
)

# Реальные замеры: (имя, LUFS, LRA, пиковый уровень dBFS)
MEASURED = [
    ("Rutube (своя загрузка)", -39.9, 9.8, -13.9),
    ("Kodik 720p", -24.6, 13.5, -11.1),
    ("YouTube", -20.9, 0.0, -0.1),
]


class TestTarget:
    def test_target_is_internet_standard(self) -> None:
        assert TARGET_LUFS == -16.0

    def test_limits_are_sane(self) -> None:
        assert MAX_GAIN_DB > 0
        assert MIN_GAIN_DB < 0


class TestGain:
    @pytest.mark.parametrize(("name", "lufs", "lra", "peak"), MEASURED)
    def test_gain_moves_towards_target(
        self, name: str, lufs: float, lra: float, peak: float
    ) -> None:
        """Усиление всегда приближает к цели, а не уводит от неё."""
        item = Loudness(lufs=lufs, lra=lra, peak_dbfs=peak)
        after = lufs + item.gain_db
        assert abs(TARGET_LUFS - after) <= abs(TARGET_LUFS - lufs), name

    def test_quiet_source_is_boosted(self) -> None:
        """Rutube с -39.9 LUFS должен стать громче."""
        item = Loudness(lufs=-39.9, lra=9.8, peak_dbfs=-13.9)
        assert item.gain_db > 5

    def test_loud_source_is_reduced(self) -> None:
        """Клип с -8 LUFS нужно сделать тише."""
        item = Loudness(lufs=-8.0, lra=20.0, peak_dbfs=-0.1)
        assert item.gain_db < 0

    def test_normal_source_is_untouched(self) -> None:
        item = Loudness(lufs=-16.0, lra=8.0, peak_dbfs=-3.0)
        assert item.gain_db == 0.0

    def test_peak_limits_boost(self) -> None:
        """Пик уже на -0.5: поднимать некуда, будет клиппинг."""
        item = Loudness(lufs=-39.9, lra=9.8, peak_dbfs=-0.5)
        assert item.gain_db <= 0.5

    def test_quiet_peak_allows_more(self) -> None:
        """Тот же тихий звук, но пик -20: места хватает."""
        item = Loudness(lufs=-39.9, lra=9.8, peak_dbfs=-20.0)
        assert item.gain_db > 5

    def test_max_gain_respected(self) -> None:
        """Почти тишина: не вытягиваем бесконечно, шум вылезет."""
        item = Loudness(lufs=-70.0, lra=5.0, peak_dbfs=-60.0)
        assert item.gain_db <= MAX_GAIN_DB

    def test_min_gain_respected(self) -> None:
        """Очень громкий источник: не убиваем звук в ноль."""
        item = Loudness(lufs=5.0, lra=5.0, peak_dbfs=-0.1)
        assert item.gain_db >= MIN_GAIN_DB


class TestLinearGain:
    def test_zero_db_is_one(self) -> None:
        item = Loudness(lufs=-16.0, lra=8.0, peak_dbfs=-3.0)
        assert item.gain_linear == 1.0

    def test_plus_six_db_is_double(self) -> None:
        """+6 дБ ≈ x2 по амплитуде — проверка формулы для GainNode."""
        item = Loudness(lufs=-22.0, lra=8.0, peak_dbfs=-10.0)
        assert item.gain_db == pytest.approx(6.0, abs=0.1)
        assert item.gain_linear == pytest.approx(2.0, abs=0.02)


class TestDynamic:
    def test_wide_range_flagged(self) -> None:
        """LRA 20 — звук скачет внутри видео (тихие сцены, взрывы)."""
        assert Loudness(lufs=-20.0, lra=20.0, peak_dbfs=-1.0).is_dynamic

    def test_narrow_range_not_flagged(self) -> None:
        assert not Loudness(lufs=-20.0, lra=8.0, peak_dbfs=-1.0).is_dynamic


class TestDescribe:
    def test_normal(self) -> None:
        assert "в норме" in Loudness(lufs=-16.0, lra=8.0, peak_dbfs=-3.0).describe()

    def test_boost_mentions_db(self) -> None:
        text = Loudness(lufs=-24.6, lra=13.5, peak_dbfs=-11.1).describe()
        assert "громче" in text and "дБ" in text

    def test_dynamic_warning_added(self) -> None:
        text = Loudness(lufs=-24.6, lra=20.0, peak_dbfs=-11.1).describe()
        assert "скачет" in text


class TestSpread:
    def test_sources_were_uneven(self) -> None:
        """Главная проверка: разброс между источниками был огромным.

        Если этот тест когда-то начнёт падать — значит источники
        выровнялись, и нормализация стала не нужна. Это хорошая новость,
        но тогда надо пересмотреть архитектуру.
        """
        values = [lufs for _, lufs, _, _ in MEASURED]
        spread = max(values) - min(values)
        assert spread > 15, "разброс между источниками должен был быть большим"

    @pytest.mark.parametrize(("name", "lufs", "lra", "peak"), MEASURED)
    def test_after_normalisation_is_closer_to_target(
        self, name: str, lufs: float, lra: float, peak: float
    ) -> None:
        """После нормализации каждый источник ближе к цели, чем был.

        Не «ровно -16»: у Rutube с пиком -13.9 места поднять всего 12.9 дБ,
        и остаётся -27 LUFS. Это честный предел — лучше тише, чем
        с искажениями. Важно, что разрыв сокращается, а не растёт.
        """
        item = Loudness(lufs=lufs, lra=lra, peak_dbfs=peak)
        before = abs(lufs - TARGET_LUFS)
        after = abs(lufs + item.gain_db - TARGET_LUFS)
        assert after <= before, f"{name}: стало хуже ({before:.1f} → {after:.1f} дБ)"

    def test_spread_narrows_after_normalisation(self) -> None:
        """Главное требование заказчика: разброс между источниками падает.

        До: 19.0 дБ. После: считаем с учётом предела по пику.
        """
        before = [lufs for _, lufs, _, _ in MEASURED]
        after = []
        for _, lufs, lra, peak in MEASURED:
            item = Loudness(lufs=lufs, lra=lra, peak_dbfs=peak)
            after.append(lufs + item.gain_db)
        spread_before = max(before) - min(before)
        spread_after = max(after) - min(after)
        assert spread_after < spread_before * 0.75, (
            f"разброс должен заметно упасть: {spread_before:.1f} → {spread_after:.1f} дБ"
        )


class TestCache:
    def test_put_and_get(self, tmp_path: Path) -> None:
        cache = LoudnessCache(tmp_path / "loudness.json")
        value = Loudness(lufs=-24.6, lra=13.5, peak_dbfs=-11.1)
        cache.put("kodik:720", value)
        got = cache.get("kodik:720")
        assert got is not None
        assert got.lufs == -24.6

    def test_missing_returns_none(self, tmp_path: Path) -> None:
        assert LoudnessCache(tmp_path / "x.json").get("нет") is None

    def test_survives_reload(self, tmp_path: Path) -> None:
        """Кэш живёт между запусками — иначе замер каждый раз."""
        path = tmp_path / "loudness.json"
        LoudnessCache(path).put("videо", Loudness(lufs=-20.0, lra=5.0, peak_dbfs=-2.0))
        assert LoudnessCache(path).get("videо") is not None

    def test_expires(self, tmp_path: Path) -> None:
        """Ссылки у источников живут часы — старый замер бесполезен."""
        path = tmp_path / "loudness.json"
        old = time.time() - 7 * 3600
        path.write_text(
            json.dumps({"x": {"lufs": -20.0, "lra": 5.0, "peak": -2.0, "at": old}}),
            encoding="utf-8",
        )
        assert LoudnessCache(path).get("x") is None

    def test_broken_file_is_ignored(self, tmp_path: Path) -> None:
        """Битый кэш не должен ронять приложение."""
        path = tmp_path / "loudness.json"
        path.write_text("не json", encoding="utf-8")
        assert LoudnessCache(path).get("x") is None
