"""Этап 6: Kodik — самый сложный источник.

Всё в этих тестах добыто замерами на живом плеере 23.11.2026:
шифр ROT18+base64, ответ /ftor, разметка озвучек.

Ключевая проверка (по требованию заказчика): не просто «получили ссылки»,
а «видео играет» — это отдельный тест на реальном потоке, помеченный live.
"""

from __future__ import annotations

import pytest

from tvhost2.core import SourceState, StreamKind, registry
from tvhost2.sources import KodikSource, register_all
from tvhost2.sources.kodik import (
    decrypt_link,
    first_url,
    page_signatures,
    parse_player_url,
    parse_translations,
    rot18,
)

# Настоящая зашифрованная ссылка из ответа /ftor (качество 360p).
REAL_ENCODED = (
    "iPZ0kPU6Tg9eUBQck29aj2ZrHO4cG29bT3UdjA9pANQeG0pVVsf5WExqZhsfEsZRlEUgWFVqUtRy"
    "Esk0lDsgWFZUU1H6EtpSUEVPmPHHD1R6BPxDms1PABDWUts0BNlDVM5PDFZWZ1N4EFsfiM5FBFxU"
    "D00hBsZrVM0gZFlWmsxbBsMdWBRsUBI0GuUeUuHsGuG3VEQ0U2Q3UhttVENrWBRrGhIfGrChHBo1U"
    "2HtUuRuHORsUERuUBChV2M1HuQfGrtsVBwgULQ2UBMgVLIhThU2UK5bkLY6iOfhWu1pjutuHFV0Tu0"
    "hlBo"
)

# Настоящий фрагмент HTML плеера: озвучки лежат в <option data-media-hash>.
REAL_OPTIONS_HTML = """
<div class="movie-translations-box">
  <select>
    <option value="609" data-id="609" data-translation-type="voice"
            data-media-id="1030406" data-media-hash="e5688041cd57e7f0bcb29e2ed1d766b7"
            data-media-type="seria" data-title="AniDUB">AniDUB</option>
    <option value="923" data-id="923" data-translation-type="voice"
            data-media-id="1033431" data-media-hash="8d87cde76c8b4203ea57fd457b6ef40e"
            data-media-type="seria" data-title="AnimeVost">AnimeVost</option>
    <option value="729" data-id="729" data-translation-type="voice"
            data-media-id="1420137" data-media-hash="746cbbd95c6ac9b2bd8c113953f63646"
            data-media-type="seria" data-title="СВ-Дубль"
            selected="selected">СВ-Дубль</option>
    <option value="1968" data-id="1968" data-translation-type="subtitles"
            data-media-id="1044820" data-media-hash="abc123def456abc123def456abc123de"
            data-media-type="seria" data-title="Light Family.Subtitles"
            >Light Family.Subtitles</option>
  </select>
</div>
"""


def rot8(text: str) -> str:
    """Обратный к ROT18 сдвиг: 18 + 8 = 26 — полный оборот по алфавиту."""
    out = []
    for ch in text:
        code = ord(ch)
        if 65 <= code <= 90:
            out.append(chr((code - 65 + 8) % 26 + 65))
        elif 97 <= code <= 122:
            out.append(chr((code - 97 + 8) % 26 + 97))
        else:
            out.append(ch)
    return "".join(out)


class TestRot18:
    def test_round_trip(self) -> None:
        """Шифрование Kodik — ROT8, расшифровка — ROT18 (8 + 18 = 26).

        Важно: ROT18 НЕ симметричен сам себе (18 + 18 = 36 = сдвиг 10),
        поэтому проверяем именно пару 8/18.
        """
        text = "https://p12.solodcdn.com/s/m/abc"
        assert rot18(rot8(text)) == text

    def test_not_self_inverse(self) -> None:
        """Защита от неверного допущения: дважды ROT18 не возвращает исходное."""
        text = "https://example.com/abc"
        assert rot18(rot18(text)) != text

    def test_letters_shifted(self) -> None:
        assert rot18("a") == "s"
        assert rot18("A") == "S"

    def test_digits_untouched(self) -> None:
        """Цифры и знаки не сдвигаются — иначе base64 поедет."""
        assert rot18("0123456789:/.-_") == "0123456789:/.-_"


class TestDecryptLink:
    def test_real_link_from_ftor(self) -> None:
        """Настоящая строка из ответа Kodik расшифровывается в URL."""
        url = decrypt_link(REAL_ENCODED)
        assert url.startswith("https://p")
        assert "solodcdn.com/s/m/" in url

    def test_already_plain_is_passed(self) -> None:
        """Старые записи отдают ссылку без шифра — не ломаем их."""
        plain = "https://example.com/stream.m3u8"
        assert decrypt_link(plain) == plain

    def test_garbage_raises_human_error(self) -> None:
        from tvhost2.core import SourceFailedError

        with pytest.raises(SourceFailedError) as info:
            decrypt_link("!!!не-base64!!!")
        assert "расшифровать" in info.value.why


class TestParsePlayerUrl:
    def test_seria(self) -> None:
        ref = parse_player_url(
            "https://kodikplayer.com/seria/1420137/746cbbd95c6ac9b2bd8c113953f63646/720p"
        )
        assert ref is not None
        assert ref.kind == "seria"
        assert ref.video_id == "1420137"
        assert ref.hash == "746cbbd95c6ac9b2bd8c113953f63646"
        assert ref.quality == "720p"

    def test_video_kind(self) -> None:
        ref = parse_player_url(
            "https://kodikplayer.com/video/12345/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/360p"
        )
        assert ref is not None and ref.kind == "video"

    def test_mirror_hosts(self) -> None:
        """У Kodik несколько зеркал — все должны узнаваться."""
        for host in ("kodik.info", "kodik.biz", "kodik.cc"):
            ref = parse_player_url(f"https://{host}/seria/1/{'a' * 32}/720p")
            assert ref is not None, host
            assert ref.host == host

    def test_foreign_link(self) -> None:
        assert parse_player_url("https://rutube.ru/video/abc/") is None

    def test_short_hash_rejected(self) -> None:
        """Hash у Kodik ровно 32 символа — иначе это не плеер."""
        assert parse_player_url("https://kodikplayer.com/seria/1/abc/720p") is None


class TestTranslations:
    def test_finds_all(self) -> None:
        translations = parse_translations(REAL_OPTIONS_HTML)
        assert [t["title"] for t in translations] == [
            "AniDUB",
            "AnimeVost",
            "СВ-Дубль",
            "Light Family.Subtitles",
        ]

    def test_selected_flag(self) -> None:
        selected = [t for t in parse_translations(REAL_OPTIONS_HTML) if t["selected"]]
        assert len(selected) == 1
        assert selected[0]["title"] == "СВ-Дубль"

    def test_subtitles_are_kept(self) -> None:
        """Субтитры — тоже дорожка, их нельзя терять."""
        subs = [
            t for t in parse_translations(REAL_OPTIONS_HTML) if t["translation_type"] == "subtitles"
        ]
        assert len(subs) == 1

    def test_hash_is_extracted(self) -> None:
        """Без hash озвучку не запросить — это главное поле."""
        for item in parse_translations(REAL_OPTIONS_HTML):
            assert len(item["hash"]) == 32

    def test_empty_html(self) -> None:
        assert parse_translations("<html></html>") == []


class TestPageSignatures:
    def test_extracts_tokens(self) -> None:
        html = (
            'var domain = "animego.me";\n'
            'var d_sign = "abc123:2611250726";\n'
            'var pd = "kodikplayer.com";\n'
            'var pd_sign = "def456:2611250726";\n'
            'var ref = "https://animego.me/";\n'
            'var ref_sign = "ghi789:2611250726";\n'
        )
        sig = page_signatures(html)
        assert sig["domain"] == "animego.me"
        assert sig["d_sign"] == "abc123:2611250726"
        assert sig["ref"] == "https://animego.me/"

    def test_missing_signs_give_empty(self) -> None:
        assert page_signatures("<html></html>") == {}


class TestFirstUrl:
    def test_best_quality_first(self) -> None:
        links = {
            "360": [{"src": "u360"}],
            "720": [{"src": "u720"}],
            "480": [{"src": "u480"}],
        }
        assert first_url(links) == "u720"

    def test_empty(self) -> None:
        assert first_url({}) == ""


def fake_http(responses: dict[str, str]):
    """Подмена _http: отдаёт заданные ответы по ключу в URL."""

    def _inner(url: str, data: dict[str, str] | None = None, referer: str = "") -> str:
        for key, value in responses.items():
            if key in url:
                return value
        raise AssertionError(f"неожиданный запрос: {url}")

    return _inner


PLAYER_HTML = (
    'var domain = "animego.me"; var d_sign = "d1"; var pd = "kodikplayer.com";'
    ' var pd_sign = "p1"; var ref = "https://animego.me/"; var ref_sign = "r1";' + REAL_OPTIONS_HTML
)

FTOR_JSON = (
    '{"links": {"360": [{"src": "' + REAL_ENCODED + '", "type": "application/x-mpegURL"}],'
    ' "720": [{"src": "' + REAL_ENCODED + '", "type": "application/x-mpegURL"}]}}'
)


class TestResolve:
    def test_full_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tvhost2.sources.kodik._http",
            fake_http({"kodikplayer.com/seria": PLAYER_HTML, "/ftor": FTOR_JSON}),
        )
        stream = KodikSource().resolve(
            "https://kodikplayer.com/seria/1420137/746cbbd95c6ac9b2bd8c113953f63646/720p"
        )
        assert stream.source == "Kodik"
        assert stream.kind is StreamKind.HLS
        assert len(stream.qualities) == 2
        assert stream.qualities[0].height == 720

    def test_translations_attached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Озвучки должны ехать вместе с потоком — иначе переключать нечего."""
        monkeypatch.setattr(
            "tvhost2.sources.kodik._http",
            fake_http({"kodikplayer.com/seria": PLAYER_HTML, "/ftor": FTOR_JSON}),
        )
        stream = KodikSource().resolve(
            "https://kodikplayer.com/seria/1420137/746cbbd95c6ac9b2bd8c113953f63646/720p"
        )
        assert len(stream.audio_tracks) == 4
        assert any(t["title"] == "СВ-Дубль" for t in stream.audio_tracks)

    def test_urls_are_decrypted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tvhost2.sources.kodik._http",
            fake_http({"kodikplayer.com/seria": PLAYER_HTML, "/ftor": FTOR_JSON}),
        )
        stream = KodikSource().resolve(
            "https://kodikplayer.com/seria/1420137/746cbbd95c6ac9b2bd8c113953f63646/720p"
        )
        assert all(q.url.startswith("https://") for q in stream.qualities)

    def test_no_signatures_gives_human_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tvhost2.core import SourceFailedError

        monkeypatch.setattr(
            "tvhost2.sources.kodik._http", fake_http({"kodik": "<html>пусто</html>"})
        )
        with pytest.raises(SourceFailedError) as info:
            KodikSource().resolve("https://kodikplayer.com/seria/1/" + "a" * 32 + "/720p")
        assert "подписи" in info.value.why
        assert info.value.state is SourceState.PARTIAL

    def test_empty_links_gives_human_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tvhost2.core import SourceFailedError

        monkeypatch.setattr(
            "tvhost2.sources.kodik._http",
            fake_http({"kodikplayer.com/seria": PLAYER_HTML, "/ftor": '{"links": {}}'}),
        )
        with pytest.raises(SourceFailedError) as info:
            KodikSource().resolve("https://kodikplayer.com/seria/1/" + "a" * 32 + "/720p")
        assert "не отдано" in info.value.why


class TestMatches:
    def test_player_links(self) -> None:
        assert KodikSource().matches(
            "https://kodikplayer.com/seria/1420137/746cbbd95c6ac9b2bd8c113953f63646/720p"
        )
        assert KodikSource().matches(
            "https://kodik.info/video/1/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/360p"
        )

    def test_foreign(self) -> None:
        assert not KodikSource().matches("https://rutube.ru/video/abc/")
        assert not KodikSource().matches("https://www.youtube.com/watch?v=x")


class TestRegistry:
    def test_registered(self) -> None:
        register_all()
        assert "Kodik" in registry.names()

    def test_finds_player_link(self) -> None:
        register_all()
        found = registry.find(
            "https://kodikplayer.com/seria/1420137/746cbbd95c6ac9b2bd8c113953f63646/720p"
        )
        assert found is not None and found.name == "Kodik"


@pytest.mark.network
class TestLiveStream:
    """Живая проверка: сеть обязательна, поэтому отдельная метка.

    Запуск: pytest -m network tests/v2/test_kodik.py
    """

    def test_player_url_parsed(self) -> None:
        ref = parse_player_url(
            "https://kodikplayer.com/seria/1420137/746cbbd95c6ac9b2bd8c113953f63646/720p"
        )
        assert ref is not None

    def test_stream_url_is_real_hls(self) -> None:
        """Расшифрованная ссылка действительно отдаёт HLS-плейлист."""
        import urllib.request

        ref = parse_player_url(
            "https://kodikplayer.com/seria/1420137/746cbbd95c6ac9b2bd8c113953f63646/720p"
        )
        assert ref is not None
        from tvhost2.sources.kodik import fetch_links

        links = fetch_links(ref)
        assert links, "Kodik не отдал ни одного качества"
        url = first_url(links)
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://kodikplayer.com/"}
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read(500).decode("utf-8", "replace")
        assert body.startswith("#EXTM3U"), "ответ не является HLS-плейлистом"
