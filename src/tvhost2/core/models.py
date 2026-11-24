"""Модель данных — единственный язык, на котором говорят все модули.

Зачем отдельный модуль: в v1 источники отдавали словари с разными ключами
(`quality_files`, `variants`, `bitrates`), и прокси разбирал это через
if/else. Здесь структура одна, и опечатка в имени поля — ошибка на этапе
написания кода, а не в рантайме.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StreamKind(str, Enum):
    """Как именно отдаётся поток — от этого зависит маршрут прокси."""

    HLS = "hls"
    """Один master-плейлист, внутри варианты качеств."""

    HLS_SPLIT = "hls_split"
    """Видео и звук отдельными плейлистами, звук НЕ в HLS (не хватает 233/234).

    Проверено экспериментом 23.11.2026: hls.js играет сырой AAC-сегмент
    в аудиогруппе `EXT-X-MEDIA` — поэтому для YouTube штатный режим HLS,
    а HLS_SPLIT остаётся аварийным (когда аудио только прямым файлом)
    и требует склейки."""

    MP4 = "mp4"
    """Прямой файл. Playwright-путь, blob-URL."""

    PAGE = "page"
    """HTML-страница с плеером; поток достаёт браузерная сессия (Kodik)."""


class SourceState(str, Enum):
    """Честное состояние источника — показывается во вкладке «Источники»."""

    OK = "ok"
    """Проверен, работает."""

    PARTIAL = "partial"
    """Работает с оговоркой: cookies, гео, только часть видео."""

    NEEDS_COOKIES = "needs_cookies"
    """Без cookies браузера не отдаст поток."""

    BROKEN = "broken"
    """Сломался — сайт поменял защиту. Ждёт ссылку от заказчика."""

    BROWSER_ONLY = "browser_only"
    """Только через браузерную сессию (медленно, но работает)."""


@dataclass(frozen=True, slots=True)
class Quality:
    """Одна дорожка качества.

    height и width раздельно — потому что вертикальные видео существуют
    (ERR-062): у них 1080x1920, и `height=1080` без ширины даёт квадрат.

    bitrate_kbps — РЕАЛЬНЫЙ (из tbr), не выдуманный. ERR-067: завышение
    на 47% заставляло ABR навсегда остаться на 144p.
    """

    height: int
    width: int
    bitrate_kbps: int
    url: str
    nominal_height: int = 0
    """«Паспортное» качество, как его называет сайт.

    VK пишет `url1080`, а кадр отдаёт 1920x960 (фильм 2:1 с полосами).
    Показывать «960p» — путать человека: он искал 1080p. Здесь 1080,
    а в height — реальные 960, которые нужны для RESOLUTION в плеере.
    Если 0 — берём height.
    """

    def __post_init__(self) -> None:
        if self.height <= 0:
            raise ValueError(f"height должен быть > 0, получено {self.height}")
        if self.width <= 0:
            raise ValueError(f"width должен быть > 0, получено {self.width}")
        if self.bitrate_kbps <= 0:
            raise ValueError(
                f"bitrate_kbps должен быть реальным и > 0, получено {self.bitrate_kbps}"
            )
        if not self.url:
            raise ValueError("url не может быть пустым")

    @property
    def is_portrait(self) -> bool:
        """Вертикальное видео (сторис, шортсы)."""
        return self.height > self.width

    @property
    def label_height(self) -> int:
        """Высота для подписи: паспортная, если сайт её назвал."""
        return self.nominal_height or self.height

    @property
    def label(self) -> str:
        """Подпись для выпадающего списка качества.

        У вертикального видео качество меряется по ШИРИНЕ: клип 1080x1920
        это «1080p», а не «1920p» — иначе он в списке стоит выше 4K.
        """
        if self.is_portrait:
            side = self.nominal_height or self.width
            return f"{side}x{self.height} (верт.)"
        return f"{self.label_height}p"

    @property
    def is_letterboxed(self) -> bool:
        """Кадр шире 16:9 — фильм с чёрными полосами сверху и снизу."""
        return self.width * 9 > self.height * 16


@dataclass(frozen=True, slots=True)
class Stream:
    """Готовый к показу поток: одно видео, все его качества."""

    source: str
    title: str
    kind: StreamKind
    qualities: tuple[Quality, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    duration_s: int = 0
    is_live: bool = False
    is_direct_file: bool = False
    """Качества — прямые файлы (mp4), а не HLS-плейлисты.

    Нужно плееру: hls.js не умеет mp4, и если скормить ему master, где
    варианты — mp4-файлы, он молча не проигрывает ничего. При этом признаке
    плеер переключает качество сам, меняя `video.src`.

    Откуда взялось: у anidub.org (новый плеер cdnvideohub) HLS-мастер отдаёт
    варианты ссылками вида `/expires/…/type/2/sig/…` — без хоста, и наш мост
    превращает их в мусор. Прямые mp4 там работают, а HLS — нет.
    """

    audio_tracks: tuple[dict[str, Any], ...] = ()
    """Список озвучек (Kodik, кинотеатры): [{"id","title","hash","type"}].

    У Kodik каждая озвучка — отдельный поток со своим hash, поэтому
    переключение озвучки = повторный разбор, а не смена дорожки в плеере.
    Пустой кортеж — у источника одна дорожка.
    """

    audio_url: str = ""
    """Отдельная аудио-дорожка. Для HLS — URI аудиогруппы master-плейлиста.

    Проверено: hls.js проигрывает её, даже если сегменты — сырой AAC
    (itag 233 у YouTube). Поэтому это штатный путь, а не аварийный.
    """

    subtitles: tuple[dict[str, Any], ...] = ()
    """Дорожки субтитров: [{"lang": "ru", "title": "Русские", "url": "…"}].

    Обычно это WebVTT — браузер игры понимает его нативно через `<track>`.
    У YouTube субтитры бывают и в других форматах, поэтому источник обязан
    отдать ссылку на VTT: конвертацию делает он, а не плеер.
    """

    episodes: tuple[dict[str, Any], ...] = ()
    """Серии, которые источник показал: `[{"season","episode","title"}]`.

    ОБЩИЙ контракт, а не kodik-овский: так сериал выглядит одинаково, откуда
    бы он ни пришёл. Источник заполняет его, только если серии реально есть —
    у Rutube, Twitch, YouTube и у фильмов список пустой, и пульт тогда не
    рисует переключатель вовсе.

    Что означает пустой список, важно различать:

      * источник УМЕЕТ серии, но их нет в этом видео (фильм) — `episodes_of`
        всё равно вернёт способность, и кнопки будут выключены с пояснением;
      * источник серии не умеет вовсе — переключателя нет и вопроса нет.
    """

    episode: int = 0
    """Какая серия сейчас играет, НОМЕР с единицы. 0 — не сериал."""

    episode_season: int = 0
    """Сезон этой серии. 0 — источник сезонов не различает."""

    can_switch_episode: bool = False
    """Может ли приложение переключить серию само, не открывая сайт.

    True — у источника серия берётся из уже полученного списка, и это
    стоит одного повторного запроса (Kodik, anidub.org). False — источник
    такого не умеет, и пульт честно скажет, что серию надо открыть на сайте.
    """

    state: SourceState = SourceState.OK

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("source не может быть пустым")

    @property
    def best(self) -> Quality | None:
        """Лучшее качество. None — если качеств ещё нет (PAGE-поток)."""
        return self.qualities[0] if self.qualities else None

    @property
    def has_episodes(self) -> bool:
        """Источник отдал список серий и по нему можно переключаться."""
        return self.can_switch_episode and bool(self.episodes)

    @property
    def worst(self) -> Quality | None:
        """Худшее качество — старт для слабого интернета."""
        return self.qualities[-1] if self.qualities else None

    def by_height(self, height: int) -> Quality | None:
        """Найти качество по высоте. Точное совпадение, иначе ближайшее ниже."""
        exact = [q for q in self.qualities if q.height == height]
        if exact:
            return exact[0]
        lower = [q for q in self.qualities if q.height <= height]
        if lower:
            return max(lower, key=lambda q: q.height)
        return self.worst

    def sorted_qualities(self) -> tuple[Quality, ...]:
        """От лучшего к худшему — как в выпадающем списке."""
        return tuple(sorted(self.qualities, key=lambda q: (q.height, q.bitrate_kbps), reverse=True))

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_url) or bool(self.qualities)


def sort_qualities(items: list[Quality]) -> tuple[Quality, ...]:
    """Отсортировать и убрать дубли по высоте (оставить лучший битрейт).

    Дубли бывают реально: у VK несколько CDN-хостов на одно качество.
    """
    seen: dict[int, Quality] = {}
    for q in items:
        cur = seen.get(q.height)
        if cur is None or q.bitrate_kbps > cur.bitrate_kbps:
            seen[q.height] = q
    return tuple(sorted(seen.values(), key=lambda q: (q.height, q.bitrate_kbps), reverse=True))
