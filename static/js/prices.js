// ── Product prices: filter, card badge, automatic reading ──
// Pages are read by the admin's own browser through the Chrome extension
// (anExtFetchPrices in analysis.js), never by the server.
const PRICE_PRESETS = [
  ['', 'الكل', 0, 0, ''], ['cheap', '🟢 رخيص (حتى 2500)', 0, 2500, ''],
  ['mid', '🟡 متوسط (2500–5000)', 2500, 5000, ''], ['high', '🔴 غالٍ (5000+)', 5000, 0, ''],
  ['none', 'بدون سعر', 0, 0, 'none']];
let priceState = '';

function priceVal(id) { return Math.max(0, parseInt((document.getElementById(id) || {}).value, 10) || 0); }
function priceFilterActive() { return !!(priceState || priceVal('price-min') || priceVal('price-max')); }

function priceFilterParams() {
  const min = priceVal('price-min'), max = priceVal('price-max');
  return (min ? `&price_min=${min}` : '') + (max ? `&price_max=${max}` : '') +
    (priceState ? `&price_state=${priceState}` : '');
}

function renderPriceBtns() {
  const box = document.getElementById('price-btns');
  if (!box) return;
  const min = priceVal('price-min'), max = priceVal('price-max');
  box.innerHTML = PRICE_PRESETS.map(([id, label, lo, hi, st]) => {
    const on = lo === min && hi === max && st === priceState;
    return `<button type="button" onclick="setPricePreset('${id}')" class="text-xs px-3 py-1.5 rounded-lg font-bold border transition ${on
      ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-slate-700 border-[#cfe0f5] hover:bg-[#eef6ff]'}">${label}</button>`;
  }).join('');
}

function setPricePreset(id) {
  const [, , lo, hi, st] = PRICE_PRESETS.find(x => x[0] === id);
  document.getElementById('price-min').value = lo || '';
  document.getElementById('price-max').value = hi || '';
  priceState = st;
  renderPriceBtns();
  loadAds();
}

function priceInputChanged() { priceState = ''; renderPriceBtns(); loadAds(); }

function priceLabel(price, source, status) {
  if (price) return `💰 ${Number(price).toLocaleString('en-US')} دج${source === 'manual' ? ' ✍️' : ''}`;
  return status === 'blocked' ? '🔒 سعر محجوب' : '💰 —';
}

function priceBadgeHtml(a) {
  if (!a.product_key) return '';
  const text = priceLabel(a.price_dzd, a.price_source, a.price_status);
  const tip = a.price_checked_at ? `آخر قراءة: ${String(a.price_checked_at).slice(0, 10)}` : 'لم يُقرأ السعر بعد';
  const attrs = `data-price-key="${escT(a.product_key)}" title="${escT(tip)}"`;
  return isAdmin()
    ? `<button type="button" ${attrs} data-price="${a.price_dzd || ''}" onclick="editPrice(this)" class="num font-bold text-emerald-700 hover:underline">${text}</button>`
    : `<span ${attrs} class="num font-bold text-emerald-700">${text}</span>`;
}

function paintPrice(key, price, source, status) {
  document.querySelectorAll('[data-price-key]').forEach(el => {
    if (el.dataset.priceKey !== key) return;
    el.textContent = priceLabel(price, source, status);
    el.dataset.price = price || '';
  });
}

async function editPrice(btn) {
  const key = btn.dataset.priceKey;
  const v = prompt('سعر المنتج بالدينار (اتركه فارغاً لحذفه وإعادة قراءته):', btn.dataset.price || '');
  if (v === null) return;
  try {
    const r = await fetch('/api/prices/manual', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key, price: v.replace(/[\s,]/g, '')})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'فشل الحفظ');
    paintPrice(key, d.price, d.price ? 'manual' : '', '');
  } catch (e) { alert(e.message); }
}

// ── automatic reading ──
let priceAutoOn = true;
try { priceAutoOn = localStorage.getItem('priceAuto') !== '0'; } catch (_e) {}
let priceAutoRunning = false;
let priceReadCount = 0;
const priceWait = ms => new Promise(r => setTimeout(r, ms));

function priceMsg(t) { const el = document.getElementById('price-auto-msg'); if (el) el.textContent = t; }

function priceAutoSet(on) {
  priceAutoOn = on;
  try { localStorage.setItem('priceAuto', on ? '1' : '0'); } catch (_e) {}
  if (on) priceAutoStart(); else priceMsg('متوقفة');
}

async function priceBatch() {
  const r = await fetch('/api/prices/queue?limit=10');
  if (!r.ok) throw new Error('تعذر جلب قائمة المنتجات');
  const q = await r.json();
  if (!q.items.length) { priceMsg(`لا منتجات بانتظار القراءة${q.blocked_hosts ? ` · ${q.blocked_hosts} متجر محجوب` : ''}`); return false; }
  priceMsg(`يقرأ الآن… متبقٍ ${q.waiting}${q.more ? '+' : ''} منتج`);
  // Two advertisers can share one page: open it once.
  const got = new Map((await anExtFetchPrices([...new Set(q.items.map(i => i.url))])).map(x => [x.url, x]));
  const results = q.items.filter(i => got.has(i.url)).map(i => {
    const x = got.get(i.url);
    return {key: i.key, price: x.price, how: x.how, status: x.status};
  });
  if (!results.length) throw new Error('الإضافة لم ترجع نتائج');
  const s = await fetch('/api/prices/results', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({results})});
  if (!s.ok) throw new Error('تعذر حفظ الأسعار');
  results.forEach(x => { if (x.price) { priceReadCount++; paintPrice(x.key, x.price, 'browser', x.status); } });
  priceMsg(`قُرئ ${priceReadCount} سعر · متبقٍ ${Math.max(0, q.waiting - results.length)}${q.more ? '+' : ''}${q.blocked_hosts ? ` · ${q.blocked_hosts} متجر محجوب` : ''}`);
  return true;
}

async function priceAutoLoop() {
  while (priceAutoOn && isAdmin()) {
    let pause = 5000;
    try { if (!(await priceBatch())) pause = 60000; }
    catch (e) { priceMsg('⚠️ ' + e.message); pause = 60000; }
    await priceWait(pause);
  }
}

async function priceAutoStart() {
  const box = document.getElementById('price-auto');
  if (!box || !isAdmin()) { if (box) box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  document.getElementById('price-auto-toggle').checked = priceAutoOn;
  if (!anExt) { priceMsg('🧩 تحتاج إضافة Chrome: ثبّتها أو حدّثها ثم أعد تحميل الصفحة'); return; }
  if (!priceAutoOn) { priceMsg('متوقفة'); return; }
  if (priceAutoRunning) return;
  priceAutoRunning = true;
  try {
    // One reading tab at a time.
    if (navigator.locks) {
      await navigator.locks.request('adscloud-prices', {ifAvailable: true},
        lock => lock ? priceAutoLoop() : priceMsg('القراءة تعمل في تبويب آخر'));
    } else await priceAutoLoop();
  } finally { priceAutoRunning = false; }
}

renderPriceBtns();
setInterval(() => { if (currentUser && !priceAutoRunning) priceAutoStart(); }, 5000);
