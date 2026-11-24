"""Заголовки моста под CDN Alloha (`userplay.live`).

Проверено живьём 24.11.2026 и по HAR заказчика (Firefox, zlord.org):

    только User-Agent            → 403
    только Origin (без UA)       → 200
    Origin + испорченный путь    → 403
    Origin + Referer             → 200

То есть CDN принимает поток, если в запросе есть `Origin` домена плеера.
Ни `Accepts-Controls`, ни `Authorizations`, ни `Referer`, ни cookies не
проверяются — это проверено перебором по одному заголовку.

Раньше правило отсутствовало, и мост получал 403 на подплейлисте: мастер
отдавался (его мы запрашивали напрямую), а всё, что за ним, — нет. Теперь
проверено живьём: мастер → подплейлист → сегмент 1.7 МБ через наш мост.
"""

from __future__ import annotations

from tvhost2.web.server import _ALLOHA_PLAYER_ORIGIN, _referer_headers


class TestAllohaHeaders:
    def test_cdn_gets_player_origin(self) -> None:
        """`Origin` обязателен именно доменом плеера — CDN и сайт дают 403."""
        headers = _referer_headers("https://0a-2e-7c-r400.userplay.live/1/abc123/master.m3u8")
        assert headers == {"Origin": "https://tories-as.stravers.live"}

    def test_segments_and_sublists_too(self) -> None:
        """Правило по домену, а не по конкретному файлу."""
        for path in ("index-f1-v1-a1.m3u8", "seg-1-f1-v1-a1.m4s", "master.m3u8"):
            url = f"https://0a-2e-7c-r400.userplay.live/1/abc123/{path}"
            assert _referer_headers(url)["Origin"] == _ALLOHA_PLAYER_ORIGIN

    def test_no_extra_headers(self) -> None:
        """Лишних заголовков не шлём: CDN их не требует, а Referer чужого
        домена некоторые CDN считают попыткой обхода."""
        headers = _referer_headers("https://x.userplay.live/1/y/master.m3u8")
        assert set(headers) == {"Origin"}

    def test_other_hosts_untouched(self) -> None:
        """Правило не протекает на другие источники."""
        assert _referer_headers("https://rutube.ru/x.m3u8") == {"Referer": "https://rutube.ru/"}
        assert _referer_headers("https://example.com/x.m3u8") == {}
