// Извлекаем из app.js рабочий пул строк: загружаем a0Q, a0y и IIFE-прокрутку
// в node «как есть», чтобы получить массив ровно в том состоянии, в котором
// его видит плеер после самозащиты.
const fs = require('fs');
const path = '/tmp/pl_app.c63ca8da.js';
const src = fs.readFileSync(path, 'utf8');

const start = src.indexOf('function a0Q()');
const rotStart = src.indexOf('}(function(Q,y){var d=Q();', start);
const rotEnd = src.indexOf('}(a0Q,', rotStart);
const rotClose = src.indexOf('));', rotEnd);
if (start < 0 || rotStart < 0 || rotClose < 0) throw new Error('не нашли блок a0Q/a0y/IIFE');

const bootstrap = src.slice(start, rotClose + 3);
console.log('длина bootstrap:', bootstrap.length);

// globalThis как window: код обращается к self/window только вне этих кусков
const vm = require('vm');
const ctx = { self: {}, window: {}, parseInt, console };
vm.createContext(ctx);
vm.runInContext(bootstrap + '\n; globalThis.__pool = a0Q(); globalThis.__a0y = a0y;', ctx);

const pool = ctx.__pool;
console.log('элементов после прокрутки:', pool.length);
console.log('первый:', JSON.stringify(pool[0]));
const L0 = JSON.parse(fs.readFileSync('/tmp/pool.json', 'utf8'));
// определяем величину сдвига: pool сдвинут относительно L0
let shift = -1;
for (let k = 0; k < L0.length; k++) {
  if (pool[0] === L0[k]) { shift = k; break; }
}
console.log('сдвиг (относительно pool.json):', shift, shift >= 0 ? '' : '(L0 — исходный литерал)');

fs.writeFileSync('/tmp/pool_rot.json', JSON.stringify(pool));

// быстрые обёртки
function a0qk(Q, y) { return ctx.__a0y(y - -0x22b, Q); }
function qY(Q, y) { return a0qk(Q, y - -0x56); }
ctx.a0qk = a0qk; ctx.qY = qY;

// проверка якорей
console.log('qY(0x1da,0x509) =', JSON.stringify(qY(0x1da, 0x509)), '(ждём consoleLog)');
console.log('a0qk(-0x1a8,0x1f6) =', JSON.stringify(a0qk(-0x1a8, 0x1f6)), '(ждём webpackChunk)');
