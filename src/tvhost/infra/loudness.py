"""Громкость: замер по EBU R128 и выравнивание между источниками.

Замеры на живых потоках 23.11.2026:

    Rutube (своя загрузка)   -39.9 LUFS   пик -13.9 dBFS
    Kodik 720p                -24.6 LUFS   пик -11.1 dBFS
    YouTube                   -20.9 LUFS   пик  -0.1 dBFS
                              ─────────────
    разброс                   19.0 дБ

Девятнадцать децибел — это шёпот против крика. Ползунок громкости в игре
один, а источники разные: на Rutube не слышно, на Kodik соседи стучат.
Отсюда жалоба «на маленькой громкости звучит громко».

Решение: приводим всё к -16 LUFS (интернет-стандарт) БЕЗ перекодирования.
Один замер ffmpeg, результат уходит в браузер игры как GainNode.
На 8 ГБ ОЗУ рядом с запущенной GTA 5 это принципиально: loudnorm на лету
дал бы рывки, а усиление готового звука их не даёт вовсе.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TARGET_LUFS = -16.0
"""Цель. EBU R128 для интернета; телевидение использует -23 LUFS."""

MAX_GAIN_DB = 24.0
"""Предел усиления: тише -40 LUFS — почти тишина, шум вылезет наружу."""

MIN_GAIN_DB = -24.0
"""Предел ослабления: громче -8 LUFS — уже клиппинг, режем жёстко."""

HEADROOM_DB = 1.0
"""Запас до клиппинга: не поднимаем выше -1 dBFS."""

DYNAMIC_LRA = 15.0
"""Порог «звук скачет внутри самого видео»."""

SAMPLE_SECONDS = 45
"""Длина замера. 45 секунд хватает, а идёт он около 3 секунд."""

CACHE_TTL_S = 6 * 3600
"""Столько живут ссылки у источников — дольше хранить замер бессмысленно."""


@dataclass(frozen=True)
class Loudness:
    """Результат замера громкости."""

    lufs: float
    lra: float
    peak_dbfs: float

    @property
    def gain_db(self) -> float:
        """Сколько дБ добавить, чтобы попасть в TARGET_LUFS.

        Порядок решений важен:

        1. что просит цель;
        2. поднимать можно не выше запаса до пика — иначе клиппинг;
        3. внутри общих пределов MIN/MAX.

        Найденный баг: тихий звук с высоким пиком (YouTube -20.9 LUFS при
        пике -0.1) получал отрицательное усиление и становился ЕЩЁ ТИШЕ.
        Поэтому если поднимать некуда — возвращаем 0, а не опускаем.
        """
        wanted = TARGET_LUFS - self.lufs
        if wanted > 0 and self.peak_dbfs < 0:
            available = -HEADROOM_DB - self.peak_dbfs
            if available <= 0:
                return 0.0  # места нет — не трогаем
            wanted = min(wanted, available)
        return round(max(MIN_GAIN_DB, min(MAX_GAIN_DB, wanted)), 1)

    @property
    def gain_linear(self) -> float:
        """Усиление в разах — его и получает GainNode в браузере."""
        return round(10 ** (self.gain_db / 20), 4)

    @property
    def is_dynamic(self) -> bool:
        return self.lra > DYNAMIC_LRA

    def describe(self) -> str:
        if abs(self.gain_db) < 1.0:
            return "громкость в норме"
        action = "громче" if self.gain_db > 0 else "тише"
        note = ", звук внутри скачет" if self.is_dynamic else ""
        return f"звук {action} на {abs(self.gain_db):.1f} дБ до нормы{note}"

    def as_dict(self) -> dict:
        return {
            "lufs": self.lufs,
            "lra": self.lra,
            "peakDbfs": self.peak_dbfs,
            "gainDb": self.gain_db,
            "gainLinear": self.gain_linear,
            "dynamic": self.is_dynamic,
            "target": TARGET_LUFS,
            "note": self.describe(),
        }


_LUFS_RE = re.compile(r"I:\s+(-?[\d.]+)\s+LUFS")
_LRA_RE = re.compile(r"LRA:\s+(-?[\d.]+)\s+LU")
_PEAK_RE = re.compile(r"Peak level dB:\s+(-?[\d.]+)")


def _ffmpeg() -> str | None:
    """Путь к ffmpeg. Без него нормализация просто не работает."""
    try:
        from ..infra import ffmpeg_bin

        return ffmpeg_bin.find_ffmpeg()
    except Exception:
        return None


def measure(url: str, headers: dict[str, str] | None = None) -> Loudness | None:
    """Замерить громкость потока по EBU R128.

    Возвращает None, если не удалось: вызывающий код тогда не нормализует,
    а не падает. Замер идёт по первым SAMPLE_SECONDS секундам.
    """
    exe = _ffmpeg()
    if not exe:
        log.info("громкость: ffmpeg не найден")
        return None

    cmd = [exe, "-hide_banner", "-nostats"]
    for name, value in (headers or {}).items():
        cmd += ["-headers", f"{name}: {value}\r\n"]
    cmd += [
        "-t",
        str(SAMPLE_SECONDS),
        "-i",
        url,
        "-map",
        "0:a:0?",
        # ebur128 даёт LUFS/LRA, astats — точный пик
        "-af",
        "ebur128=framelog=quiet,astats=metadata=1:reset=0",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.info("громкость: замер не удался (%s)", str(exc)[:80])
        return None

    lufs = _LUFS_RE.findall(proc.stderr)
    if not lufs:
        log.info("громкость: ffmpeg не отдал LUFS")
        return None
    peaks = _PEAK_RE.findall(proc.stderr)
    lra = _LRA_RE.findall(proc.stderr)
    return Loudness(
        lufs=float(lufs[-1]),
        lra=float(lra[-1]) if lra else 0.0,
        peak_dbfs=float(peaks[-1]) if peaks else 0.0,
    )


class LoudnessCache:
    """Кэш замеров: замер стоит ~3 секунды, повторять его нельзя."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict] = {}
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def get(self, key: str) -> Loudness | None:
        item = self._data.get(key)
        if not item or time.time() - float(item.get("at", 0)) > CACHE_TTL_S:
            return None
        return Loudness(
            lufs=float(item["lufs"]),
            lra=float(item.get("lra", 0)),
            peak_dbfs=float(item.get("peak", 0)),
        )

    def put(self, key: str, value: Loudness) -> None:
        self._data[key] = {
            "lufs": value.lufs,
            "lra": value.lra,
            "peak": value.peak_dbfs,
            "at": time.time(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("громкость: кэш не записался (%s)", exc)
