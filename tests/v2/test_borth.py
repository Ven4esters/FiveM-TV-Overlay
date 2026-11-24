"""Borth Alloha: порт iS/iR/iP сверяется с настоящими функциями из app.js.

Фикстура `fixtures/borth_vectors.json` снята не «на глаз»: в node загружались
САМИ функции `iS`, `iR`, `iP` из расшифрованного `build/app.c63ca8da.js` и
прогонялись на наборе входов (мета со страницы заказчика, короткие строки,
случайные 88 и 120 символов). Наш питон обязан давать ровно те же строки.

Зачем так строго: первая версия порта проходила «на вид» (длина та же,
символы те же), но переставляла НЕ ТАК — перепутаны признак группировки в
iR и шаг в iP. Такое не ловит ни один осмысленный smoke-тест, только сверка
с эталоном.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from tvhost2.sources.alloha_borth import MIN_LEN, borth_suffix, i_p, i_r, i_s

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "borth_vectors.json").read_text("utf-8"))


class TestAgainstRealFunctions:
    @pytest.mark.parametrize("row", VECTORS, ids=lambda r: f"len{len(r['input'])}")
    def test_i_s(self, row: dict) -> None:
        assert i_s(row["input"]) == row["i_s"]

    @pytest.mark.parametrize("row", VECTORS, ids=lambda r: f"len{len(r['input'])}")
    def test_i_r(self, row: dict) -> None:
        assert i_r(row["i_s"]) == row["i_r"]

    @pytest.mark.parametrize("row", VECTORS, ids=lambda r: f"len{len(r['input'])}")
    def test_i_p(self, row: dict) -> None:
        assert i_p(row["i_r"]) == row["i_p"]

    def test_short_strings_untouched(self) -> None:
        """`hC<=1` — строка возвращается как есть во всех трёх функциях."""
        assert MIN_LEN == 1
        for s in ("", "a"):
            assert i_s(s) == s
            assert i_r(s) == s
            assert i_p(s) == s


class TestBorthSuffix:
    def test_suffix_of_order_page_reveals_expiry(self) -> None:
        """Суффикс — base64url, внутри `id|unix_time|подпись`.

        Это не догадка: 88-символьная мета со страницы заказчика даёт после
        трёх перестановок строку, которая декодируется в читаемый вид, а
        число внутри — 1795523144 (24.11.2026 12:25:44 UTC). Значит сервер
        кладёт в мету подписанный срок жизни, а не случайный мусор.
        """
        meta = VECTORS[0]["input"]
        assert len(meta) == 88
        suffix = borth_suffix(meta)
        raw = base64.urlsafe_b64decode(suffix + "=" * (-len(suffix) % 4))
        text = raw.decode("ascii")
        parts = text.split("|")
        assert len(parts) == 3, f"не три поля: {text!r}"
        assert parts[1].isdigit(), f"второе поле не время: {parts[1]!r}"
        assert 1_700_000_000 < int(parts[1]) < 2_000_000_000

    def test_permutation_keeps_characters(self) -> None:
        """Перестановка не меняет состав строки — только порядок."""
        for row in VECTORS:
            assert sorted(borth_suffix(row["input"])) == sorted(row["input"])
