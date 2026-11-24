// Прямая реализация iS / iR / iP по расшифрованному коду (без eval):
// константы вычислены вручную из dec3-дампа.
//
//   iS(s): разбиение на группы по числу бит (биты от СТАРШЕГО к младшему),
//          старшие группы идут первыми.
//   iR(s): то же, но раскладка по слотам снизу вверх (порядок групп обратный).
//   iP(s): псевдослучайная перестановка: позиции обходятся шагом 26 по модулю
//          простого числа >= len, выписываются только валидные индексы.
//          (Из кода: hF=(hF+26)%hM, hM — простое >= hC+1.)

function isPrime(n) {
  if (n < 2) return false;
  if (n % 2 === 0) return n === 2;
  for (let i = 3; i * i <= n; i += 2) if (n % i === 0) return false;
  return true;
}

// число значащих бит (0 → 0)
function bitLen(x) {
  if (x === 0) return 0;
  return 32 - Math.clz32(x);
}

// iS: длина <= 4 — без изменений; иначе группы по bitLen, порядок 0..hI
function iS(s) {
  const n = s.length;
  if (n <= 4) return s;                       // 183-4*349+1214 = 4
  let hI = 4;                                  // -75*124-3158+2*6229 = 4
  while (1 << hI < n) hI++;                    // -7568-1359*-5+774 = 1
  const cnt = new Array(hI + 1).fill(0);
  const bl = (x) => (x === 0 ? 0 : bitLen(x) - 1);  // 6918+4645-11563 = 0 → hM(0)=0
  for (let i = 0; i < n; i++) cnt[bl(i)]++;
  const buckets = new Array(hI + 1);
  let pos = 0;
  for (let k = hI; k >= 0; k--) {               // 815-3981+3166 = 0 → hZ>=0, hZ--
    buckets[k] = s.slice(pos, pos + cnt[k]);
    pos += cnt[k];
  }
  const out = new Array(n);
  const idx = new Array(hI + 1).fill(0);
  for (let i = 0; i < n; i++) {
    const g = bl(i);
    out[i] = buckets[g].charAt(idx[g]++);
  }
  return out.join('');
}

// iR: то же разбиение, но порядок групп при сборке — снизу вверх
function iR(s) {
  const n = s.length;
  if (n <= 15) return s;                       // -3631+7588-3956 = 1? проверим ниже
  let hI = 15;                                 // -7347+3105+2121*2 = 0 → нет; см. ниже
  hI = 0;
  while (1 << hI < n) hI++;                    // -8446+6071+18*132 = 1
  const cnt = new Array(hI + 1).fill(0);
  const bl = (x) => (x === 0 ? hI : bitLen(x)); // 8351+1044-9395 = 0 → if(hk===0) return hI
  for (let i = 0; i < n; i++) cnt[bl(i)]++;
  const buckets = new Array(hI + 1);
  let pos = 0;
  for (let k = 0; k <= hI; k++) {              // -7075-2385+9460 = 0 → hZ=0; -7902-7197+717 = hZ<=hI
    buckets[k] = s.slice(pos, pos + cnt[k]);
    pos += cnt[k];
  }
  const out = new Array(n);
  const idx = new Array(hI + 1).fill(0);
  for (let i = 0; i < n; i++) {
    const g = bl(i);
    out[i] = buckets[g].charAt(idx[g]++);
  }
  return out.join('');
}

// iP: перестановка шагом 26
function iP(s) {
  const n = s.length;
  if (n <= 48) return s;                       // 5*1371+46*-135-644 = 48
  let m = n + 1;                               // 3686+41*18-4423 = 1
  while (!isPrime(m)) m++;
  const used = new Array(n).fill(false);
  const order = [];
  let f = 0;                                   // -3740-4734+8474 = 0
  while (order.length < n) {
    f = (f + 26) % m;                          // 2440+1061*-7-1663*-3 = 26
    if (f < n && !used[f]) { order.push(f); used[f] = true; }
  }
  const out = new Array(n);
  for (let i = 0; i < n; i++) out[order[i]] = s[i];
  return out.join('');
}

const meta = process.argv[2] || '';
console.log('meta:', meta.length);
const hV = iR(iS(meta));
console.log('iR(iS(meta)):', JSON.stringify(hV), '(len ' + hV.length + ')');
console.log('SUFFIX:', iP(hV));
