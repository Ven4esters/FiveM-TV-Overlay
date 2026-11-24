"""Всеoha guard: рабочая реализация проверки /sarn → /vorf.

Проверено живьём 24.11.2026 из США: возвращает настоящий токен.
Запуск: python3 tools/alloha/guard_probe.py

Зачем файл в репозитории: это единственная часть механизма Alloha, которую
удалось снять с плеера целиком и проверить. Когда понадобится реализовывать
источник (или отлаживать его), начинать надо отсюда, а не с нуля.

Что НЕ решено: как из этого токена получается адрес CDN
(`https://<узел>.userplay.live/1/<1962 символа>/master.m3u8`). Адрес не
лежит ни в странице плеера, ни в ответе api.alloha.tv — его выдаёт запрос
плеера, который найти пока не удалось.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"


def _call(
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    merged = {"User-Agent": USER_AGENT}
    merged.update(headers or {})
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=merged)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def guard_token(player_origin: str, translation: str = "93") -> str:
    """Токен плеера. Именно он уходит в заголовок `Accepts-Controls`.

    Как снято с `app.c63ca8da.js` (функция `c9`), переписанное вручную:

        GET  /sarn        заголовок acceptis: <домен>.<озвучка>
             → {challenge, nonce, uap}

        proof = sha256( nonce | <домен> | uap | <origin> | <origin> )

        POST /vorf        заголовки: content-type, accepts-block: <домен>.<озвучка>
             {challenge, proof, parentOrigin, referrer}
             → {ok, token}

    Все четыре части proof обязательны, порядок именно такой. `parentOrigin`
    повторяется дважды — это не опечатка: в коде одна и та же величина стоит
    дважды через вертикальную черту (страховка автора на случай пустого
    `document.referrer`).
    """
    host = player_origin.split("://", 1)[-1].split("/", 1)[0]
    acceptis = f"{host}.{translation}"

    status, body = _call(f"{player_origin}/sarn", headers={"acceptis": acceptis})
    if status != 200:
        raise RuntimeError(f"/sarn ответил {status}: {body[:120]}")
    challenge = json.loads(body)

    glue = "|".join([challenge["nonce"], host, challenge["uap"], player_origin, player_origin])
    proof = hashlib.sha256(glue.encode()).hexdigest()

    status, body = _call(
        f"{player_origin}/vorf",
        {
            "challenge": challenge["challenge"],
            "proof": proof,
            "parentOrigin": player_origin,
            "referrer": f"{player_origin}/",
        },
        {"content-type": "application/json", "accepts-block": acceptis},
    )
    if status != 200:
        raise RuntimeError(f"/vorf ответил {status}: {body[:120]}")
    answer = json.loads(body)
    if not answer.get("ok") or not answer.get("token"):
        raise RuntimeError(f"/vorf не дал токен: {body[:120]}")
    return str(answer["token"])


if __name__ == "__main__":
    token = guard_token("https://tories-as.stravers.live")
    print(f"токен получен: {len(token)} символов")
    print(f"  {token[:48]}…")
