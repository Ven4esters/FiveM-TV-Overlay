"""cdnvideohub — ОДИН разбор на все сайты, где стоит этот плеер.

Требование заказчика: «займись CVH и в идеале не только на yummyanime».
Раньше разбор был в `anidub_org.py` и знал только про anidub. Здесь
проверяется, что он общий: anidub.org и yummyanime отличаются двумя
числами (`pub`, `aggr`), а не копией кода.
"""

from __future__ import annotations

from typing import Any

from tvhost2.sources import cvh

PLAYLIST: dict[str, Any] = {
    "titleName": "Чёрный клевер 2",
    "isSerial": True,
    "items": [
        {"cvhId": "a1", "vkId": "111", "voiceStudio": "DEEP", "voiceType": "Дубляж", "episode": 1},
        {
            "cvhId": "a2",
            "vkId": "222",
            "voiceStudio": "AniStar",
            "voiceType": "Многоголосый",
            "episode": 1,
        },
        {"cvhId": "b1", "vkId": "333", "voiceStudio": "DEEP", "voiceType": "Дубляж", "episode": 3},
    ],
}


class TestParams:
    """Параметры плеера: у каждого сайта свои числа, форма запроса одна."""

    def test_anidub_numbers(self) -> None:
        url = cvh.CvhParams(pub="19", aggr="cvh", title_id="019169b6").playlist_url()
        assert "pub=19" in url and "aggr=cvh" in url and "id=019169b6" in url

    def test_yummy_numbers(self) -> None:
        """Проверено из бандла iframeCVH: pub=745, aggr=mali."""
        url = cvh.CvhParams(pub="745", aggr="mali", title_id="61967").playlist_url()
        assert "pub=745" in url and "aggr=mali" in url and "id=61967" in url

    def test_aggr_optional(self) -> None:
        """Без aggregator API тоже отвечает — поле не обязательное."""
        assert "aggr" not in cvh.CvhParams(pub="19", title_id="x").playlist_url()


class TestPick:
    def test_first_episode_by_default(self) -> None:
        assert cvh.pick(PLAYLIST)["vkId"] == "111"

    def test_translation_within_episode(self) -> None:
        assert cvh.pick(PLAYLIST, 1, 1)["vkId"] == "222"

    def test_wanted_episode(self) -> None:
        assert cvh.pick(PLAYLIST, 0, 3)["vkId"] == "333"

    def test_missing_episode_takes_nearest_below(self) -> None:
        """Серии 2 нет: «ближайшая меньшая» — это 1, а не пустота."""
        assert cvh.pick(PLAYLIST, 0, 2)["vkId"] == "111"

    def test_episode_below_range_takes_first(self) -> None:
        assert cvh.pick(PLAYLIST, 0, 0)["vkId"] == "111"

    def test_bad_index_means_first(self) -> None:
        """Неверный индекс озвучки — главная, а не случайная последняя."""
        assert cvh.pick(PLAYLIST, 99, 1)["vkId"] == "111"

    def test_empty_playlist(self) -> None:
        assert cvh.pick({"items": []}) == {}

    def test_tracks_without_vk_are_skipped(self) -> None:
        """Дорожка без `vkId` — не видео: её нельзя открыть."""
        playlist = {"items": [{"episode": 1, "voiceStudio": "X"}]}
        assert cvh.tracks_by_episode(playlist) == {}


class TestVoiceTitle:
    def test_studio_wins_over_changing_type(self) -> None:
        """Студия — подпись, тип к ней не добавляется.

        Тип у одной и той же студии меняется от серии к серии (у anidub.org
        «Дубляж» в первой, «Многоголосый» во второй). Пульт сверяет строку
        по названию, поэтому подпись обязана быть стабильной — иначе список
        выглядит «перепутанным», а номер перестаёт находиться.
        """
        assert cvh.voice_title({"voiceStudio": "DEEP", "voiceType": "Дубляж"}) == "DEEP"

    def test_same_studio_same_title_across_series(self) -> None:
        """Одна студия в разных сериях — одна и та же подпись."""
        first = cvh.voice_title({"voiceStudio": "AniLiberty", "voiceType": "Дубляж"})
        second = cvh.voice_title({"voiceStudio": "AniLiberty", "voiceType": "Многоголосый"})
        assert first == second == "AniLiberty"

    def test_unknown_studio_shows_type(self) -> None:
        assert (
            cvh.voice_title({"voiceStudio": "Неизвестный", "voiceType": "Субтитры"}) == "Субтитры"
        )

    def test_same_words_not_doubled(self) -> None:
        assert cvh.voice_title({"voiceStudio": "Дубляж", "voiceType": "Дубляж"}) == "Дубляж"


class TestTkhd:
    """Размер кадра: из самого файла, а не из имени поля `mpegHighUrl`."""

    @staticmethod
    def build_head(moov_size: int, width: int = 1920, height: int = 1080) -> bytes:
        """mp4-голова по образцу живого файла okcdn.

        Проверено 24.11.2026: `ftyp (32) → moov (1 175 356) → mvhd → trak
        → tkhd`, и `tkhd` лежит в самом начале `moov`, на 160-м байте.
        """
        head = bytearray(400)
        head[0:8] = (32).to_bytes(4, "big") + b"ftyp"
        head[32:40] = moov_size.to_bytes(4, "big") + b"moov"
        head[40:48] = (100).to_bytes(4, "big") + b"mvhd"
        head[140:148] = (228).to_bytes(4, "big") + b"trak"
        head[148:156] = (92).to_bytes(4, "big") + b"tkhd"
        head[148 + 92 - 8 : 148 + 92 - 4] = (width << 16).to_bytes(4, "big")
        head[148 + 92 - 4 : 148 + 92] = (height << 16).to_bytes(4, "big")
        return bytes(head)

    def test_garbage_is_zero(self) -> None:
        assert cvh._tkhd_size(b"<html>403 Forbidden</html>") == (0, 0)

    def test_truncated_is_zero(self) -> None:
        assert cvh._tkhd_size(bytes(100)) == (0, 0)

    def test_small_container_reads(self) -> None:
        assert cvh._tkhd_size(self.build_head(moov_size=368)) == (1920, 1080)

    def test_cut_container_is_walked(self) -> None:
        """`moov` длиннее прочитанного куска — всё равно ищем внутри.

        Живой случай okcdn: `moov` весит 1,1 МБ, а читаем мы 256 КБ.
        Прежний обход требовал, чтобы контейнер влез целиком, и молча
        терял все качества — видео открывалось пустым (поймано на
        «Чёрном клевере» с yummyanime).
        """
        head = self.build_head(moov_size=1_175_356)
        assert cvh._tkhd_size(head) == (1920, 1080)


class TestQualities:
    def test_no_mp4_means_no_qualities(self) -> None:
        """Пустые поля okcdn — это отсутствие варианта, а не 0p."""
        video = {"sources": {"hlsUrl": "https://x/video.m3u8", "mpegHighUrl": ""}}
        assert cvh.qualities_from_video(video) == ()

    def test_master_parsed(self) -> None:
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=3301280,RESOLUTION=1280x720\n"
            "https://x/720/index.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=5834016,RESOLUTION=1920x1080\n"
            "https://x/1080/index.m3u8\n"
        )
        qualities = cvh.parse_master(master)
        assert [q.height for q in qualities] == [1080, 720]

    def test_relative_master_lines_skipped(self) -> None:
        """У okcdn в мастере пути, а не адреса — их брать нельзя."""
        master = "#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720\n/expires/1/type/2/video/\n"
        assert cvh.parse_master(master) == ()

    def test_hls_url_from_video(self) -> None:
        assert cvh.video_hls_url({"sources": {"hlsUrl": "https://x/a.m3u8"}}) == "https://x/a.m3u8"
        assert cvh.video_hls_url({}) == ""
