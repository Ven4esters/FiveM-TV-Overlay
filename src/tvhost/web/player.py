"""
player.py — страница-плеер и система сессий для управления видео из оверлея.

Идея:
    Телевизор в GTA 5 открывает НЕ голый файл, а нашу HTML-страницу /player/<sid>.
    На странице есть <video> и JS, который:
      - опрашивает прокси на предмет команд (play, pause, volume, seek, subs, fullscreen);
      - отправляет обратно своё состояние (позиция, длительность, пауза, громкость).

    Оверлей выступает пультом: POST /control/<sid> с командой → страница её исполняет.

Почему polling, а не EventSource/WebSocket:
    CEF внутри GTA 5 может быть старой сборкой Chromium, а BaseHTTPRequestHandler
    не умеет chunked/SSE без плясок. Опрос раз в 250 мс работает везде и стоит копейки.

Публичный API:
    sessions = SessionStore()
    sid = sessions.create(url=..., mode=..., title=..., sub=...)
    sessions.push_command(sid, "play", {})       -> seq
    sessions.poll_commands(sid, since=0)         -> (seq, [...])
    sessions.set_state(sid, {...})
    sessions.get_state(sid)                      -> dict
    render_player(sid, session)                  -> str (HTML)
"""

from __future__ import annotations

import html
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

MAX_COMMANDS = 200  # сколько команд хранить (защита от роста)


@dataclass
class Session:
    sid: str
    url: str  # исходная ссылка (обычная, из интернета)
    mode: str  # "yt" | "r" | "get" | "local"
    title: str = ""
    sub: str = ""  # URL .vtt для субтитров (необязательно)
    created: float = field(default_factory=time.time)

    # Откуда играем на самом деле (заполняется при подготовке сессии):
    # "hls" — master-плейлист, "file" — обычный mp4/webm.
    source: str = "file"
    # Для многоозвучных источников (Kodik): список озвучек и выбранная.
    translations: list[dict] = field(default_factory=list)
    translation: str = ""
    translation_index: int = 0
    # Готовая ссылка на поток (если источник требует особой сборки)
    source_url: str = ""
    # Сайт-партнёр, с которого открыт плеер (нужен Kodik для /ftor)
    referer: str = ""

    # очередь команд для страницы
    commands: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0  # последняя выданная команда

    # статус подготовки: preparing -> ready | error
    status: str = "preparing"
    error: str = ""

    # состояние, которое присылает страница
    state: dict[str, Any] = field(
        default_factory=lambda: {
            "playing": False,
            "paused": True,
            "currentTime": 0.0,
            "duration": 0.0,
            "volume": 1.0,
            "muted": False,
            "subs": False,
            "fullscreen": False,
            "ready": False,
            "error": "",
        }
    )
    last_report: float = 0.0


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    # ---------- создание/получение ----------
    def create(self, url: str, mode: str, title: str = "", sub: str = "") -> Session:
        sid = secrets.token_urlsafe(6)
        with self._lock:
            self._sessions[sid] = Session(sid=sid, url=url, mode=mode, title=title, sub=sub)
            # не даём разрастаться: держим последние 50 сессий
            if len(self._sessions) > 50:
                oldest = sorted(self._sessions.values(), key=lambda s: s.created)[:10]
                for s in oldest:
                    self._sessions.pop(s.sid, None)
            return self._sessions[sid]

    def set_status(self, sid: str, status: str, error: str = "") -> None:
        with self._lock:
            s = self._sessions.get(sid)
            if s:
                s.status = status
                s.error = error

    def get(self, sid: str) -> Session | None:
        with self._lock:
            return self._sessions.get(sid)

    def all(self) -> list[Session]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda s: s.created, reverse=True)

    def delete(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.pop(sid, None) is not None

    # ---------- команды от оверлея ----------
    def push_command(self, sid: str, cmd: str, value: Any = None) -> int | None:
        with self._lock:
            s = self._sessions.get(sid)
            if not s:
                return None
            s.seq += 1
            s.commands.append({"seq": s.seq, "cmd": cmd, "value": value, "t": time.time()})
            if len(s.commands) > MAX_COMMANDS:
                s.commands = s.commands[-MAX_COMMANDS:]
            return s.seq

    def poll_commands(self, sid: str, since: int) -> tuple[int, list[dict]]:
        with self._lock:
            s = self._sessions.get(sid)
            if not s:
                return 0, []
            fresh = [c for c in s.commands if c["seq"] > since]
            return s.seq, fresh

    # ---------- состояние от страницы ----------
    def set_state(self, sid: str, patch: dict) -> None:
        with self._lock:
            s = self._sessions.get(sid)
            if not s:
                return
            s.state.update(patch)
            s.last_report = time.time()

    def get_state(self, sid: str) -> dict:
        with self._lock:
            s = self._sessions.get(sid)
            if not s:
                return {}
            st = dict(s.state)
            st["connected"] = (time.time() - s.last_report) < 6 if s.last_report else False
            st["sid"] = sid
            st["title"] = s.title or s.url
            st["seq"] = s.seq
            st["status"] = s.status
            st["sessionError"] = s.error
            st["source"] = s.source
            st["translations"] = [t.get("title") for t in s.translations]
            st["translation"] = s.translation
            st["translationIndex"] = s.translation_index
            st["player"] = f"/player/{sid}"
            return st

    def summary(self) -> list[dict]:
        """Список всех сессий для оверлея: что где играет."""
        with self._lock:
            out = []
            for s in sorted(self._sessions.values(), key=lambda x: x.created, reverse=True):
                st = s.state
                out.append(
                    {
                        "sid": s.sid,
                        "url": s.url,
                        "title": s.title or s.url,
                        "mode": s.mode,
                        "player": f"/player/{s.sid}",
                        "created": s.created,
                        "connected": (time.time() - s.last_report) < 6 if s.last_report else False,
                        "playing": bool(st.get("playing")),
                        "currentTime": st.get("currentTime") or 0,
                        "duration": st.get("duration") or 0,
                        "translation": s.translation,
                        "translations": [t.get("title") for t in s.translations],
                        "translationIndex": s.translation_index,
                        "quality": st.get("quality") or "",
                    }
                )
            return out

    def set_source(self, sid: str, source: str) -> None:
        with self._lock:
            s = self._sessions.get(sid)
            if s:
                s.source = source

    def set_translations(self, sid: str, items: list[dict], index: int = 0) -> None:
        with self._lock:
            s = self._sessions.get(sid)
            if s:
                s.translations = items
                s.translation_index = index
                s.translation = items[index]["title"] if 0 <= index < len(items) else ""

    def set_translation_index(self, sid: str, index: int) -> None:
        with self._lock:
            s = self._sessions.get(sid)
            if s and 0 <= index < len(s.translations):
                s.translation_index = index
                s.translation = s.translations[index]["title"]


# ======================================================================
#                            HTML-СТРАНИЦА
# ======================================================================

PLAYER_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  :root { --ui: rgba(10,12,18,.82); --acc: #4d8dff; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin:0; padding:0; height:100%; background:#000; overflow:hidden;
               font-family: system-ui, "Segoe UI", Roboto, sans-serif; color:#fff; }
  #stage { position:fixed; inset:0; display:flex; align-items:center; justify-content:center; }
  video { width:100%; height:100%; background:#000; object-fit:contain; }
  /* Субтитры: размер по умолчанию (~5% высоты) не читается с дивана.
     150% и подложка — приём из receml/player. Псевдоэлемент понимают
     и старый CEF, и современные браузеры. */
  video::cue { font-size:150%; color:#fff; background:rgba(0,0,0,.6); }

  /* Кнопка запуска поверх всего — CEF может блокировать autoplay */
  #bigplay {
    position:fixed; inset:0; display:flex; align-items:center; justify-content:center;
    background: rgba(0,0,0,.55); cursor:pointer; z-index:40; border:0;
    color:#fff; font: inherit;
  }
  #bigplay .inner { text-align:center; }
  #bigplay .circle {
    width:140px; height:140px; border-radius:50%; background:var(--acc);
    display:flex; align-items:center; justify-content:center; margin:0 auto 18px;
    box-shadow:0 12px 48px rgba(77,141,255,.55);
  }
  #bigplay svg { width:64px; height:64px; margin-left:8px; }
  #bigplay .hint { font-size:20px; opacity:.9; }
  #bigplay .meta { font-size:14px; opacity:.6; margin-top:8px; max-width:70vw;
                   overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

  /* Панель управления */
  #ui {
    position:fixed; left:0; right:0; bottom:0; z-index:30;
    background:linear-gradient(to top, var(--ui), rgba(10,12,18,0));
    padding:26px 18px 14px; transition:opacity .25s, transform .25s;
  }
  #ui.hide { opacity:0; transform:translateY(14px); pointer-events:none; }
  .row { display:flex; align-items:center; gap:12px; }
  .btn {
    background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.18);
    color:#fff; border-radius:10px; padding:10px 14px; font-size:16px; cursor:pointer;
    min-width:52px; line-height:1; user-select:none;
  }
  .btn:hover { background:rgba(255,255,255,.26); }
  .btn.on { background:var(--acc); border-color:var(--acc); }
  .btn.big { font-size:22px; padding:12px 18px; }
  #seek { flex:1; height:8px; -webkit-appearance:none; appearance:none;
          background:rgba(255,255,255,.24); border-radius:99px; outline:none; }
  #seek::-webkit-slider-thumb { -webkit-appearance:none; width:20px; height:20px;
          border-radius:50%; background:#fff; cursor:pointer; }
  #time { font-size:14px; opacity:.85; font-variant-numeric:tabular-nums; min-width:112px; }
  #vol { width:110px; height:8px; -webkit-appearance:none; appearance:none;
         background:rgba(255,255,255,.24); border-radius:99px; }
  #vol::-webkit-slider-thumb { -webkit-appearance:none; width:18px; height:18px;
         border-radius:50%; background:#fff; cursor:pointer; }
  #badge { position:fixed; top:14px; left:14px; z-index:31; font-size:13px;
           background:var(--ui); border:1px solid rgba(255,255,255,.16);
           padding:8px 12px; border-radius:10px; opacity:.9; }
  #toast { position:fixed; top:50%; left:50%; transform:translate(-50%,-50%);
           background:var(--ui); padding:16px 26px; border-radius:14px; font-size:20px;
           opacity:0; transition:opacity .2s; pointer-events:none; z-index:35; }
  #toast.show { opacity:1; }
  /* Режим «на весь экран» без Fullscreen API (CEF может не разрешать) */
  body.theater #ui { padding-bottom:8px; }
  body.clean #ui, body.clean #badge { display:none; }
  body.clean { cursor:none; }
</style>
</head>
<body>
<div id="stage"><video id="v" playsinline preload="auto"></video></div>

<div id="badge">__BADGE__</div>

<button id="bigplay" type="button">
  <div class="inner">
    <div class="circle">
      <svg viewBox="0 0 24 24" fill="#fff"><path d="M8 5v14l11-7z"/></svg>
    </div>
    <div class="hint">Нажмите, чтобы начать</div>
    <div class="meta">__TITLE__</div>
  </div>
</button>

<div id="ui">
  <div class="row" style="margin-bottom:12px">
    <span id="time">0:00 / 0:00</span>
    <input id="seek" type="range" min="0" max="1000" value="0" step="1">
  </div>
  <div class="row">
    <button class="btn big" id="bBack"  title="−10 сек">⏪</button>
    <button class="btn big" id="bPlay"  title="Пауза/пуск (Space)">▶</button>
    <button class="btn big" id="bFwd"   title="+10 сек">⏩</button>
    <button class="btn" id="bMute" title="Без звука (M)">🔊</button>
    <input id="vol" type="range" min="0" max="100" value="100">
    <span style="flex:1"></span>
    <select id="quality" class="pick" title="Качество">
      <option value="-1">Авто</option>
    </select>
    <select id="trans" class="pick" title="Озвучка" style="display:none"></select>
    <button class="btn" id="bSubs" title="Субтитры (C)">CC</button>
    <button class="btn" id="bFull" title="Развернуть (F)">⛶</button>
  </div>
</div>

<style>
/* Выпадающие списки качества и озвучки.
   Без явных цветов у <option> браузер рисует их системным стилем:
   белый фон + белый текст — пункты «не видны», пока не наведёшь мышь. */
select.pick {
  background: rgba(24,24,28,.96);
  color: #fff;
  border: 1px solid rgba(255,255,255,.28);
  border-radius: 10px;
  padding: 10px;
  font-size: 14px;
  cursor: pointer;
}
select.pick option {
  background: #18181c;       /* тёмный фон пункта */
  color: #fff;               /* светлый текст — видно всегда */
  padding: 6px;
}
select.pick option:checked,
select.pick option:hover {
  background: #4d8dff;
  color: #fff;
}
select.pick:focus { outline: 2px solid #4d8dff; }
</style>

<div id="toast"></div>

<script src="/static/hls.min.js"></script>
<script>
(function () {
  "use strict";
  var SID = "__SID__";
  var BASE = "";                       // страница отдаётся тем же сервером
  // ---------- ДИАГНОСТИКА: первый контакт ----------
  // Сколько бы ни было проблем, страница обязана сообщить о себе: иначе
  // в журнале не видно даже того, что телевизор её открыл.
  function bootReport(extra) {
    try {
      var ua = "";
      try { ua = String(navigator.userAgent || "").slice(0, 180); } catch (e) {}
      var info = {
        boot: 1,
        ua: ua,
        screen: (screen && screen.width ? screen.width + "x" + screen.height : ""),
        hlsjs: (window.Hls ? "загружен" : "НЕ загружен"),
        mse: (window.MediaSource ? "да" : "нет"),
        nativeHls: (document.createElement("video").canPlayType(
                      "application/vnd.apple.mpegurl") || "нет"),
        pageTime: (performance && performance.now ? Math.round(performance.now()) : "")
      };
      if (extra) { for (var k in extra) { info[k] = extra[k]; } }
      fetch(BASE + "/report/" + SID, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(info)
      }).catch(function () {});
    } catch (e) {}
  }
  bootReport(null);
  // Если через 12 секунд ничего не заиграло — тоже в журнал, с причиной.
  setTimeout(function () {
    try {
      if (v && v.readyState < 1) {
        bootReport({ error: "12 с, видео не готово: readyState=" + v.readyState
          + " networkState=" + v.networkState
          + " src=" + String(SRC).slice(-50)
          + (window.Hls ? "" : " hls.js НЕ загружен") });
      }
    } catch (e) {}
  }, 12000);

  var v = document.getElementById("v");
  var bigplay = document.getElementById("bigplay");
  var ui = document.getElementById("ui");
  var seek = document.getElementById("seek");
  var vol = document.getElementById("vol");
  var timeEl = document.getElementById("time");
  var toastEl = document.getElementById("toast");
  var bPlay = document.getElementById("bPlay");
  var bMute = document.getElementById("bMute");
  var bSubs = document.getElementById("bSubs");
  var bFull = document.getElementById("bFull");
  var hasSubs = __HAS_SUBS__;

  var hlsInstance = null;      // ссылка на hls.js (для качества)
  var levels = [];             // доступные качества (из master, не теряем)
  var manualQuality = -1;      // -1 = авто
  var SRC = "__SRC__";         // текущий источник (меняется при смене озвучки)
  var TRANS_TITLE = "__TRANS__";
  var TRANS_INDEX = __TRANS_IDX__;
  var lastSeq = 0;
  var dragging = false;
  var hideTimer = null;
  var toastTimer = null;

  // ---------- утилиты ----------
  function isAutoQuality() {
    // «авто» имеет смысл только когда есть из чего выбирать
    return manualQuality < 0 && !!hlsInstance && levels.length > 1;
  }

  // Ближайшее «нормальное» качество к фактической высоте кадра.
  // Нужно потому, что в потоках бывают паддинги (854x640 — это 480p
  // с чёрными полосами, 852x480 у Kodik и т.п.). Показывать «641p»
  // бессмысленно — пользователь ждёт знакомое 480p/720p.
  var STD_HEIGHTS = [144, 240, 360, 480, 540, 720, 1080, 1440, 2160];
  // Стандартные ширины для этих высот — по ним и различаем паддинг.
  var STD_WIDTHS = { 144: 256, 240: 426, 360: 640, 480: 852,
                     540: 960, 720: 1280, 1080: 1920, 1440: 2560, 2160: 3840 };
  function normQuality(hh) {
    if (!hh) return 0;
    var ww = (v && v.videoWidth) || 0;
    // 1) точное совпадение — самый частый случай
    if (STD_HEIGHTS.indexOf(hh) !== -1) return hh;
    // 2) паддинг до кратности 16: сверяем ШИРИНУ, она не искажается
    //    (854x640 -> ширина 854 ≈ 852 -> это 480p, а не 576p)
    if (ww) {
      var best = 0, gap = 1e9;
      for (var i = 0; i < STD_HEIGHTS.length; i++) {
        var std = STD_HEIGHTS[i];
        var d = Math.abs((STD_WIDTHS[std] || std * 16 / 9) - ww);
        if (d < gap) { gap = d; best = std; }
      }
      if (gap <= 40) return best;      // ширина совпала — верим
    }
    // 3) запасной вариант: ближайший снизу (кадр мог «подрасти» паддингом)
    var lower = 0;
    for (var j = 0; j < STD_HEIGHTS.length; j++) {
      if (STD_HEIGHTS[j] <= hh) lower = STD_HEIGHTS[j];
    }
    return lower || hh;
  }

  function currentQualityName() {
    if (manualQuality >= 0 && manualQuality < levels.length) {
      // выбран вручную — показываем именно его
      return levels[manualQuality].name;
    }
    if (v.videoHeight) {
      var hh = normQuality(v.videoHeight) + "p";
      return isAutoQuality() ? (hh + " (авто)") : hh;
    }
    return isAutoQuality() ? "авто" : "";
  }

  /**
   * Переключение качества.
   *
   * Почему не через currentLevel/nextLevel: на живых потоках (Rutube, CDN
   * с короткими сегментами) hls.js игнорирует эти свойства — проверено
   * вживую, уровень не менялся. Надёжный способ — загрузить m3u8 конкретного
   * качества заново, сохранив позицию и состояние воспроизведения.
   */
  function startHls(srcUrl, startSec, wasPlaying) {
    if (hlsInstance) {
      try { hlsInstance.destroy(); } catch (e) {}
      hlsInstance = null;
    }
    if (!window.Hls || !window.Hls.isSupported()) return false;

    // Таймауты и стартовый уровень — те же, что у запасного плеера ниже.
    // Раньше здесь стояли только буферы, и на слабом канале / медленном CDN
    // hls.js сдавался через стандартные 10 с: это и давало manifestLoadError
    // и fragLoadError на первом же сегменте.
    var h = new window.Hls({
      lowLatencyMode: false,
      maxBufferLength: 30,
      maxMaxBufferLength: 60,
      manifestLoadingTimeOut: 30000,
      manifestLoadingMaxRetry: 4,
      manifestLoadingRetryDelay: 1000,
      levelLoadingTimeOut: 30000,
      levelLoadingMaxRetry: 4,
      fragLoadingTimeOut: 60000,
      fragLoadingMaxRetry: 6,
      fragLoadingRetryDelay: 1000,
      // -1 = адаптивно по скорости сети (не фиксируем 1080p: на слабом
      // канале это гарантированный обрыв на первом сегменте)
      startLevel: -1,
      // Оценка скорости «на старте»: по умолчанию у hls.js 500 кбит/с,
      // и он выбирает САМЫЙ НИЗКИЙ уровень (144p) — картинка в игре мыльная.
      // Здесь источник — локальный прокси на этой же машине, поэтому
      // стартуем с разумных 5 Мбит/с, а дальше ABR подстроится сам.
      abrEwmaDefaultEstimate: 5000000,
      // ABR поднимает уровень только если скорость выше битрейта в 1/0.7
      // раз. На локальном прокси это лишнее торможение: поднимаем до 0.9.
      abrBandWidthUpFactor: 0.9,
      abrBandWidthFactor: 0.9,
      startPosition: startSec > 0 ? startSec : -1,
    });
    h.loadSource(srcUrl);
    h.attachMedia(v);

    h.on(window.Hls.Events.MANIFEST_PARSED, function () {
      try { v.currentTime = startSec || 0; } catch (e) {}
      if (wasPlaying) { v.play().catch(function () {}); }
      report();
    });

    var lastErr = "";
    h.on(window.Hls.Events.ERROR, function (_e, data) {
      if (!data || !data.fatal) return;
      var reason = data.details || ("HLS-ошибка " + (data.type || ""));
      if (reason === lastErr) return;
      lastErr = reason;
      showError("Поток: " + reason);
      try {
        fetch(BASE + "/report/" + SID, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ error: "поток: " + reason })
        });
      } catch (e) {}
    });

    hlsInstance = h;
    return true;
  }

  function setQuality(val) {
    if (!hlsInstance) {
      // не поток: качество = разрешение самого файла, менять нечего
      toast("Качество можно менять только у потоковых видео");
      return;
    }

    if (val < 0) {
      // авто: отдаём выбор hls.js (он подберёт под скорость интернета)
      manualQuality = -1;
      try {
        hlsInstance.loadLevel = -1;
        hlsInstance.nextLevel = -1;
        hlsInstance.currentLevel = -1;
      } catch (e) {}
      toast("Качество: авто");
    } else if (val < levels.length) {
      manualQuality = val;
      var native = levels[val].index;
      var pos = v.currentTime || 0;
      var wasPlaying = !v.paused;
      try {
        hlsInstance.loadLevel = native;
        hlsInstance.nextLevel = native;
        hlsInstance.currentLevel = native;
      } catch (e) {}

      // hls.js подхватывает новый уровень не сразу: он доигрывает буфер
      // старого качества (иногда 20+ секунд) и только потом переключается.
      // В игре это выглядит как «качество не меняется». Поэтому сразу
      // вычищаем буфер — переключение происходит мгновенно, а позиция
      // восстанавливается из того же места.
      try {
        var cur = hlsInstance.currentLevel;
        if (cur !== native || v.videoHeight) {
          if (typeof hlsInstance.stopLoad === "function") {
            hlsInstance.stopLoad();
            hlsInstance.startLoad(pos);
          }
        }
      } catch (e) {}

      toast("Качество: " + levels[val].name);
    }
    report();
  }

  // Смена озвучки: сервер присылает новый источник, мы перезапускаем поток
  // с той же позиции (как при переключении качества).
  var pendingTrans = -1;
  function pickTranslation(idx) {
    if (isNaN(idx) || idx < 0) return;
    pendingTrans = idx;
    var pos = v.currentTime || 0;
    try {
      var x = new XMLHttpRequest();
      x.open("GET", BASE + "/trans/" + SID + "?index=" + idx + "&pos=" + Math.round(pos), false);
      x.send(null);
      var data = JSON.parse(x.responseText || "{}");
      if (data && data.ok && data.src) {
        SRC = data.src;
        // ВАЖНО: обновляем текущую озвучку. Иначе report() продолжит
        // сообщать старое значение, оверлей «вернёт» список на первую
        // озвучку, и переключение будет выглядеть сломанным.
        TRANS_INDEX = (typeof data.index === "number") ? data.index : idx;
        if (data.title) TRANS_TITLE = data.title;
        pendingTrans = -1;
        var wasPlaying = !v.paused;
        if (!startHls(SRC, pos, wasPlaying)) { v.src = SRC; }
        toast("Озвучка: " + (data.title || data.index));
        report();
      } else {
        toast("Не удалось сменить озвучку");
      }
    } catch (e) {
      toast("Не удалось сменить озвучку");
    }
  }

  function errText(code) {
    switch (code) {
      case 1: return "воспроизведение прервано";
      case 2: return "сетевая ошибка — сервер не отвечает";
      case 3: return "ошибка декодирования — файл повреждён";
      case 4: return "формат не поддерживается (нужен MP4/H.264 или WebM)";
      default: return "неизвестная ошибка";
    }
  }
  function fmt(t) {
    if (!isFinite(t) || t < 0) t = 0;
    var m = Math.floor(t / 60), s = Math.floor(t % 60);
    return m + ":" + (s < 10 ? "0" : "") + s;
  }
  function toast(text) {
    toastEl.textContent = text;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 1100);
  }

  // Ошибка потока держится на экране, пока воспроизведение не наладится.
  // Иначе беда выглядит так: секунду был текст, потом чёрный экран —
  // и непонятно, то ли ждать, то ли что-то нажимать.
  function showError(text) {
    toastEl.textContent = text;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = null;      // null = сообщение закреплено, само не исчезнет
  }
  function showUI(forceClean) {
    ui.classList.remove("hide");
    if (forceClean === true) { document.body.classList.remove("clean"); }
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function () {
      if (!v.paused) { ui.classList.add("hide"); }
    }, 3200);
  }
  function report() {
    var patch = {
      playing: !v.paused && !v.ended,
      paused: v.paused,
      currentTime: v.currentTime || 0,
      duration: isFinite(v.duration) ? v.duration : 0,
      volume: v.volume,
      muted: v.muted,
      subs: hasSubs && v.textTracks.length > 0 && v.textTracks[0].mode === "showing",
      fullscreen: !!(document.fullscreenElement || document.body.classList.contains("clean")),
      ready: v.readyState >= 1,
      quality: currentQualityName(),
      qualities: levels.map(function (l) { return l.name; }),
      autoQuality: isAutoQuality(),
      translation: TRANS_TITLE,
      translationIndex: TRANS_INDEX,
      error: v.error ? ("код " + v.error.code + ": " + errText(v.error.code)
                        + " | src=" + String(SRC).slice(-60)
                        + " | readyState=" + v.readyState
                        + (v.error.message ? " | " + v.error.message : "")) : ""
    };
    try {
      fetch(BASE + "/report/" + SID, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch)
      }).catch(function () {});
    } catch (e) {}
  }

  // ---------- команды ----------

  // Запуск с обходом блокировки автоплея (приём из receml/player).
  //
  // CEF в GTA 5 не считает нажатие на пульте оверлея «жестом пользователя»
  // для страницы, поэтому play() со звуком может быть отклонён. Тогда
  // пробуем ещё раз БЕЗ ЗВУКА: немое видео лучше чёрного экрана.
  // Звук вернёт первый клик по странице либо команда volume с пульта.
  var unmuteOnClick = false;   // ждём клик, чтобы вернуть звук

  function startPlayback() {
    var p = v.play();
    if (!p || !p.catch) return;
    p.catch(function () {
      if (v.muted) { bigplay.style.display = "flex"; return; }
      v.muted = true;
      if (!unmuteOnClick) {
        unmuteOnClick = true;
        document.addEventListener("click", function unmuteOnce() {
          document.removeEventListener("click", unmuteOnce);
          unmuteOnClick = false;
          v.muted = false;
        });
      }
      var p2 = v.play();
      if (p2 && p2.catch) p2.catch(function () {
        bigplay.style.display = "flex";
        toast("Нажмите на экран");
      });
    });
  }

  function doPlay()  { startPlayback(); }
  function doPause() { v.pause(); }
  function doToggle(){ if (v.paused) doPlay(); else doPause(); }

  function setFullscreen(on) {
    if (on) {
      // 1) пробуем настоящий Fullscreen API
      var el = document.documentElement;
      var p = (el.requestFullscreen || el.webkitRequestFullscreen || el.webkitRequestFullScreen);
      if (p) { try { p.call(el); } catch (e) {} }
      // 2) всегда включаем «театральный» режим — он работает в любом CEF
      document.body.classList.add("clean");
      hideTimer = setTimeout(function () {}, 0);
      toast("Развёрнуто");
    } else {
      if (document.exitFullscreen) { try { document.exitFullscreen(); } catch (e) {} }
      document.body.classList.remove("clean");
      showUI();
      toast("Свёрнуто");
    }
  }

  function setSubs(on) {
    if (!hasSubs) { toast("Субтитры не подключены"); return; }
    for (var i = 0; i < v.textTracks.length; i++) {
      v.textTracks[i].mode = on ? "showing" : "hidden";
    }
    bSubs.classList.toggle("on", on);
    toast(on ? "Субтитры вкл" : "Субтитры выкл");
  }

  // ---------- выравнивание громкости между источниками ----------
  // Зачем: Rutube отдаёт -39.9 LUFS, Kodik -24.6, YouTube -20.9. Разброс
  // 19 дБ — на одном источнике ползунок «20%» тихий, на другом громкий.
  //
  // Как: НЕ перекодируем звук (на 8 ГБ рядом с GTA это дало бы рывки).
  // Поднимаем громкость готового потока через GainNode — это одна
  // операция над выборкой звука, процессор её не замечает.
  //
  // ВАЖНО: createMediaElementSource можно вызвать для элемента ТОЛЬКО
  // ОДИН раз за жизнь страницы, иначе браузер бросит исключение и звук
  // пропадёт совсем. Поэтому граф строится один раз, а дальше меняется
  // только gain.gain.value.
  var audioCtx = null, gainNode = null, normOn = false, normGain = 1;

  function ensureAudioGraph() {
    if (gainNode) return true;
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return false;
      audioCtx = new Ctx();
      var src = audioCtx.createMediaElementSource(v);
      gainNode = audioCtx.createGain();
      gainNode.gain.value = normGain;
      src.connect(gainNode);
      gainNode.connect(audioCtx.destination);
      return true;
    } catch (e) {
      // сюда попадаем, если граф уже создан браузером (CEF иногда
      // переиспользует элемент) — тогда просто не трогаем звук
      gainNode = null;
      return false;
    }
  }

  function applyGain() {
    if (!gainNode) return;
    // автоплей после паузы браузера: контекст создаётся «спящим»
    if (audioCtx && audioCtx.state === "suspended") { try { audioCtx.resume(); } catch (e) {} }
    gainNode.gain.value = normOn ? normGain : 1;
  }

  function setNormalize(on) {
    normOn = !!on;
    if (!normOn) { applyGain(); return; }
    if (!ensureAudioGraph()) { normOn = false; return; }
    applyGain();
    // просим у прокси усиление для этого потока (замер кэширован)
    try {
      fetch(BASE + "/loudness/" + SID)
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var g = Number(d && d.gainLinear);
          if (!isFinite(g) || g <= 0) return;
          // предел x16: выше — это уже шум записи, а не звук
          normGain = Math.max(0.05, Math.min(16, g));
          applyGain();
          toast("Громкость выровнена на " + (d.gainDb > 0 ? "+" : "") + d.gainDb + " дБ");
        })
        .catch(function () {});
    } catch (e) {}
  }

  function applyCommand(c) {
    var val = c.value;
    switch (c.cmd) {
      case "play":        doPlay(); break;
      case "pause":       doPause(); break;
      case "toggle":      doToggle(); break;
      case "seek":        v.currentTime = Number(val) || 0; break;
      case "seekRel":     v.currentTime = Math.max(0, Math.min(v.duration || 1e9, v.currentTime + (Number(val) || 0))); break;
      case "volume":      v.volume = Math.max(0, Math.min(1, Number(val))); v.muted = false; break;
      case "normalize":   setNormalize(val === true || val === "true" || val === 1); break;
      case "mute":        v.muted = (val === true || val === "true" || val === 1); break;
      case "subs":        setSubs(val === true || val === "true" || val === 1); break;
      case "fullscreen":  setFullscreen(val === true || val === "true" || val === 1); break;
      case "restart":     v.currentTime = 0; doPlay(); break;
      case "quality":     setQuality(Number(val)); break;
      case "trans":       pickTranslation(Number(val)); break;
      case "hideui":      document.body.classList.toggle("clean"); break;
    }
    // fullscreen/hideui управляют классом clean сами — не сбрасываем его
    if (c.cmd !== "fullscreen" && c.cmd !== "hideui") { showUI(); }
    report();
    setTimeout(report, 200);   // повторный отчёт: состояние успевает примениться
  }

  function poll() {
    try {
      fetch(BASE + "/cmd/" + SID + "?since=" + lastSeq)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data || !data.commands) return;
          lastSeq = data.seq || lastSeq;
          for (var i = 0; i < data.commands.length; i++) applyCommand(data.commands[i]);
        })
        .catch(function () {});
    } catch (e) {}
  }

  // ---------- интерфейс ----------
  bigplay.addEventListener("click", function () {
    bigplay.style.display = "none";
    // Настоящий жест пользователя: если звук выключили МЫ ради автозапуска,
    // возвращаем его. Если пользователь выключил сам — не трогаем.
    if (unmuteOnClick) { unmuteOnClick = false; v.muted = false; }
    doPlay();
    showUI();
  });
  bPlay.addEventListener("click", doToggle);
  document.getElementById("bBack").addEventListener("click", function () {
    v.currentTime = Math.max(0, v.currentTime - 10); showUI();
  });
  document.getElementById("bFwd").addEventListener("click", function () {
    v.currentTime = Math.min(v.duration || 1e9, v.currentTime + 10); showUI();
  });
  bMute.addEventListener("click", function () { v.muted = !v.muted; });
  bSubs.addEventListener("click", function () { setSubs(bSubs.classList.contains("on") === false); });
  bFull.addEventListener("click", function () {
    setFullscreen(!(document.fullscreenElement || document.body.classList.contains("clean")));
  });

  seek.addEventListener("input", function () {
    dragging = true;
    if (v.duration) timeEl.textContent = fmt(seek.value / 1000 * v.duration) + " / " + fmt(v.duration);
  });
  seek.addEventListener("change", function () {
    if (v.duration) v.currentTime = seek.value / 1000 * v.duration;
    dragging = false;
    report();
  });
  vol.addEventListener("input", function () {
    v.volume = vol.value / 100; v.muted = false; report();
  });

  v.addEventListener("loadedmetadata", function () {
    bigplay.style.display = v.paused ? "flex" : "none";
    if (hasSubs) bSubs.classList.remove("on");
    report();
  });
  v.addEventListener("play", function () {
    bPlay.textContent = "⏸";
    bigplay.style.display = "none";
    // Пошло воспроизведение — снимаем закреплённое сообщение об ошибке.
    // toastTimer === null значит «закреплено» (см. showError).
    if (toastTimer === null) toastEl.classList.remove("show");
    showUI(); report();
  });
  v.addEventListener("pause", function () {
    bPlay.textContent = "▶";
    showUI(); report();
  });
  v.addEventListener("ended", function () { bPlay.textContent = "▶"; report(); });
  v.addEventListener("volumechange", function () {
    bMute.textContent = v.muted || v.volume === 0 ? "🔇" : "🔊";
    vol.value = Math.round((v.muted ? 0 : v.volume) * 100);
    report();
  });
  v.addEventListener("error", function () {
    var code = v.error ? v.error.code : 0;
    var msg = errText(code);
    toast("Ошибка " + code + ": " + msg);
    // подробная подсказка на экране поверх видео
    var box = document.createElement("div");
    box.style.cssText = "position:fixed;inset:0;display:flex;align-items:center;" +
      "justify-content:center;background:rgba(0,0,0,.82);z-index:45;padding:40px;" +
      "text-align:center;font-size:20px;line-height:1.5;color:#fff;";
    box.innerHTML = "<div><div style='font-size:52px;margin-bottom:16px'>⚠️</div>" +
      "<b>Не удалось воспроизвести видео</b><br><br>" +
      "Код " + code + ": " + msg + "<br><br>" +
      "<span style='font-size:16px;opacity:.75'>" +
      "Вернитесь в оверлей — там подробности и кнопка «Повторить»</span></div>";
    box.addEventListener("click", function () { box.remove(); });
    document.body.appendChild(box);
    report();
  });
  v.addEventListener("timeupdate", function () {
    if (dragging) return;
    var d = v.duration || 0;
    timeEl.textContent = fmt(v.currentTime) + " / " + fmt(d);
    if (d > 0) seek.value = Math.round(v.currentTime / d * 1000);
  });
  var qualitySel = document.getElementById("quality");
  qualitySel.addEventListener("change", function () {
    setQuality(parseInt(qualitySel.value, 10));
    showUI();
  });
  // Озвучки (для источников, где их несколько)
  var transSel = document.getElementById("trans");
  var TRANS_LIST = __TRANS_LIST__;
  function fillTransSelect() {
    if (!transSel) return;
    if (!TRANS_LIST || TRANS_LIST.length < 2) { transSel.style.display = "none"; return; }
    transSel.style.display = "";
    transSel.innerHTML = "";
    for (var i = 0; i < TRANS_LIST.length; i++) {
      var o = document.createElement("option");
      o.value = i; o.textContent = TRANS_LIST[i];
      if (i === TRANS_INDEX) o.selected = true;
      transSel.appendChild(o);
    }
  }
  if (transSel) {
    transSel.addEventListener("change", function () {
      pickTranslation(parseInt(transSel.value, 10));
    });
  }
  fillTransSelect();

  function refreshQualitySelect() {
    var prev = qualitySel.value;
    qualitySel.innerHTML = "";
    var optAuto = document.createElement("option");
    optAuto.value = "-1"; optAuto.textContent = "Авто";
    qualitySel.appendChild(optAuto);
    for (var i = 0; i < levels.length; i++) {
      var o = document.createElement("option");
      o.value = String(i);
      o.textContent = levels[i].name;
      qualitySel.appendChild(o);
    }
    qualitySel.value = manualQuality < 0 ? "-1" : String(manualQuality);
    qualitySel.style.display = levels.length > 1 ? "" : "none";
  }
  // обновлять список при появлении уровней
  setInterval(refreshQualitySelect, 1500);

  document.addEventListener("mousemove", showUI);
  document.addEventListener("touchstart", showUI);

  // горячие клавиши — если в игре есть клавиатура/пульт
  document.addEventListener("keydown", function (e) {
    var k = e.key;
    if (k === " " || k === "k") { e.preventDefault(); doToggle(); }
    else if (k === "ArrowLeft")  { v.currentTime = Math.max(0, v.currentTime - 10); showUI(); }
    else if (k === "ArrowRight") { v.currentTime = Math.min(v.duration || 1e9, v.currentTime + 10); showUI(); }
    else if (k === "ArrowUp")   { e.preventDefault(); v.volume = Math.min(1, v.volume + 0.1); }
    else if (k === "ArrowDown") { e.preventDefault(); v.volume = Math.max(0, v.volume - 0.1); }
    else if (k === "m" || k === "M") { v.muted = !v.muted; }
    else if (k === "c" || k === "C") { setSubs(bSubs.classList.contains("on") === false); }
    else if (k === "f" || k === "F") { setFullscreen(!document.fullscreenElement); }
  });

  // отладочный доступ (удобно и для тестов, и для диагностики в игре)
  window.__player = {
    get hls() { return hlsInstance; },
    get levels() { return levels; },
    get quality() { return currentQualityName(); },
    // Для диагностики в игре и для автотеста: показать/снять ошибку
    showError: function (t) { showError(String(t)); },
    hideError: function () { toastEl.classList.remove("show"); }
  };

  // ---------- старт ----------
  var IS_HLS = __IS_HLS__;

  // HLS играем напрямую через hls.js — без скачивания и конвертации.
  // Это главный путь для Rutube, VK, Twitch-записей и большинства сайтов.
  if (IS_HLS && window.Hls && window.Hls.isSupported()) {
    // Таймауты с большим запасом: прокси скачивает сегмент с CDN у себя,
    // и на медленном канале стандартные 10 с hls.js не хватало —
    // отсюда был manifestLoadError «на всех источниках».
    var hls = new window.Hls({
      lowLatencyMode: false,
      maxBufferLength: 30,
      maxMaxBufferLength: 60,
      manifestLoadingTimeOut: 30000,      // было 10000
      manifestLoadingMaxRetry: 4,
      manifestLoadingRetryDelay: 1000,
      levelLoadingTimeOut: 30000,         // плейлист качества
      levelLoadingMaxRetry: 4,
      fragLoadingTimeOut: 60000,          // сегмент 3 МБ на слабом канале
      fragLoadingMaxRetry: 6,
      fragLoadingRetryDelay: 1000,
      startLevel: -1,                     // по скорости сети, не жёстко 1080p
      // Оценка скорости «на старте»: по умолчанию у hls.js 500 кбит/с,
      // и он выбирает САМЫЙ НИЗКИЙ уровень (144p) — картинка в игре мыльная.
      // Здесь источник — локальный прокси на этой же машине, поэтому
      // стартуем с разумных 5 Мбит/с, а дальше ABR подстроится сам.
      abrEwmaDefaultEstimate: 5000000,
      // ABR поднимает уровень только если скорость выше битрейта в 1/0.7
      // раз. На локальном прокси это лишнее торможение: поднимаем до 0.9.
      abrBandWidthUpFactor: 0.9,
      abrBandWidthFactor: 0.9,
    });
    hls.loadSource(SRC);
    hls.attachMedia(v);
    hlsInstance = hls;

    // Если сеть не тянет текущее качество — hls.js понижает уровень сам,
    // но при BANDWIDTH-оценке это происходит не всегда. Помогаем: при
    // ошибке загрузки сегмента уходим уровнем ниже.
    hls.on(window.Hls.Events.ERROR, function (_e, data) {
      if (!data || data.fatal) return;
      if (data.details !== "fragLoadError" && data.details !== "fragLoadTimeOut") return;
      try {
        var cur = hls.currentLevel;
        var max = hls.levels.length - 1;
        if (cur >= 0 && cur < max) {
          hls.nextLevel = cur + 1;        // +1 = ниже качеством
          toast("Сеть не тянет — понижаю качество");
        }
      } catch (e) {}
    });

    // Собираем доступные качества (уровни HLS)
    hls.on(window.Hls.Events.MANIFEST_PARSED, function (_e, data) {
      // ВАЖНО: сохраняем родной порядок hls.js (index = позиция в hls.levels),
      // но показываем пользователю от лучшего к худшему.
      var raw = (data.levels || []).map(function (l, i) {
        return { index: i, height: l.height || 0, bitrate: l.bitrate || 0,
                 url: (l.url && l.url[0]) || "",
                 name: l.height ? (l.height + "p") : Math.round((l.bitrate || 0) / 1000) + "k" };
      }).filter(function (l) { return l.height > 0; });

      // Когда мы загрузили ОДИН вариант (после переключения качества),
      // hls.js видит только его. Список всех качеств не затираем.
      if (raw.length > levels.length) {
        raw.sort(function (a, b) { return b.height - a.height; });  // лучшее сверху
        levels = raw;
      } else if (levels.length === 0) {
        levels = raw;
      }
      report();
    });
    var lastHlsError = "";
    hls.on(window.Hls.Events.ERROR, function (evt, data) {
      if (!data) return;
      var reason = data.details || ("HLS-ошибка " + (data.type || ""));
      // Диагностика: даже НЕфатальные ошибки интересны — по ним видно,
      // что именно тормозит (таймаут сегмента, 403 на плейлисте и т.д.).
      var detail = "поток: " + reason
        + (data.fatal ? " (критично)" : "")
        + (data.response && data.response.code ? " HTTP " + data.response.code : "")
        + (data.response && data.response.text ? " " + String(data.response.text).slice(0, 80) : "")
        + (data.url ? " " + String(data.url).slice(-70) : "")
        + (data.frag && data.frag.url ? " фрагмент " + String(data.frag.url).slice(-50) : "");
      // в журнал отправляем КАЖДУЮ новую формулировку, тост — только фатальное
      if (detail !== lastHlsError) {
        lastHlsError = detail;
        try {
          fetch(BASE + "/report/" + SID, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ error: detail.slice(0, 300) })
          });
        } catch (e) {}
      }
      if (!data.fatal) return;
      showError("Поток: " + reason);
      // одна попытка восстановления
      if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
        setTimeout(function () { try { hls.startLoad(); } catch (e) {} }, 1500);
      } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
        try { hls.recoverMediaError(); } catch (e) {}
      }
    });
  } else if (IS_HLS && v.canPlayType("application/vnd.apple.mpegurl")) {
    v.src = SRC;   // нативная поддержка (Safari)
  } else {
    v.src = SRC;   // обычный файл mp4/webm
  }
  if (hasSubs) {
    var tr = document.createElement("track");
    tr.kind = "subtitles"; tr.label = "Русский"; tr.srclang = "ru";
    tr.src = "__SUB__"; tr.default = false;
    v.appendChild(tr);
  }
  // Стартуем сразу. Если CEF заблокирует автоплей со звуком,
  // startPlayback повторит попытку без звука, а не оставит чёрный экран.
  startPlayback();
  setInterval(poll, 250);
  setInterval(report, 2000);
  report();
})();
</script>
</body>
</html>
"""


def build_source_url(sid: str, session: Session, base: str) -> str:
    """Собрать URL, который страница подставит в источник видео."""
    if session.mode == "local":
        return f"{base}/local/{urllib.parse.quote(session.url)}"
    if session.mode == "play":  # ссылка уже прямая
        return session.url
    q = urllib.parse.quote(session.url, safe="")
    return f"{base}/{session.mode}?url={q}"


# Расширения прямых файлов: их hls.js НЕ разбирает, нужен обычный <video src>.
# Если отдать mp4 в hls.js, он попробует прочитать его как плейлист:
# currentSrc станет blob:, readyState останется 0, экран чёрный (ERR-050).
_DIRECT_FILE_EXT = (
    ".mp4",
    ".m4v",
    ".webm",
    ".mkv",
    ".mov",
    ".avi",
    ".ogv",
    ".ogg",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".opus",
    ".wav",
)


def _looks_like_direct_file(url: str) -> bool:
    """Ссылка оканчивается на расширение файла (с учётом ?query и #hash)."""
    path = (url or "").split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith(_DIRECT_FILE_EXT)


def is_hls_session(session: Session) -> bool:
    """
    Похоже ли на HLS — тогда играем через hls.js, без конвертации.
    Смотрим и на исходную ссылку, и на разрешённый поток (Rutube/YouTube
    отдают страницу, а реальный поток — .m3u8).
    """
    candidates = (session.url or "", getattr(session, "resolved_url", "") or "")
    for candidate in candidates:
        low = candidate.lower()
        if ".m3u8" in low or low.startswith("hls:") or ".mpd" in low:
            return True

    # Прямой файл с известным расширением — это НЕ HLS, что бы там ни было
    # с режимом. Проверяем ДО эвристики по режиму (ERR-050).
    if any(_looks_like_direct_file(c) for c in candidates if c):
        return False

    # Неизвестный сайт с режимом "любой сайт" — вероятнее поток, чем файл:
    # страницы отдают m3u8 через плеер. Если не угадали, hls.js сообщит
    # об ошибке, а плеер покажет текст, а не чёрный экран.
    return session.mode == "r"


def build_stream_url(sid: str, session: Session, base: str) -> str:
    """
    URL именно медиапотока (m3u8/mp4), а не прокси-обёртки.
    Для HLS отдаём /direct — он разрешит страницу в настоящий m3u8
    и проксирует сегменты (нужно из-за CORS у источников).
    """
    q = urllib.parse.quote(session.url, safe="")
    low = (session.url or "").lower()

    # Источник, подготовленный при создании сессии (VK, Kodik, YouTube-HLS)
    if getattr(session, "source_url", ""):
        return session.source_url

    # VK: только наш собственный master (иначе аудио идёт отдельной дорожкой)
    if any(d in low for d in ("vkvideo.ru/", "vk.com/video", "vk.ru/video")):
        return f"{base}/hls?url={q}"

    if is_hls_session(session):
        return f"{base}/direct?url={q}"
    return build_source_url(sid, session, base)


def render_player(sid: str, session: Session, base: str) -> str:
    """Сгенерировать HTML страницы-плеера."""
    title = html.escape(session.title or session.url or "Видео")
    has_subs = bool(session.sub)
    sub_url = ""
    if has_subs:
        if session.sub.startswith(("http://", "https://")):
            sub_url = f"{base}/subs?url={urllib.parse.quote(session.sub, safe='')}"
        else:
            sub_url = f"{base}/local/{urllib.parse.quote(session.sub)}"

    page = PLAYER_HTML
    page = page.replace("__TITLE__", title)
    page = page.replace("__BADGE__", "🎬 " + title)
    page = page.replace("__SID__", sid)
    page = page.replace("__SRC__", build_stream_url(sid, session, base).replace('"', "&quot;"))
    # озвучки (Kodik и подобные): список + выбранная
    import json as _json

    trans_titles = [t.get("title", "") for t in (session.translations or [])]
    page = page.replace("__TRANS_LIST__", _json.dumps(trans_titles, ensure_ascii=False))
    page = page.replace("__TRANS__", (session.translation or "").replace('"', "&quot;"))
    page = page.replace("__TRANS_IDX__", str(max(0, session.translation_index)))
    # источник может быть подготовлен заранее (VK/Kodik/YouTube-HLS)
    is_hls = (session.source == "hls") or is_hls_session(session)
    page = page.replace("__IS_HLS__", "true" if is_hls else "false")
    page = page.replace("__HAS_SUBS__", "true" if has_subs else "false")
    return page.replace("__SUB__", sub_url.replace('"', "&quot;"))


if __name__ == "__main__":  # pragma: no cover
    # Ручная самопроверка рендера. Автотесты — в tests/test_player.py.
    _store = SessionStore()
    _session = _store.create(
        "https://media.w3.org/2010/05/sintel/trailer.mp4",
        "get",
        title="Sintel trailer",
        sub="subs.vtt",
    )
    _page = render_player(_session.sid, _session, "http://127.0.0.1:9000")
    assert "__SID__" not in _page and "__TITLE__" not in _page
    assert "/get?url=" in _page
