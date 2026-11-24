"""Полная расшифровка блока loadSource в app.js Alloha.

pool_rot.json — пул после самопрокрутки (получен в node, см. pool_node.js).
Алиасы разрешаются с учётом позиции: обфускатор объявляет function Yk(Q,y){}
в начале каждой функции поверх общего qY/a0qk.
"""
from __future__ import annotations

import json
import re
import sys

SRC = open("/tmp/pl_app.c63ca8da.js", encoding="utf-8", errors="replace").read()
POOL = json.load(open("/tmp/pool_rot.json"))

RE_FUNC = re.compile(r"function (\w+)\((\w+),(\w+)\)\{return (\w+)\(([^()]*)\);\}")


def _hex(e: str) -> str:
    return re.sub(r"0x([0-9a-fA-F]+)", lambda m: str(int(m.group(1), 16)), e)


class R:
    def __init__(self) -> None:
        self.defs: dict[str, list] = {}
        for m in RE_FUNC.finditer(SRC):
            name, a, b, target, args = m.groups()
            parts = [p.strip() for p in args.split(",")]
            if len(parts) != 2:
                continue
            self.defs.setdefault(name, []).append((m.start(), a, b, target, *parts))
        m = re.search(r"function a0y\((\w+),(\w+)\)\{\1=\1-\(([^)]*)\)", SRC)
        self.base = int(self.ev(m.group(3), {}))
        self.defs = {k: sorted(v, key=lambda t: t[0]) for k, v in self.defs.items()}

    @staticmethod
    def ev(expr: str, env: dict[str, float]) -> float:
        e = _hex(expr.replace("--", "+").replace("+-", "-"))
        for k, v in env.items():
            e = re.sub(r"\b%s\b" % re.escape(k), str(v), e)
        if re.search(r"[A-Za-z_$]", e):
            raise ValueError(expr)
        return eval(e)  # noqa: S307

    def idx(self, fn: str, a: float, b: float, at: int) -> int:
        cur, (c1, c2) = fn, (a, b)
        for _ in range(10):
            cand = [d for d in self.defs.get(cur, []) if d[0] < at]
            if not cand:
                raise KeyError(cur)
            _, pa, pb, target, e1, e2 = cand[-1]
            n1, n2 = self.ev(e1, {pa: c1, pb: c2}), self.ev(e2, {pa: c1, pb: c2})
            if target == "a0y":
                return int(round(n1 - self.base))
            cur, c1, c2 = target, n1, n2
        raise AssertionError

    def s(self, fn: str, a: float, b: float, at: int) -> str:
        return POOL[self.idx(fn, a, b, at)]


def num(t: str) -> float:
    t = t.strip()
    return float(int(t, 16)) if "0x" in t.lower() else float(int(t))


def decode_span(r: R, text: str, at: int, quiet: bool = False) -> str:
    """Заменяем все F(<num>,<num>) на строки из пула."""
    def repl(m: re.Match) -> str:
        fn, a, b = m.group(1), m.group(2), m.group(3)
        try:
            s = r.s(fn, num(a), num(b), at)
        except Exception:  # noqa: BLE001
            return m.group(0)
        return json.dumps(s, ensure_ascii=False)
    out = re.sub(r"\b(\w+)\((-?0x[0-9a-fA-F]+|-?\d+),(-?0x[0-9a-fA-F]+|-?\d+)\)", repl, text)
    # hex-константы → десятичные
    out = re.sub(r"(?<![\w'\"])0x([0-9a-fA-F]+)", lambda m: str(int(m.group(1), 16)), out)
    return out


if __name__ == "__main__":
    r = R()
    print("// функций-алиасов:", len(r.defs), " база:", hex(r.base), flush=True)
    i = SRC.find("Q5()[Yl(0xc92,0xc7d)]")
    span_start = i - 3000
    span_end = i + 2500
    text = SRC[span_start:span_end]
    out = decode_span(r, text, i)
    sys.stdout.write(out.replace(",", ",\n") if "--pretty" in sys.argv else out)
