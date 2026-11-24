# Инструменты разбора плеера Alloha

Всё, чем 24.11.2026 расшифровывался `build/app.c63ca8da.js` и проверялась
цепочка получения адресов. Порядок — как в работе.

## 1. Скачать скрипт и извлечь пул строк

```bash
curl -s -A 'Mozilla/5.0 Chrome/131' \
     https://tories-as.stravers.live/build/app.c63ca8da.js \
  | gunzip > /tmp/pl_app.c63ca8da.js

node tools/alloha/deob/extract_pool.js     # → /tmp/pool_rot.json (2208 строк)
```

`extract_pool.js` исполняет `a0Q()` и IIFE самозащиты в `vm`, поэтому пул
получается уже после прокрутки (394 позиции). Взять литерал с листа нельзя:
индексы съедут.

## 2. Расшифровать весь скрипт

```bash
python3 tools/alloha/deob/decode_app.py    # → /tmp/app_dec.js (648 КБ)
```

Ключ: все обёртки сводятся к `a0y(Q,y){Q=Q-353; return a0Q()[Q]}`, значит
`F(a,b)` — это `pool[a - const]`. Имя алиаса (`Yl`, `Yk`) объявлено в файле
десятки раз с РАЗНЫМИ смещениями — разрешать нужно ближайшее ПРЕДЫДУЩЕЕ
объявление, а не последнее по файлу (иначе мусор, проверено).

Контроль: `qY(0x1da,0x509)` → `consoleLog`, `self['webpackChunk']` сходится.
Если не сходится — пул или смещение неверны, дальше идти бессмысленно.

## 3. Посчитать суффикс `Borth` из меты страницы

```bash
META=$(python3 - <<'EOF'
import re
h = open('alloha_player.html', encoding='utf-8').read()
print(re.search(r'name="viewporti" content="([^"]+)"', h).group(1))
EOF
)
node tools/alloha/deob/borth.js "$META"
```

`Borth = <первая часть> | iP(iR(iS(meta)))`. Первая часть — отпечаток
браузера (`c0()` в app.js, sha256 от UA/TZ/экрана/canvas/WebGL/audio);
вне браузера точно не воспроизводится.

## 4. Проверить саму цепочку

```bash
python3 tools/alloha/deob/bnsi_probe.py
```

Делает: guard (`/sarn` → `/vorf`) → мету со страницы → `Borth` →
`POST /bnsi/{movies,trailers}/<id>`. С российского адреса в ответе будет
`hlsSource` — массив озвучек с адресами. С зарубежного — 404 «контент не
найден» на любой id (проверка региональная, см. отчёт).

## Что где в расшифровке искать

| Что | Маркер в `/tmp/app_dec.js` |
|---|---|
| отпечаток браузера | `function c0()` |
| перестановки меты | `function iS(`, `function iR(`, `function iP(` |
| запрос адресов | `"/bnsi"`, `'Borth'` |
| ответ и поля | `hX["hlsSource"]`, `hX["pnr"]`, `hX["pnk"]` |
| выбор высоты | `currentSource["quality"][hf]`, `.split(" or ")` |
| WebSocket | `function cj(`, `"config_update"`, `edge_hash` |
| заголовки для CDN | `setRequestHeader('Accepts-Controls'`, `Authorizations` |

Подробный разбор — `docs/REPORT_alloha_source.md`.
