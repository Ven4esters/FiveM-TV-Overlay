"""Страница плеера для телевизора в GTA 5.

Почему страница, а не прямой файл: телевизор в игре открывает наш адрес, а
не ссылку CDN. Тогда мы можем управлять видео из оверлея (пульт), показывать
состояние и держать поток живым через мост.

Ключевые решения, перенесённые из v1 вместе с опытом:

1. **Опрос, а не WebSocket.** CEF внутри GTA 5 — старая сборка Chromium,
   и события через SSE/WS там ведут себя непредсказуемо. Опрос раз в 250 мс
   работает везде и стоит копейки.

2. **Автоплей со звуком может быть заблокирован.** Тогда запускаем без
   звука, а звук включаем по первому клику. Оставлять чёрный экран нельзя —
   человек не понимает, что происходит.

3. **Выравнивание громкости — GainNode, не перекодирование.** Усиление
   готового звука процессор не замечает; loudnorm на лету дал бы рывки
   на 8 ГБ памяти рядом с запущенной игрой.

4. **Ошибка hls.js показывается с причиной.** Обрыв моста и 502 — разные
   вещи: на 502 плеер переключается на другое качество сам.
"""

from __future__ import annotations

import html
import urllib.parse

from .session import Session

BASE_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; background: #000; overflow: hidden; }
body { font-family: "Segoe UI", Roboto, Arial, sans-serif; color: #CBD2E4; }
#wrap { position: relative; width: 100%; height: 100%; }
video { width: 100%; height: 100%; object-fit: contain; background: #000; display: block; }
#bigplay {
  position: absolute; inset: 0; display: none; align-items: center;
  justify-content: center; background: #000A; cursor: pointer; border: 0;
}
#bigplay svg { width: 96px; height: 96px; fill: #FFF; opacity: .9; }
#bigplay:hover svg { opacity: 1; }
#toast {
  position: absolute; left: 50%; bottom: 48px; transform: translateX(-50%);
  background: #111827; border: 1px solid #39404F; border-radius: 6px;
  padding: 8px 12px; font-size: 14px; opacity: 0; transition: opacity .2s;
  pointer-events: none; max-width: 80%;
}
#toast.on { opacity: 1; }
#err {
  position: absolute; left: 24px; right: 24px; bottom: 24px;
  background: #111827; border: 1px solid #7F1D1D; border-radius: 6px;
  padding: 12px 16px; font-size: 14px; display: none; line-height: 1.5;
}
#err b { color: #F87171; }
#err small { color: #6E7689; }
"""

PLAY_ICON = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>'

# Скрипт страницы. Подставляются только SID и BASE — остальное статично.
SCRIPT = """
(function () {
  var SID = __SID__;
  var BASE = __BASE__;
  // Прямой эфир: у live нет длительности, перемотка бессмысленна, а hls.js
  // должен держаться края плейлиста, иначе отставание растёт на глазах.
  var LIVE = __LIVE__;
  var v = document.getElementById("v");
  var bigplay = document.getElementById("bigplay");
  var toastEl = document.getElementById("toast");
  var errEl = document.getElementById("err");

  var lastSeq = 0;
  var unmuteOnClick = false;
  var hls = null;
  var toastTimer = null;
  var normOn = false, normGain = 1;
  var audioCtx = null, gainNode = null;

  function toast(text) {
    toastEl.textContent = text;
    toastEl.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("on"); }, 2500);
  }

  function showError(title, hint) {
    /* Текст ошибки виден В ТЕЛЕВИЗОРЕ: человек должен понять, что случилось,
       не открывая компьютер. */
    errEl.innerHTML = "<b>" + title + "</b>" + (hint ? "<br><small>" + hint + "</small>" : "");
    errEl.style.display = "block";
  }

  function hideError() { errEl.style.display = "none"; }

  // ---------------- выравнивание громкости ----------------
  // createMediaElementSource можно вызвать для элемента ТОЛЬКО ОДИН раз.
  // Второй вызов бросает исключение и звук пропадает совсем — поэтому
  // граф строится один раз, дальше меняется только gain.gain.value.
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
      gainNode = null;   // CEF иногда переиспользует элемент — не ломаем звук
      return false;
    }
  }

  function applyGain() {
    if (!gainNode) return;
    if (audioCtx && audioCtx.state === "suspended") {
      try { audioCtx.resume(); } catch (e) {}
    }
    gainNode.gain.value = normOn ? normGain : 1;
  }

  function setNormalize(on) {
    normOn = !!on;
    if (!normOn) { applyGain(); return; }
    if (!ensureAudioGraph()) { normOn = false; return; }
    applyGain();
    // усиление считает прокси (замер EBU R128), плеер только применяет
    fetch(BASE + "/loudness/" + SID)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var g = Number(d && d.gainLinear);
        if (!isFinite(g) || g <= 0) return;
        normGain = Math.max(0.05, Math.min(16, g));   // x16 = предел разумного
        applyGain();
        toast("Громкость выровнена на " + (d.gainDb > 0 ? "+" : "") + d.gainDb + " дБ");
      })
      .catch(function () {});
  }

  // ---------------- запуск воспроизведения ----------------
  function startPlayback() {
    var p = v.play();
    if (!p || !p.catch) return;
    p.catch(function () {
      if (v.muted) { bigplay.style.display = "flex"; return; }
      // автоплей со звуком заблокирован: запускаем без звука, звук — по клику
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

  bigplay.addEventListener("click", function () {
    bigplay.style.display = "none";
    v.muted = false;
    startPlayback();
  });

  // ---------------- загрузка потока ----------------
  function attach() {
    var src = BASE + "/stream/" + SID;
    if (window.Hls && window.Hls.isSupported()) {
      hls = new window.Hls({
        // Стартуем с лучшего качества, дальше ABR решает сам.
        // capLevelToPlayerSize: не тянуть 1080p в окошко 640 пикселей.
        capLevelToPlayerSize: true,
        enableWorker: true,
        lowLatencyMode: false,
        // У эфира — держимся живого края: 3 сегмента отставания, а не
        // «догнать любой ценой» (рывками картинка хуже, чем отставание).
        liveDurationInfinity: LIVE,
        maxLiveSyncPlaybackRate: LIVE ? 1.5 : 1
      });
      hls.loadSource(src);
      hls.attachMedia(v);

      hls.on(window.Hls.Events.ERROR, function (evt, data) {
        if (!data) return;
        var reason = data.details || ("HLS-ошибка " + (data.type || ""));
        var code = data.response && data.response.code ? " HTTP " + data.response.code : "";
        if (!data.fatal) return;
        showError("Поток не загрузился", reason + code);
        // Одна попытка восстановления: сетевые сбои часто временные.
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
          setTimeout(function () { try { hls.startLoad(); } catch (e) {} }, 1500);
        } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
          try { hls.recoverMediaError(); } catch (e) {}
        }
      });

      hls.on(window.Hls.Events.MANIFEST_PARSED, function () {
        hideError();
        startPlayback();
      });
    } else if (v.canPlayType("application/vnd.apple.mpegurl")) {
      v.src = src;   // нативная поддержка (Safari, часть сборок CEF)
      startPlayback();
    } else {
      v.src = src;
      startPlayback();
    }
  }

  // ---------------- команды из оверлея ----------------
  function applyCommand(c) {
    var val = c.value;
    switch (c.cmd) {
      case "play":       startPlayback(); break;
      case "pause":      v.pause(); break;
      case "toggle":     if (v.paused) { startPlayback(); } else { v.pause(); } break;
      case "seek":
        if (LIVE) { toast("В прямом эфире перемотки нет"); break; }
        v.currentTime = Number(val) || 0;
        break;
      case "seekRel":
        if (LIVE) { toast("В прямом эфире перемотки нет"); break; }
        var d = Number.isFinite(v.duration) ? v.duration : 1e9;
        v.currentTime = Math.max(0, Math.min(d, v.currentTime + (Number(val) || 0)));
        break;
      case "volume":     v.volume = Math.max(0, Math.min(1, Number(val))); v.muted = false; break;
      case "mute":       v.muted = (val === true || val === "true" || val === 1); break;
      case "normalize":  setNormalize(val === true || val === "true" || val === 1); break;
      case "quality":    setQuality(Number(val)); break;
      case "trans":      pickTranslation(Number(val)); break;
      case "restart":    v.currentTime = 0; startPlayback(); break;
      case "fullscreen":
        if (val === true || val === "true" || val === 1) {
          if (document.documentElement.requestFullscreen) {
            document.documentElement.requestFullscreen().catch(function () {});
          }
        } else if (document.exitFullscreen) {
          document.exitFullscreen().catch(function () {});
        }
        break;
    }
    report();
    setTimeout(report, 200);   // повтор: состояние успевает примениться
  }

  function setQuality(height) {
    if (!hls || !hls.levels || !hls.levels.length) return;
    if (!height) { hls.currentLevel = -1; return; }   // «Авто»
    for (var i = 0; i < hls.levels.length; i++) {
      if (hls.levels[i].height === height) { hls.currentLevel = i; return; }
    }
    // точного совпадения нет — берём ближайшее не выше запрошенного
    var best = -1, bestH = 0;
    for (var j = 0; j < hls.levels.length; j++) {
      var h = hls.levels[j].height || 0;
      if (h <= height && h > bestH) { bestH = h; best = j; }
    }
    if (best >= 0) hls.currentLevel = best;
  }

  function pickTranslation(index) {
    // У Kodik озвучка — отдельный поток со своим hash. Смена озвучки =
    // новая сессия на стороне прокси, страница просто перезагружается.
    fetch(BASE + "/trans/" + SID + "?index=" + index, { method: "POST" })
      .then(function () {
        toast("Озвучка меняется…");
        setTimeout(function () { location.reload(); }, 600);
      })
      .catch(function () { toast("Не удалось сменить озвучку"); });
  }

  // ---------------- связь с прокси ----------------
  function report() {
    var levels = [];
    if (hls && hls.levels) {
      for (var i = 0; i < hls.levels.length; i++) levels.push(hls.levels[i].height || 0);
    }
    fetch(BASE + "/report/" + SID, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        currentTime: v.currentTime || 0,
        duration: LIVE ? 0 : (v.duration || 0),
        paused: !!v.paused,
        volume: v.volume,
        muted: v.muted,
        playing: !v.paused && !v.ended && v.readyState > 2,
        levels: levels,
        level: hls && hls.currentLevel >= 0 ? (hls.levels[hls.currentLevel].height || 0) : 0
      })
    }).catch(function () {});
  }

  function poll() {
    fetch(BASE + "/cmd/" + SID + "?since=" + lastSeq)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.commands) return;
        lastSeq = data.seq || lastSeq;
        for (var i = 0; i < data.commands.length; i++) applyCommand(data.commands[i]);
      })
      .catch(function () {});
  }

  v.addEventListener("error", function () {
    var e = v.error;
    if (!e) return;
    var codes = {
      1: "загрузка прервана", 2: "сбой сети",
      3: "ошибка декодирования", 4: "формат не поддерживается"
    };
    showError("Видео не воспроизводится", codes[e.code] || ("код " + e.code));
  });

  attach();
  setInterval(poll, 250);
  setInterval(report, 2000);
  report();
})();
"""


def render(session: Session, base: str, hls_js_url: str) -> str:
    """HTML страницы-плеера для одной сессии.

    base — адрес прокси (например http://127.0.0.1:9000). Из него строится
    всё остальное, поэтому игре не нужно знать ничего, кроме этого адреса.
    """
    title = html.escape(session.title or "Видео")
    script = (
        SCRIPT.replace("__SID__", _js(session.sid))
        .replace("__BASE__", _js(base))
        .replace("__LIVE__", "true" if session.stream and session.stream.is_live else "false")
    )
    styles = BASE_CSS.replace("</", "<\\/")  # безопасность: не закрыть тег внутри
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{styles}</style>
</head>
<body>
<div id="wrap">
  <video id="v" playsinline preload="auto"></video>
  <button id="bigplay" type="button" aria-label="Запустить">{PLAY_ICON}</button>
  <div id="toast"></div>
  <div id="err"></div>
</div>
<script src="{html.escape(hls_js_url)}"></script>
<script>{script}</script>
</body>
</html>
"""


def _js(value: str) -> str:
    """Строка как JS-литерал. Кавычки и переводы строк экранируются."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def stream_url(sid: str, base: str) -> str:
    """Адрес master-плейлиста для этой сессии.

    Не ссылка источника напрямую: через мост, иначе протухший токен CDN
    остановит видео через час.
    """
    return f"{base}/stream/{urllib.parse.quote(sid)}"
