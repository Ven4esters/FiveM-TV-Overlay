"""Питоновский порт iS/iR/iP из расшифрованного app.js Alloha.

Зачем: пробник на компьютере заказчика должен обходиться без node, а держать
две реализации без сверки нельзя — моя первая версия молча давала другой
результат (перепутаны константы и признак группировки). Правильность держит
тест `tests/v2/test_borth.py`: он сверяет порт с выводом НАСТОЯЩИХ функций
из `app.js`, снятым node и сохранённым фикстурой.

Как читать оригинал: все числа внутри функций — это арифметика вида
`-1207+8600+-66*112`, значения проставлены рядом в комментариях.

Три функции по очереди:

    iS  — раскладывает строку по группам, где номер группы = ДЛИНА двоичной
          записи индекса (0→0, 1→1, 2..3→2, 4..7→3, …), старшая группа
          идёт первой;
    iR  — то же, но номер группы = ЧИСЛО НУЛЕВЫХ МЛАДШИХ БИТ индекса
          (0→старшая группа, 1→0, 2→1, 4→2, …), группы идут по возрастанию;
    iP  — перестановка: позиции обходятся шагом 2 по модулю простого числа
          ≥ n+1, берутся только попавшие в строку.
"""

from __future__ import annotations

MIN_LEN = 1
"""Во всех трёх функциях `hC<=1` — строка возвращается как есть."""

STEP = 2
"""Шаг обхода позиций в iP: `hF=(hF+2)%hM` (2440+1061*-7-1663*-3)."""


def _bit_len(x: int) -> int:
    """Длина двоичной записи: 0→0, 1→1, 2..3→2, 4..7→3."""
    return x.bit_length()


def _ctz(x: int, top: int) -> int:
    """Число нулевых младших бит; у нуля — старшая группа `top`.

    1→0, 2→1, 3→0, 4→2, 6→1; 0→top.
    """
    if x == 0:
        return top
    n = 0
    while not (x & 1):
        n += 1
        x >>= 1
    return n


def _top(n: int) -> int:
    """`hI`: минимальное число, при котором 1<<hI >= n."""
    top = 1
    while (1 << top) < n:
        top += 1
    return top


def i_s(s: str) -> str:
    """Группировка по длине двоичной записи, старшая группа первой."""
    n = len(s)
    if n <= MIN_LEN:
        return s
    top = _top(n)
    groups = [0] * (top + 1)
    for i in range(n):
        groups[_bit_len(i)] += 1
    buckets: list[str] = [""] * (top + 1)
    pos = 0
    for g in range(top, -1, -1):
        buckets[g] = s[pos : pos + groups[g]]
        pos += groups[g]
    used = [0] * (top + 1)
    out: list[str] = [""] * n
    for i in range(n):
        g = _bit_len(i)
        out[i] = buckets[g][used[g]]
        used[g] += 1
    return "".join(out)


def i_r(s: str) -> str:
    """Группировка по числу нулевых младших бит, группы по возрастанию."""
    n = len(s)
    if n <= MIN_LEN:
        return s
    top = _top(n)
    groups = [0] * (top + 1)
    for i in range(n):
        groups[_ctz(i, top)] += 1
    buckets: list[str] = [""] * (top + 1)
    pos = 0
    for g in range(top + 1):
        buckets[g] = s[pos : pos + groups[g]]
        pos += groups[g]
    used = [0] * (top + 1)
    out: list[str] = [""] * n
    for i in range(n):
        g = _ctz(i, top)
        out[i] = buckets[g][used[g]]
        used[g] += 1
    return "".join(out)


def _is_prime(x: int) -> bool:
    """Простое ли число. Нужно для модуля перестановки в iP."""
    if x < 2:  # noqa: PLR2004 — 1 не простое, порог очевиден
        return False
    if x % 2 == 0:
        return x == 2  # noqa: PLR2004 — единственное чётное простое
    d = 3
    while d * d <= x:
        if x % d == 0:
            return False
        d += 2
    return True


def i_p(s: str) -> str:
    """Перестановка шагом 2 по модулю простого >= n+1."""
    n = len(s)
    if n <= MIN_LEN:
        return s
    m = n + 1
    while not _is_prime(m):
        m += 1
    used = [False] * n
    order: list[int] = []
    f = 0
    while len(order) < n:
        f = (f + STEP) % m
        if f < n and not used[f]:
            order.append(f)
            used[f] = True
    out = [""] * n
    for k, pos in enumerate(order):
        out[pos] = s[k]
    return "".join(out)


def borth_suffix(meta: str) -> str:
    """`iP(iR(iS(meta)))` — вторая половина заголовка Borth."""
    return i_p(i_r(i_s(meta)))
