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
import json
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

/* ---------- панель управления ----------
   Зачем она в CEF: в игре нет ни клавиатуры, ни системных кнопок браузера.
   Без своих элементов человек получает картинку без паузы и перемотки —
   в v1 они были, в v2 потерялись при переписывании.

   Панель всплывает по движению мыши и прячется через 2.5 с, чтобы не
   закрывать кино. Пока курсор над ней или идёт перетаскивание полосы —
   не прячется. */
#ui {
  position: absolute; left: 0; right: 0; bottom: 0;
  padding: 10px 16px 12px;
  background: linear-gradient(to top, #0B0E14F2 0%, #0B0E14C0 60%, #0B0E1400 100%);
  opacity: 0; transform: translateY(6px); transition: opacity .18s, transform .18s;
  pointer-events: none;
}
/* pointer-events: auto ставится на САМУ панель, а не наследуется:
   без этого видео под ней перехватывает клики (проверено в Chromium —
   Playwright честно сказал «video intercepts pointer events»), и в игре
   кнопки были бы недоступны вовсе. */
#ui.on { opacity: 1; transform: none; pointer-events: auto; }
#ui.on * { pointer-events: auto; }
#ui.hidden { display: none; }

/* полоса перемотки: тонкая, но с широкой зоной захвата — в игре мышью
   попасть в 4 пикселя невозможно */
#bar { position: relative; height: 18px; cursor: pointer; margin-bottom: 2px; }
#bar::before {
  content: ""; position: absolute; left: 0; right: 0; top: 6px; height: 6px;
  /* Тёмная кайма обязательна: светлые кадры (небо, снег, вспышки) иначе
     съедают полосу целиком — проверено скриншотом, её не было видно. */
  background: #39404F; border-radius: 3px; box-shadow: 0 0 0 1px #0B0E1480;
}
#buffered {
  position: absolute; left: 0; top: 6px; height: 6px; width: 0;
  background: #5A6478; border-radius: 3px;
}
#played {
  position: absolute; left: 0; top: 6px; height: 6px; width: 0;
  background: #4D8DFF; border-radius: 3px;
}
#knob {
  position: absolute; top: 3px; width: 14px; height: 14px; margin-left: -7px;
  background: #E8E8EE; border-radius: 50%; box-shadow: 0 0 0 2px #0B0E14;
}
#bar.live #played, #bar.live #knob { display: none; }
#bar.live::after {
  content: "ПРЯМОЙ ЭФИР"; position: absolute; left: 0; top: -2px;
  font-size: 11px; letter-spacing: .06em; color: #F87171;
}

#row { display: flex; align-items: center; gap: 10px; font-size: 13px; }
#row .btn {
  position: relative; width: 34px; height: 30px; flex: 0 0 auto;
  display: flex; align-items: center; justify-content: center;
  background: transparent; border: 0; border-radius: 6px;
  color: #CBD2E4; cursor: pointer; padding: 0;
}
#row .btn:hover { background: #2A2E38; color: #4D8DFF; }
#row .btn svg { width: 20px; height: 20px; fill: currentColor; }
#time { font-variant-numeric: tabular-nums; color: #9AA0B4; white-space: nowrap; }
#time b { color: #CBD2E4; font-weight: 600; }
#spacer { flex: 1 1 auto; }

/* громкость: иконка + ползунок, ползунок раскрывается при наведении */
#volwrap { display: flex; align-items: center; gap: 6px; }
#vol { width: 0; opacity: 0; transition: width .15s, opacity .15s; }
#volwrap:hover #vol, #vol:focus { width: 80px; opacity: 1; }
#vol, input[type=range] { -webkit-appearance: none; appearance: none; height: 4px;
  background: #39404F; border-radius: 2px; outline: none; }
#vol::-webkit-slider-thumb { -webkit-appearance: none; width: 12px; height: 12px;
  border-radius: 50%; background: #4D8DFF; cursor: pointer; }

/* CEF внутри GTA 5 не всегда умеет Fullscreen API. Тогда полноэкранный
   режим — это класс clean: панель убирается, курсор скрывается, картинка
   на весь экран. Без этих правил кнопка «на весь экран» в игре не давала
   бы ничего. */
body.clean #ui { display: none; }
body.clean #wrap { cursor: none; }

#badge {
  position: absolute; right: 16px; top: 14px; padding: 4px 9px;
  background: #0B0E14C0; border: 1px solid #39404F; border-radius: 4px;
  font-size: 12px; color: #9AA0B4; opacity: 0; transition: opacity .5s;
}
#badge.on { opacity: 1; }
"""

PLAY_ICON = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>'
PAUSE_ICON = '<svg viewBox="0 0 24 24"><path d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>'
BACK_ICON = (
    '<svg viewBox="0 0 24 24"><path d="M12 5V1L7 6l5 5V7a5 5 0 1 1-5 5H5a7 7 0 1 0 7-7z"/></svg>'
)
FWD_ICON = (
    '<svg viewBox="0 0 24 24"><path d="M12 5V1l5 5-5 5V7a5 5 0 1 0 5 5h2a7 7 0 1 1-7-7z"/></svg>'
)
VOL_ICON = (
    '<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 4V5L7 9H3zm13.5 3a4.5 4.5 0 0 0-2.5-4v8'
    'a4.5 4.5 0 0 0 2.5-4zM14 3.2v2.1a7 7 0 0 1 0 13.4v2.1a9 9 0 0 0 0-17.6z"/></svg>'
)
MUTE_ICON = (
    '<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 4V5L7 9H3zm13.6 3 2.7-2.7-1.4-1.4L15.2 10.6 '
    '12.5 7.9l-1.4 1.4 2.7 2.7-2.7 2.7 1.4 1.4 2.7-2.7 2.7 2.7 1.4-1.4-2.7-2.7z"/></svg>'
)
FULL_ICON = (
    '<svg viewBox="0 0 24 24"><path d="M4 4h6v2H6v4H4V4zm10 0h6v6h-2V6h-4V4z'
    'M4 14h2v4h4v2H4v-6zm14 0h2v6h-6v-2h4v-4z"/></svg>'
)

SKIP_BACK_S = 10
"""Сколько секунд отматывает кнопка назад. 10 — привычно для телевизора
и удобно пальцем на тачпаде; 5 слишком мало, 30 слишком грубо."""

SKIP_FWD_S = 10
"""Столько же вперёд. Симметрия: человек запоминает одно число."""

START_VOLUME = 0.2
"""Громкость при открытии страницы — 20 %, а не 100 %.

Жалоба дословно: «громкость 100 %, бьёт по ушам». Причина не в плеере как
таком: игра запущена поверх видео, человек не ждёт звука и получает его на
полной. 20 % слышно, но не пугает; дальше крутится ползунком.
"""

# Скрипт страницы. Подставляются только SID и BASE — остальное статично.
SCRIPT = """
(function () {
  var SID = __SID__;
  var BASE = __BASE__;
  // Прямой эфир: у live нет длительности, перемотка бессмысленна, а hls.js
  // должен держаться края плейлиста, иначе отставание растёт на глазах.
  var LIVE = __LIVE__;
  var DIRECT = __DIRECT__;
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
  // Что ЧЕЛОВЕК хочет со звуком. Отдельно от `v.muted`, потому что браузер
  // волен поменять `v.muted` сам (политика автовоспроизведения), и раньше
  // это молча затирало выбор человека: «нажал Звук — включилось, нажал Play
  // — звук пропал».
  var wantSound = true;
  var soundBlocked = false;

  // Поставить звук по желанию человека и ПРОВЕРИТЬ результат.
  function setSound(on, why) {
    wantSound = !!on;
    if (on) {
      if (v.volume === 0) v.volume = __START_VOL__;
      v.muted = false;
    } else {
      v.muted = true;
    }
    paint();
    trace("звук " + (on ? "вкл" : "выкл") + " (" + (why || "кнопка") + ")" +
          ": muted=" + v.muted + " volume=" + v.volume.toFixed(2) +
          " paused=" + v.paused);
    // Chromium ставит видео на паузу, если звук включили без действия
    // человека. Проверяем через мгновение и честно сообщаем, что вышло.
    setTimeout(checkSound, 350);
  }

  function checkSound() {
    if (v.muted) { soundBlocked = false; return; }   // выключили — так и надо
    if (!v.paused) { soundBlocked = false; return; }  // играет со звуком — хорошо
    soundBlocked = true;
    trace("звук: браузер поставил на паузу (политика автовоспроизведения) — " +
          "продолжаю без звука");
    v.muted = true;
    startPlayback();
    toast("Звук не разрешён браузером — нажмите на картинку");
  }

  function startPlayback() {
    var p = v.play();
    if (!p || !p.catch) return;
    p.catch(function () {
      // Автовоспроизведение со звуком заблокировано политикой браузера.
      // Играем БЕЗ звука, но НЕ оставляем человека с тишиной навсегда: в
      // игре нет «первого клика по странице», поэтому снятие mute по клику
      // (как в браузере) там не наступает никогда. Вместо этого пробуем
      // включить звук сами, как только политика позволит: `play()` уже
      // отработал, и повторное снятие mute не ставит видео на паузу —
      // пауза и была жалобой «нажал Звук — видео встало».
      v.muted = true;
      // Именно здесь раньше терялся звук: catch глушил видео ВСЕГДА, даже
      // когда человек только что сам просил «звук включить». Теперь выбор
      // помним в `wantSound`, а в журнал пишем, что именно произошло.
      trace("play() отклонён браузером — продолжаю без звука" +
            (wantSound ? ", звук просили включить" : ""));
      var p2 = v.play();
      if (p2 && p2.catch) p2.catch(function () {
        bigplay.style.display = "flex";
        toast("Нажмите на экран");
      });
      // Клик по экрану — штатный путь браузера; в игре его просто не будет.
      if (!unmuteOnClick) {
        unmuteOnClick = true;
        document.addEventListener("click", function unmuteOnce() {
          document.removeEventListener("click", unmuteOnce);
          unmuteOnClick = false;
          v.muted = false;
          paint();
        });
      }
    });
  }

  bigplay.addEventListener("click", function () {
    bigplay.style.display = "none";
    v.muted = false;
    startPlayback();
  });

  // ---------------- загрузка потока ----------------
  // DIRECT = true: качества это прямые mp4-файлы, а не HLS-плейлисты.
  // hls.js такие «варианты» не разбирает и не играет ВООБЩЕ ничего, без
  // ошибки в консоли (проверено на anidub.org: видео молча стояло на 0).
  // Поэтому здесь hls.js не используется: качество меняем сами, подменой src.
  var inited = false;
  var levels = [];          // [{label, url}] — при DIRECT
  var currentLevel = -1;

  function switchLevel(index) {
    if (index < 0 || index >= levels.length || index === currentLevel) return;
    var wasPlaying = !v.paused;
    var at = v.currentTime;
    currentLevel = index;
    v.src = levels[index].url;
    // Возврат на ту же секунду: человек включил 1080p, чтобы смотреть
    // дальше, а не чтобы начать сначала.
    v.addEventListener("loadedmetadata", function once() {
      v.removeEventListener("loadedmetadata", once);
      try { v.currentTime = at; } catch (e) {}
      if (wasPlaying) startPlayback();
      toast("Качество: " + levels[index].label);
    });
  }

  function attach() {
    if (DIRECT) {
      levels = JSON.parse(document.getElementById("levels").textContent || "[]");
      if (!levels.length) {
        showError("Нет доступных качеств", "источник не отдал ни одного варианта");
        return;
      }
      currentLevel = 0;   // первый в списке — лучшее качество
      v.src = levels[0].url;
      v.addEventListener("loadedmetadata", function () {
        hideError();
        // Настоящий размер кадра важнее обещанного: источник мог соврать.
        if (v.videoHeight) levels[currentLevel].label = v.videoHeight + "p";
        startPlayback();
      });
      if (LIVE) bar.classList.add("live");
      return;
    }
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
        // В журнал — ЛЮБАЯ ошибка, включая нефатальные: «ошибка декодирования»
        // у hls.js проходит как MEDIA_ERROR и часто не фатальна, поэтому
        // раньше она не попадала никуда и жалобу не с чем было сопоставить.
        trace("HLS " + (data.fatal ? "fatal " : "") + reason + code +
              " t=" + (v.currentTime || 0).toFixed(1));
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
  // Каждая полученная команда пишется в журнал на сервере: без этого по
  // жалобе «нажал, а ничего не произошло» невозможно понять, где обрыв —
  // пульт не отправил, сервер не отдал или плеер не применил.
  function trace(text) {
    try {
      fetch(BASE + "/trace/" + SID, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: String(text).slice(0, 300) })
      }).catch(function () {});
    } catch (e) {}
  }

  function applyCommand(c) {
    var val = c.value;
    var applied = true;
    trace("плеер получил " + c.cmd + "=" + val);
    switch (c.cmd) {
      case "play":       startPlayback(); break;
      case "pause":      v.pause(); break;
      // Play/пауза НЕ трогает звук. Раньше здесь стояло `v.muted = false`:
      // человек выключил звук, нажал Play — и звук включился сам. Жалоба
      // «кнопка звука и плея не работают как надо».
      case "toggle":
        trace("toggle: было paused=" + v.paused + " muted=" + v.muted);
        if (v.paused) { startPlayback(); } else { v.pause(); }
        break;
      case "seek":
        if (LIVE) { toast("В прямом эфире перемотки нет"); break; }
        v.currentTime = Number(val) || 0;
        break;
      case "seekRel":
        if (LIVE) { toast("В прямом эфире перемотки нет"); break; }
        var d = Number.isFinite(v.duration) ? v.duration : 1e9;
        v.currentTime = Math.max(0, Math.min(d, v.currentTime + (Number(val) || 0)));
        break;
      case "volume":     v.volume = Math.max(0, Math.min(1, Number(val)));
                         if (v.volume > 0) v.muted = false; break;
      // «Включи звук»: снимаем mute и, если пришлось играть без звука из-за
      // политики автовоспроизведения, разрешаем его навсегда. В игре нет
      // «первого клика по странице», поэтому обработчик `unmuteOnce` там
      // не сработает НИКОГДА — звук оставался выключенным до перезапуска.
      case "sound":      v.muted = false;
                         if (v.volume === 0) v.volume = 0.2;
                         unmuteOnClick = false;
                         if (v.paused) startPlayback(); break;
      case "mute":
        // Одно место правды о звуке: ставит `v.muted`, помнит желание
        // человека и проверяет, не поставил ли браузер паузу.
        setSound(!(val === true || val === "true" || val === 1), "пульт");
        break;
      case "normalize":  setNormalize(val === true || val === "true" || val === 1); break;
      case "quality":
        if (DIRECT) {
          var want = String(val || "").replace(/[^0-9]/g, "");
          for (var i = 0; i < levels.length; i++) {
            if (String(levels[i].height) === want) { switchLevel(i); return; }
            }
          return;
        }    setQuality(Number(val)); break;
      case "subs":       setSubtitles(Number(val)); break;
      case "trans":      pickTranslation(Number(val)); break;
      case "restart":    v.currentTime = 0; startPlayback(); break;
      // Серия/озвучка — это НОВЫЙ поток на стороне прокси, а не дорожка в
      // плеере: страница обязана перечитать её. Раньше перезагрузку делала
      // только смена озвучки (`pickTranslation`), а после смены серии
      // страница продолжала играть старую — жалоба «серии не переключаются».
      case "reload":     setTimeout(function () { location.reload(); }, 300); break;
      case "fullscreen":
        // Ровно то же, что делает кнопка в самом плеере (`actFull`):
        // Fullscreen API в CEF игры обычно не работает, поэтому «во весь
        // экран» — это класс `clean` (убирает панель и курсор) плюс показ
        // панели, чтобы человек видел, что нажатие сработало.
        var wantFull = (val === true || val === "true" || val === 1);
        if (wantFull) {
          if (document.documentElement.requestFullscreen) {
            document.documentElement.requestFullscreen().catch(function () {});
          }
          document.body.classList.add("clean");
        } else {
          if (document.exitFullscreen) document.exitFullscreen().catch(function () {});
          document.body.classList.remove("clean");
        }
        showUI();
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

  // Субтитры: дорожки лежат в <track>, переключаем активную.
  // val = 0 — выключить, val = N — включить дорожку N-1 (с нуля у пульта).
  function setSubtitles(index) {
    var tracks = v.textTracks;
    for (var i = 0; i < tracks.length; i++) {
      tracks[i].mode = (index > 0 && i === index - 1) ? "showing" : "disabled";
    }
    if (index > 0) {
      var name = tracks[index - 1] ? (tracks[index - 1].label || "Субтитры") : "Субтитры";
      toast(name);
    } else {
      toast("Субтитры выключены");
    }
    // запоминаем выбор: после перезагрузки страницы вернём его же
    try { localStorage.setItem("subs_" + SID, String(index)); } catch (e) {}
  }

  function restoreSubtitles() {
    var saved = 0;
    try { saved = Number(localStorage.getItem("subs_" + SID) || 0); } catch (e) {}
    if (saved > 0) setTimeout(function () { setSubtitles(saved); }, 500);
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

  // ---------------- панель управления ----------------
  // В игре нет ни клавиатуры, ни браузерных кнопок: всё, что можно сделать
  // с видео, должно быть здесь. Панель всплывает по движению мыши, прячется
  // сама через 2.5 с, но не прячется, пока на ней курсор или пока тянут
  // полосу перемотки.
  var ui = document.getElementById("ui");
  var bar = document.getElementById("bar");
  var played = document.getElementById("played");
  var buffered = document.getElementById("buffered");
  var knob = document.getElementById("knob");
  var timeEl = document.getElementById("time");
  var playBtn = document.getElementById("b-play");
  var muteBtn = document.getElementById("b-mute");
  var volInput = document.getElementById("vol");
  var badge = document.getElementById("badge");
  // Иконки берутся из скрытых <template>, а не подстановкой в текст скрипта:
  // SVG внутри содержит двойные кавычки, и в JS-литерале "…" он рвёт строку
  // (проверено: SyntaxError «Unexpected number», панель молча не работала).
  var ICONS = {};
  (function () {
    var nodes = document.querySelectorAll("template[data-icon]");
    for (var i = 0; i < nodes.length; i++) {
      ICONS[nodes[i].getAttribute("data-icon")] = nodes[i].innerHTML;
    }
  })();
  var hideTimer = null;
  var hovering = false;
  var dragging = false;

  if (LIVE) { bar.classList.add("live"); }

  function fmt(sec) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s2 = Math.floor(sec % 60);
    var mm = (h && m < 10 ? "0" : "") + m;
    var ss = (s2 < 10 ? "0" : "") + s2;
    return h ? (h + ":" + mm + ":" + ss) : (m + ":" + ss);
  }

  function showUI() {
    ui.classList.add("on");
    clearTimeout(hideTimer);
    if (hovering || dragging) return;
    hideTimer = setTimeout(function () {
      if (!hovering && !dragging && !v.paused) ui.classList.remove("on");
    }, 2500);
  }

  // Движение мыши — единственный надёжный признак «человек рядом».
  // Пауза или наведение на панель держат её открытой.
  document.addEventListener("mousemove", showUI);
  ui.addEventListener("mouseenter", function () { hovering = true; showUI(); });
  ui.addEventListener("mouseleave", function () { hovering = false; showUI(); });
  v.addEventListener("pause", function () { showUI(); });
  v.addEventListener("play", function () { showUI(); });

  function paint() {
    var dur = LIVE ? 0 : (isFinite(v.duration) ? v.duration : 0);
    var pct = dur ? Math.min(100, (v.currentTime / dur) * 100) : 0;
    played.style.width = pct + "%";
    knob.style.left = pct + "%";
    bar.setAttribute("aria-valuenow", String(Math.round(pct)));
    if (!LIVE) {
      timeEl.innerHTML = "<b>" + fmt(v.currentTime) + "</b> / " + fmt(dur);
    } else {
      timeEl.innerHTML = "<b>" + fmt(v.currentTime) + "</b>";
    }
    var buf = 0;
    try {
      for (var i = 0; i < v.buffered.length; i++) {
        if (v.buffered.start(i) <= v.currentTime && v.currentTime <= v.buffered.end(i)) {
          buf = v.buffered.end(i); break;
        }
      }
    } catch (e) {}
    buffered.style.width = (dur ? Math.min(100, (buf / dur) * 100) : 0) + "%";
    playBtn.innerHTML = v.paused ? ICONS.play : ICONS.pause;
    muteBtn.innerHTML = (v.muted || v.volume === 0) ? ICONS.mute : ICONS.vol;
    if (!dragging) volInput.value = String(Math.round((v.muted ? 0 : v.volume) * 100));
  }

  // Стартовая громкость — из константы, а не из «браузер поставил 100 %».
  // Проверка `muted` не сбрасывается: если сервер прислал mute, он главнее.
  v.volume = Math.max(0, Math.min(1, Number(__START_VOL__) || 0.2));

  setInterval(paint, 250);
  v.addEventListener("timeupdate", paint);
  v.addEventListener("loadedmetadata", paint);

  // ---- перемотка ----
  function seekToClientX(clientX) {
    var rect = bar.getBoundingClientRect();
    if (!rect.width) return;
    var ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    if (LIVE) { toast("В прямом эфире перемотки нет"); return; }
    var dur = isFinite(v.duration) ? v.duration : 0;
    if (dur) v.currentTime = ratio * dur;
    paint();
    badge.textContent = fmt(ratio * dur);
    badge.classList.add("on");
  }

  bar.addEventListener("mousedown", function (e) {
    if (LIVE) { toast("В прямом эфире перемотки нет"); return; }
    dragging = true; showUI(); seekToClientX(e.clientX);
    // Слушатели на document, а не на полосе: если увести курсор за её
    // пределы, перетаскивание не должно обрываться — все так делают.
    function onMove(ev) { seekToClientX(ev.clientX); }
    function onUp() {
      dragging = false;
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      setTimeout(function () { badge.classList.remove("on"); }, 600);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  // ---- кнопки ----
  function actBack() { if (LIVE) { toast("В прямом эфире перемотки нет"); return; }
    v.currentTime = Math.max(0, v.currentTime - __BACK__); showUI(); }
  function actFwd() { if (LIVE) { toast("В прямом эфире перемотки нет"); return; }
    var dur = isFinite(v.duration) ? v.duration : 1e9;
    v.currentTime = Math.min(dur, v.currentTime + __FWD__); showUI(); }

  function actToggle() {
    // Звук не трогаем: плей/пауза — только про воспроизведение.
    if (v.paused) { startPlayback(); } else { v.pause(); }
    showUI();
  }

  function actMute() {
    var on = v.muted;   // было выключено — включаем
    setSound(on, "кнопка плеера");
    toast(on ? "Звук включён" : "Звук выключен");
  }

  function actFull() {
    var want = !document.fullscreenElement && !document.body.classList.contains("clean");
    if (want) {
      var el = document.documentElement;
      if (el.requestFullscreen) el.requestFullscreen().catch(function () {});
      // CEF в игре не всегда поддерживает Fullscreen API: тогда «во весь
      // экран» — это просто убрать панель и курсор.
      document.body.classList.add("clean");
    } else {
      if (document.exitFullscreen) document.exitFullscreen().catch(function () {});
      document.body.classList.remove("clean");
    }
    showUI();
  }

  ui.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-act]") : null;
    if (!btn) return;
    switch (btn.getAttribute("data-act")) {
      case "toggle": actToggle(); break;
      case "back":   actBack();   break;
      case "fwd":    actFwd();    break;
      case "mute":   actMute();   break;
      case "full":   actFull();   break;
    }
  });

  volInput.addEventListener("input", function () {
    v.volume = Math.max(0, Math.min(1, Number(volInput.value) / 100));
    v.muted = v.volume === 0;
    paint();
  });

  // Клик по картинке — пауза. Так ведёт себя любой телевизор; при этом
  // не срабатывает, если кликнули по самой панели.
  v.addEventListener("click", function () { actToggle(); });

  // Клавиатура: в CEF работает, если фокус на странице. Не мешает —
  // в игре её просто нет, а при отладке в браузере удобно.
  document.addEventListener("keydown", function (e) {
    if (e.target && /INPUT|TEXTAREA/.test(e.target.tagName)) return;
    if (e.code === "Space" || e.key === "k") { e.preventDefault(); actToggle(); }
    else if (e.key === "ArrowLeft")  { e.preventDefault(); actBack(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); actFwd(); }
    else if (e.key === "f") { e.preventDefault(); actFull(); }
    else if (e.key === "m") { e.preventDefault(); actMute(); }
  });

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
        subtitles: v.textTracks ? v.textTracks.length : 0,
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

  // Ошибка самого элемента <video>: сюда приходит «не воспроизводится»
  // у прямых mp4 (anidub.org) — код 3 = MEDIA_ERR_DECODE.
  v.addEventListener("error", function () {
    var e = v.error;
    if (!e) return;
    trace("video.error код=" + e.code + " (" + (e.message || "без текста") + ") t=" +
          (v.currentTime || 0).toFixed(1) + " src=" + String(v.currentSrc || "").slice(0, 60));
    var codes = {
      1: "загрузка прервана", 2: "сбой сети",
      3: "ошибка декодирования", 4: "формат не поддерживается"
    };
    showError("Видео не воспроизводится", codes[e.code] || ("код " + e.code));
  });

  attach();
  restoreSubtitles();
  setInterval(poll, 250);
  setInterval(report, 2000);
  report();
})();
"""


def levels_json(session: object) -> str:
    """Список качеств для режима прямых файлов (mp4).

    Почему JSON в странице, а не отдельный запрос: список нужен плееру
    ДО первого кадра, а гонять лишний HTTP в CEF ради пяти строк — это
    лишняя секунда к старту.

    Ссылки идут через наш мост: подпись CDN привязана к IP, и браузер
    игры не должен ходить к VK напрямую.
    """
    stream = getattr(session, "stream", None)
    if stream is None or not getattr(stream, "is_direct_file", False):
        return "[]"
    items = []
    for quality in stream.qualities:
        items.append(
            {
                "height": quality.height,
                "label": quality.label,
                "url": "/hls?url=" + urllib.parse.quote(quality.url, safe=""),
            }
        )
    return json.dumps(items, ensure_ascii=False)


def render(session: Session, base: str, hls_js_url: str) -> str:
    """HTML страницы-плеера для одной сессии.

    base — адрес прокси (например http://127.0.0.1:9000). Из него строится
    всё остальное, поэтому игре не нужно знать ничего, кроме этого адреса.
    """
    title = html.escape(session.title or "Видео")
    tracks = _tracks(session, base)
    script = (
        SCRIPT.replace("__SID__", _js(session.sid))
        .replace("__BASE__", _js(base))
        .replace("__LIVE__", "true" if session.stream and session.stream.is_live else "false")
        .replace(
            "__DIRECT__",
            "true" if session.stream and session.stream.is_direct_file else "false",
        )
        .replace("__LEVELS__", levels_json(session))
        .replace("__BACK__", str(SKIP_BACK_S))
        .replace("__FWD__", str(SKIP_FWD_S))
        .replace("__START_VOL__", f"{START_VOLUME:.2f}")
        .replace("__START_VOL_PCT__", str(round(START_VOLUME * 100)))
    )
    styles = BASE_CSS.replace("</", "<\\/")  # безопасность: не закрыть тег внутри
    markup = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{styles}</style>
</head>
<body>
<div id="wrap">
  <video id="v" playsinline preload="auto">{tracks}</video>
  <button id="bigplay" type="button" aria-label="Запустить">@PLAY@</button>
  <!-- Иконки для JS: внутри скрипта SVG не поставить, кавычки рвут литерал -->
  <script type="application/json" id="levels">__LEVELS__</script>
  <template data-icon="play">@PLAY@</template>
  <template data-icon="pause">@PAUSE@</template>
  <template data-icon="mute">@MUTE@</template>
  <template data-icon="vol">@VOL@</template>
  <div id="badge"></div>
  <div id="toast"></div>
  <div id="err"></div>
  <div id="ui" data-ui="panel">
    <div id="bar" data-ui="bar" role="slider" tabindex="0"
         aria-label="Перемотка" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
      <div id="buffered"></div><div id="played"></div><div id="knob"></div>
    </div>
    <div id="row">
      <button class="btn" id="b-play" data-act="toggle" type="button"
              aria-label="Пауза или продолжить">@PAUSE@</button>
      <button class="btn" id="b-back" data-act="back" type="button"
              aria-label="Назад на {SKIP_BACK_S} секунд">@BACK@</button>
      <button class="btn" id="b-fwd" data-act="fwd" type="button"
              aria-label="Вперёд на {SKIP_FWD_S} секунд">@FWD@</button>
      <span id="time"><b>0:00</b> / 0:00</span>
      <span id="spacer"></span>
      <div id="volwrap">
        <button class="btn" id="b-mute" data-act="mute" type="button"
                aria-label="Выключить звук">@VOL@</button>
        <input id="vol" type="range" min="0" max="100"
               value="__START_VOL_PCT__" aria-label="Громкость">
      </div>
      <button class="btn" id="b-full" data-act="full" type="button"
              aria-label="На весь экран">@FULL@</button>
    </div>
  </div>
</div>
<script src="{html.escape(hls_js_url)}"></script>
<script>{script}</script>
</body>
</html>
"""
    # __LEVELS__ живёт в РАЗМЕТКЕ (скрытый JSON для режима прямых файлов), а
    # не в скрипте — поэтому подставляется здесь. Раньше замены шли только
    # по SCRIPT, и плеер читал строку "__LEVELS__" как JSON: «is not valid
    # JSON», видео не стартовало вовсе.
    markup = markup.replace("__LEVELS__", levels_json(session))
    markup = markup.replace("__START_VOL_PCT__", str(round(START_VOLUME * 100)))
    # Иконки подставляются отдельным проходом, а не в f-строке: внутри SVG
    # есть фигурные скобки от стилей, и f-строка на них ломается. Один
    # проход по словарю читается лучше, чем цепочка .replace().
    for icon, value in (
        ("@PLAY@", PLAY_ICON),
        ("@PAUSE@", PAUSE_ICON),
        ("@BACK@", BACK_ICON),
        ("@FWD@", FWD_ICON),
        ("@VOL@", VOL_ICON),
        ("@MUTE@", MUTE_ICON),
        ("@FULL@", FULL_ICON),
    ):
        markup = markup.replace(icon, value)
    return markup


def _tracks(session: Session, base: str) -> str:
    """Теги <track> для всех дорожек субтитров.

    Все дорожки сразу в странице, но выключены: включение — команда `subs`.
    Так переключение мгновенное и не требует перезагрузки, а страница не
    ходит к источнику за каждой дорожкой.
    """
    stream = session.stream
    if stream is None or not stream.subtitles:
        return ""
    parts: list[str] = []
    for index, track in enumerate(stream.subtitles):
        lang = html.escape(str(track.get("lang") or "und"))
        label = html.escape(str(track.get("title") or lang))
        src = f"{base}/subs/{urllib.parse.quote(session.sid)}?i={index}"
        parts.append(
            f'<track kind="subtitles" src="{html.escape(src)}" srclang="{lang}" label="{label}">'
        )
    return "".join(parts)


def _js(value: str) -> str:
    """Строка как JS-литерал. Кавычки и переводы строк экранируются."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def stream_url(sid: str, base: str) -> str:
    """Адрес master-плейлиста для этой сессии.

    Не ссылка источника напрямую: через мост, иначе протухший токен CDN
    остановит видео через час.
    """
    return f"{base}/stream/{urllib.parse.quote(sid)}"
