"""Нормализация громкости в v1-приложении (EBU R128 → -16 LUFS).

Числа — живые замеры 23.11.2026:
    Rutube -39.9 LUFS (пик -13.9), Kodik -24.6 (-11.1), YouTube -20.9 (-0.1).
Разброс 19 дБ — причина жалобы «на маленькой громкости звучит громко».
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tvhost.infra import loudness
from tvhost.infra.loudness import (
    MAX_GAIN_DB,
    TARGET_LUFS,
    Loudness,
    LoudnessCache,
)

MEASURED = [
    ("Rutube", -39.9, 9.8, -13.9),
    ("Kodik", -24.6, 13.5, -11.1),
    ("YouTube", -20.9, 0.0, -0.1),
]


class TestGain:
    @pytest.mark.parametrize(("name", "lufs", "lra", "peak"), MEASURED)
    def test_moves_towards_target(self, name, lufs, lra, peak) -> None:
        item = Loudness(lufs=lufs, lra=lra, peak_dbfs=peak)
        before = abs(lufs - TARGET_LUFS)
        after = abs(lufs + item.gain_db - TARGET_LUFS)
        assert after <= before, f"{name}: стало хуже"

    def test_quiet_is_boosted(self) -> None:
        assert Loudness(lufs=-39.9, lra=9.8, peak_dbfs=-13.9).gain_db > 5

    def test_loud_is_reduced(self) -> None:
        assert Loudness(lufs=-8.0, lra=20.0, peak_dbfs=-0.1).gain_db < 0

    def test_normal_untouched(self) -> None:
        assert Loudness(lufs=-16.0, lra=8.0, peak_dbfs=-3.0).gain_db == 0.0

    def test_peak_blocks_boost(self) -> None:
        """Пик на -0.5: поднимать некуда."""
        assert Loudness(lufs=-39.9, lra=9.8, peak_dbfs=-0.5).gain_db <= 0.5

    def test_quiet_sound_with_high_peak_is_not_made_quieter(self) -> None:
        """ГЛАВНЫЙ баг: тихий звук с высоким пиком раньше становился тише.

        YouTube -20.9 LUFS при пике -0.1 получал -0.9 дБ и уходил от цели.
        Правило: некуда поднимать — не трогать.
        """
        assert Loudness(lufs=-20.9, lra=0.0, peak_dbfs=-0.1).gain_db == 0.0

    def test_max_gain_cap(self) -> None:
        assert Loudness(lufs=-70.0, lra=5.0, peak_dbfs=-60.0).gain_db <= MAX_GAIN_DB


class TestLinear:
    def test_unity(self) -> None:
        assert Loudness(lufs=-16.0, lra=8.0, peak_dbfs=-3.0).gain_linear == 1.0

    def test_six_db_is_double(self) -> None:
        """Формула для GainNode: +6 дБ = x2."""
        item = Loudness(lufs=-22.0, lra=8.0, peak_dbfs=-10.0)
        assert item.gain_db == pytest.approx(6.0, abs=0.1)
        assert item.gain_linear == pytest.approx(2.0, abs=0.02)

    def test_kodik_real_value(self) -> None:
        """Живой замер Kodik: +8.6 дБ = x2.69 — ровно как в браузере."""
        item = Loudness(lufs=-24.6, lra=13.5, peak_dbfs=-11.1)
        assert item.gain_db == pytest.approx(8.6, abs=0.1)
        assert item.gain_linear == pytest.approx(2.69, abs=0.02)


class TestDict:
    def test_has_all_fields(self) -> None:
        d = Loudness(lufs=-24.6, lra=13.5, peak_dbfs=-11.1).as_dict()
        for key in ("lufs", "gainDb", "gainLinear", "target", "note"):
            assert key in d

    def test_target_in_dict(self) -> None:
        assert Loudness(lufs=-20.0, lra=5.0, peak_dbfs=-2.0).as_dict()["target"] == -16.0


class TestSpread:
    def test_spread_was_huge(self) -> None:
        values = [v[1] for v in MEASURED]
        assert max(values) - min(values) > 15

    def test_spread_narrows(self) -> None:
        before = [v[1] for v in MEASURED]
        after = [
            lufs + Loudness(lufs=lufs, lra=lra, peak_dbfs=pk).gain_db
            for _, lufs, lra, pk in MEASURED
        ]
        assert (max(after) - min(after)) < (max(before) - min(before)) * 0.75


class TestCache:
    def test_roundtrip(self, tmp_path: Path) -> None:
        c = LoudnessCache(tmp_path / "l.json")
        c.put("k", Loudness(lufs=-24.6, lra=13.5, peak_dbfs=-11.1))
        got = c.get("k")
        assert got is not None and got.lufs == -24.6

    def test_missing(self, tmp_path: Path) -> None:
        assert LoudnessCache(tmp_path / "x.json").get("нет") is None

    def test_expires(self, tmp_path: Path) -> None:
        p = tmp_path / "l.json"
        p.write_text(
            json.dumps({"k": {"lufs": -20, "lra": 1, "peak": -2, "at": time.time() - 7 * 3600}}),
            encoding="utf-8",
        )
        assert LoudnessCache(p).get("k") is None

    def test_broken_file(self, tmp_path: Path) -> None:
        p = tmp_path / "l.json"
        p.write_text("не json", encoding="utf-8")
        assert LoudnessCache(p).get("k") is None


class TestMeasuredLive:
    """Живой замер через ffmpeg — пропускается, если ffmpeg недоступен."""

    @pytest.mark.skipif(loudness._ffmpeg() is None, reason="нет ffmpeg")
    def test_kodik_is_quieter_than_target(self) -> None:
        """Kodik реально тише цели: усиление должно быть положительным."""
        item = Loudness(lufs=-24.6, lra=13.5, peak_dbfs=-11.1)
        assert item.gain_db > 0
