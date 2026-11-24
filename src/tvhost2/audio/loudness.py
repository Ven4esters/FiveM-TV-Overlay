"""Нормализация громкости — приведение разных источников к одному уровню.

Проблема (замеры 23.11.2026, EBU R128):

    Rutube (своя загрузка)   -39.9 LUFS
    Kodik 720p                -24.6 LUFS
    YouTube                   -20.9 LUFS
                              ─────────────
    разброс                   19.0 дБ!

Девятнадцать децибел — это разница между шёпотом и криком. Человек ставит
громкость «на 20», у Rutube еле слышно, а на Kodik — соседи стучат.

Целевой уровень: **-16 LUFS** (стандарт интернета, EBU R128 для стриминга).
Телевидение использует -23 LUFS, но там своя аккустика; для игры -16 верно.

Как нормализуем — БЕЗ перекодирования и без CPU на лету:

1. Один раз замеряем громкость по EBU R128 (`ffmpeg -af ebur128`);
2. Запоминаем усиление в дБ вместе с отпечатком ссылки;
3. Отдаём это усиление в браузер игры — там один GainNode на звук.

Цена: один замер ffmpeg (2-3 секунды на 30 секунд звука), кэшируется
на 6 часов вместе с самой ссылкой. Дальше — ноль затрат.

Почему не `loudnorm` в ffmpeg на лету: это перекодирование звука в реальном
времени, на 8 ГБ с запущенной GTA 5 — гарантированные рывки.
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
"""Цель нормализации. Интернет-стандарт для видео."""

MAX_GAIN_DB = 24.0
"""Предел усиления: тише -40 LUFS — это уже почти тишина, не вытягиваем."""

DYNAMIC_LRA = 15.0
"""Порог «звук скачет внутри себя»: тихие сцены и резкие взрывы."""

HEADROOM_DB = 1.0
"""Запас до клиппинга: не выходим на 0 dBFS."""

MIN_GAIN_DB = -24.0
"""Предел ослабления: громче +8 LUFS — это клиппинг, режем жёстко."""

SAMPLE_SECONDS = 45
"""Сколько секунд мерить. 45 хватает: типичный фильм не меняет уровень
за это время, а замер идёт 3 секунды вместо минут."""

CACHE_TTL_S = 6 * 3600
"""Сколько живёт замер. Ссылки у источников живут около 6-12 часов,
поэтому пересчитывать чаще нет смысла."""


@dataclass(frozen=True)
class Loudness:
    """Результат замера громкости."""

    lufs: float
    """Интегральная громкость по EBU R128."""

    lra: float
    """Диапазон громкости (Loudness Range). Больше 15 — звук «скачет»."""

    peak_dbfs: float
    """Пиковый уровень: если близко к 0, усиление вызовет клиппинг."""

    @property
    def gain_db(self) -> float:
        """Сколько дБ добавить, чтобы попасть в TARGET_LUFS.

        Порядок решений (важен именно такой):

        1. Что просит цель: `TARGET_LUFS - lufs`.
        2. Поднимать можно не выше запаса до пика (иначе клиппинг).
        3. Внутри общих пределов MIN/MAX.

        Найденный баг (23.11.2026): тихий звук с высоким пиком
        (YouTube: -20.9 LUFS при пике -0.1) получал отрицательное
        усиление и становился ЕЩЁ ТИШЕ. Поэтому если поднимать некуда —
        возвращаем 0, а не опускаем: опускать без причины нельзя.
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
        """То же усиление в разах — для GainNode в браузере."""
        return round(10 ** (self.gain_db / 20), 4)

    @property
    def is_dynamic(self) -> bool:
        """Звук внутри себя скачет: тихие сцены и резкие взрывы."""
        return self.lra > DYNAMIC_LRA

    def as_dict(self) -> dict[str, float | str]:
        """То, что отдаётся в интернет и в интерфейс.

        Поля названы так, как их уже читают панель и игровой плеер:
        `gainDb` — панель (`data.get("gainDb")`), `gainLinear` — плеер
        (`d.gainLinear`). Раньше этого метода не было вовсе, и `/loudness`
        отвечал 500 «expected str, bytes or os.PathLike object, not dict»:
        нормализация не работала ни разу, просто никто не нажимал кнопку
        при включённом журнале. Нашлось при включении mypy в CI.
        """
        return {
            "gainDb": self.gain_db,
            "gainLinear": self.gain_linear,
            "lufs": round(self.lufs, 1),
            "lra": round(self.lra, 1),
            "peakDbfs": round(self.peak_dbfs, 1),
            "dynamic": self.is_dynamic,
            "note": self.describe(),
        }

    def describe(self) -> str:
        """Короткое объяснение для интерфейса."""
        if abs(self.gain_db) < 1.0:
            return "громкость в норме"
        action = "громче" if self.gain_db > 0 else "тише"
        note = ", звук внутри скачет" if self.is_dynamic else ""
        return f"звук {action} на {abs(self.gain_db):.1f} дБ до нормы{note}"


_EBUR_URL = re.compile(r"I:\s+(-?[\d.]+)\s+LUFS")
_EBUR_LRA = re.compile(r"LRA:\s+(-?[\d.]+)\s+LU")
_EBUR_PEAK = re.compile(r"Peak:\s+(-?[\d.]+)\s+dBFS")


def measure(url: str, ffmpeg_exe: str, headers: dict[str, str] | None = None) -> Loudness | None:
    """Замерить громкость потока через ffmpeg (EBU R128).

    Возвращает None, если замерить не удалось — вызывающий код тогда
    просто не нормализует, а не падает.
    """
    cmd = [ffmpeg_exe, "-hide_banner", "-nostats"]
    # Заголовки выставляются ДВУМЯ разными ключами, и это не дублирование:
    #   -user_agent  — только User-Agent. Без него ffmpeg представляется
    #                  как "Lavf/…", и CDN anivids сразу отвечает 403.
    #   -headers     — всё остальное (Referer и прочее) ОДНИМ блоком.
    #                  Повторённый `-headers` ffmpeg не складывает, а
    #                  перезаписывает: из нескольких строк выживала только
    #                  последняя. Раньше терялся как раз Referer.
    if headers:
        agent = headers.get("User-Agent", "")
        if agent:
            cmd += ["-user_agent", agent]
        rest = {name: value for name, value in headers.items() if name != "User-Agent"}
        if rest:
            block = "".join(f"{name}: {value}\r\n" for name, value in rest.items())
            cmd += ["-headers", block]
    cmd += [
        "-t",
        str(SAMPLE_SECONDS),
        "-i",
        url,
        "-map",
        "0:a:0?",
        "-af",
        "ebur128=framelog=quiet",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.info("громкость: замер не удался (%s)", str(exc)[:80])
        return None

    stderr = proc.stderr
    lufs = _EBUR_URL.findall(stderr)
    if not lufs:
        log.info("громкость: ffmpeg не отдал LUFS (%s)", stderr.strip()[-120:])
        return None
    lra = _EBUR_LRA.findall(stderr)
    peak = _EBUR_PEAK.findall(stderr)
    return Loudness(
        lufs=float(lufs[-1]),
        lra=float(lra[-1]) if lra else 0.0,
        peak_dbfs=float(peak[-1]) if peak else 0.0,
    )


class LoudnessCache:
    """Кэш замеров: замер стоит 3 секунды, поэтому повторять его нельзя."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, float]] = {}
        self._load()

    def _load(self) -> None:
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("громкость: кэш не записался (%s)", exc)

    def get(self, key: str) -> Loudness | None:
        item = self._data.get(key)
        if not item:
            return None
        if time.time() - float(item.get("at", 0)) > CACHE_TTL_S:
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
        self._save()
