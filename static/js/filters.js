function setPreset(p) {
  currentPreset = p;
  ['all','winning','new_launches','active','stopped'].forEach(k => {
    const el = document.getElementById('preset-'+k);
    if (!el) return;
    if ((p==='' && k==='all') || p===k) {
      el.className = 'text-xs px-3.5 py-1.5 rounded-lg bg-blue-600 text-white font-bold shadow-sm transition';
    } else {
      el.className = 'text-xs px-3.5 py-1.5 rounded-lg bg-white text-slate-700 border border-[#cfe0f5] hover:bg-[#eef6ff] font-bold transition shadow-sm';
    }
  });
  loadAds();
}

function toggleFiltersPanel() {
  const p = document.getElementById('filters-panel');
  const open = p.classList.contains('hidden');
  p.classList.toggle('hidden', !open);
  const btn = document.getElementById('filters-toggle');
  if (btn) {
    btn.classList.toggle('bg-[#eef6ff]', open);
    btn.classList.toggle('border-blue-400', open);
  }
}

function updateFiltersCount() {
  let n = 0;
  if (currentPreset) n++;
  if (currentKeywords.length) n++;
  if (currentNiches.length) n++;
  const dmin = document.getElementById('start-date-min');
  const dmax = document.getElementById('start-date-max');
  if ((dmin && dmin.value) || (dmax && dmax.value)) n++;
  const mdays = document.getElementById('min-days');
  if (mdays && parseInt(mdays.value || 0) > 0) n++;
  if (typeof minProductAds !== 'undefined' && minProductAds > 1) n++;
  const badge = document.getElementById('filters-count');
  if (badge) {
    badge.innerText = n;
    badge.classList.toggle('hidden', n === 0);
  }
  const btn = document.getElementById('filters-toggle');
  if (btn) btn.classList.toggle('text-blue-700', n > 0);
}

// ── Keyword tags (which scrape keyword found each ad) ──
const KW_PALETTE = [
  'bg-rose-50 text-rose-700 border-rose-300',
  'bg-amber-50 text-amber-700 border-amber-300',
  'bg-emerald-50 text-emerald-700 border-emerald-300',
  'bg-sky-50 text-sky-700 border-sky-300',
  'bg-violet-50 text-violet-700 border-violet-300',
  'bg-orange-50 text-orange-700 border-orange-300',
  'bg-teal-50 text-teal-700 border-teal-300',
  'bg-fuchsia-50 text-fuchsia-700 border-fuchsia-300',
];
function kwTone(k) {
  let h = 0;
  for (let i = 0; i < k.length; i++) h = (h * 31 + k.charCodeAt(i)) >>> 0;
  return KW_PALETTE[h % KW_PALETTE.length];
}
function kwActive(k) {
  return currentKeywords.some(x => x.toLowerCase() === String(k).toLowerCase());
}
function keywordChipHtml(k, opts = {}) {
  const lbl = opts.label || k;
  const active = kwActive(k);
  const cnt = opts.count != null ? ` <span class="num opacity-70">${opts.count}</span>` : '';
  return `<button type="button" data-kw="${escT(k)}" onclick="toggleKeyword(this.dataset.kw)" title="${escT(opts.title || 'فلترة بهذه الكلمة')}"
    class="px-2.5 py-0.5 rounded-full border text-[11px] font-bold transition max-w-[240px] truncate ${kwTone(k)} ${active ? 'ring-2 ring-blue-400 ring-offset-1' : 'hover:brightness-95'}">🔍 ${escT(lbl)}${cnt}</button>`;
}
function adKeywordChips(a, opts = {}) {
  const list = (opts.group && a.group && a.group.keywords) || a.keywords || [];
  const ks = [...new Set(list.filter(Boolean))];
  if (!ks.length) return '';
  const max = 3;
  const shown = ks.slice(0, max);
  const rest = ks.length - shown.length;
  return `<div class="flex flex-wrap gap-1.5 mb-2.5">${shown.map(k => keywordChipHtml(k)).join('')}${
    rest > 0 ? `<span class="px-2 py-0.5 rounded-full border border-slate-200 bg-slate-50 text-slate-600 text-[11px] font-bold" title="${escT(ks.join(' · '))}">+${rest}</span>` : ''
  }</div>`;
}
function renderKeywordMenu() {
  const box = document.getElementById('kw-items');
  if (!box) return;
  const q = ((document.getElementById('kw-search') || {}).value || '').trim().toLowerCase();
  const items = kwMenuItems.filter(x => !q || String(x.keyword).toLowerCase().includes(q));
  box.innerHTML = items.length ? items.map(x => `
    <label class="flex items-center justify-between gap-2 px-2 py-1.5 rounded-lg hover:bg-slate-50 cursor-pointer text-xs">
      <span class="flex items-center gap-2 min-w-0">
        <input type="checkbox" data-kw="${escT(x.keyword)}" onclick="toggleKeyword(this.dataset.kw)" ${kwActive(x.keyword) ? 'checked' : ''} class="rounded text-blue-600">
        <span class="truncate">${escT(x.keyword)}</span>
      </span>
      <span class="num text-[10px] text-slate-500 shrink-0" title="إعلانات: ${x.total_ads} · متاجر: ${x.store_ads || 0} · رابحة: ${x.winning || 0}">${x.total_ads}</span>
    </label>`).join('')
    : '<div class="text-xs text-slate-400 p-2">لا نتائج</div>';
}
function updateKeywordSelection() {
  const cnt = document.getElementById('kw-count');
  if (cnt) cnt.textContent = currentKeywords.length;
  const sel = document.getElementById('kw-selected');
  if (!sel) return;
  sel.innerHTML = currentKeywords.map(k => `
    <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border ${kwTone(k)} text-[10.5px] font-bold max-w-[190px]">
      <span class="truncate">${escT(k)}</span>
      <button type="button" data-kw="${escT(k)}" onclick="toggleKeyword(this.dataset.kw)" class="opacity-70 hover:opacity-100">✕</button>
    </span>`).join('');
}
async function loadKeywordChips() {
  try {
    const r = await fetch('/api/ad-keywords');
    if (r.status === 401) return;
    const d = await r.json();
    kwMenuItems = d.keywords || [];
    renderKeywordMenu();
    updateKeywordSelection();
  } catch (_e) {}
}
function toggleKeyword(k) {
  const key = String(k);
  const i = currentKeywords.findIndex(x => x.toLowerCase() === key.toLowerCase());
  if (i >= 0) currentKeywords.splice(i, 1); else currentKeywords.push(key);
  syncFilterUrl();
  loadAds();
  updateKeywordSelection();
  renderKeywordMenu();
}
function clearKeywordFilter() {
  currentKeywords = [];
  syncFilterUrl();
  loadAds();
  updateKeywordSelection();
  renderKeywordMenu();
}

// ── Niches (auto-classified from the ad text) ──
const NICHE_PALETTE = [
  'bg-indigo-50 text-indigo-700 border-indigo-300',
  'bg-cyan-50 text-cyan-700 border-cyan-300',
  'bg-lime-50 text-lime-700 border-lime-300',
  'bg-pink-50 text-pink-700 border-pink-300',
  'bg-slate-100 text-slate-700 border-slate-300',
  'bg-yellow-50 text-yellow-700 border-yellow-300',
  'bg-purple-50 text-purple-700 border-purple-300',
  'bg-blue-50 text-blue-700 border-blue-300',
];
function nicheTone(slug) {
  let h = 0; const s = String(slug || '');
  for (let i = 0; i < s.length; i++) h = (h * 33 + s.charCodeAt(i)) >>> 0;
  return NICHE_PALETTE[h % NICHE_PALETTE.length];
}
function nicheActive(slug) {
  return currentNiches.some(x => x.toLowerCase() === String(slug).toLowerCase());
}
function nicheChipHtml(slug, opts = {}) {
  const label = opts.label || slug;
  const active = nicheActive(slug);
  const cnt = opts.count != null ? ` <span class="num opacity-70">${opts.count}</span>` : '';
  return `<button type="button" data-niche="${escT(slug)}" onclick="toggleNiche(this.dataset.niche)" title="${escT(opts.title || label)}"
    class="px-2.5 py-0.5 rounded-full border text-[11px] font-bold transition max-w-[240px] truncate ${nicheTone(slug)} ${active ? 'ring-2 ring-indigo-400 ring-offset-1' : 'hover:brightness-95'}">🧩 ${escT(label)}${cnt}</button>`;
}
function adNicheChips(a) {
  const list = [...new Set(((a && a.niches) || []).map(n => n.slug).filter(Boolean))];
  if (!list.length) return '';
  const shown = list.slice(0, 2);
  const rest = list.length - shown.length;
  return `<div class="flex flex-wrap gap-1.5 mb-2.5">${shown.map(s => nicheChipHtml(s)).join('')}${
    rest > 0 ? `<span class="px-2 py-0.5 rounded-full border border-slate-200 bg-slate-50 text-slate-600 text-[11px] font-bold" title="${escT(list.join(' · '))}">+${rest}</span>` : ''
  }</div>`;
}
function renderNicheMenu() {
  const box = document.getElementById('niche-items');
  if (!box) return;
  box.innerHTML = nicheMenuItems.length ? nicheMenuItems.map(n => `
    <label class="flex items-center justify-between gap-2 px-2 py-1.5 rounded-lg hover:bg-slate-50 cursor-pointer text-xs">
      <span class="flex items-center gap-2 min-w-0">
        <input type="checkbox" data-niche="${escT(n.slug)}" onclick="toggleNiche(this.dataset.niche)" ${nicheActive(n.slug) ? 'checked' : ''} class="rounded text-indigo-600">
        <span class="truncate">${escT(n.name_ar || n.slug)}</span>
      </span>
      <span class="num text-[10px] text-slate-500 shrink-0">${n.total_ads}</span>
    </label>`).join('')
    : '<div class="text-xs text-slate-400 p-2">لا توجد نيشات مصنّفة بعد.</div>';
}
function updateNicheSelection() {
  const cnt = document.getElementById('niche-count');
  if (cnt) cnt.textContent = currentNiches.length;
  const sel = document.getElementById('niche-selected');
  if (!sel) return;
  sel.innerHTML = currentNiches.map(s => {
    const item = nicheMenuItems.find(x => String(x.slug).toLowerCase() === s.toLowerCase());
    const label = item ? (item.name_ar || item.slug) : s;
    return `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border ${nicheTone(s)} text-[10.5px] font-bold max-w-[190px]">
      <span class="truncate">${escT(label)}</span>
      <button type="button" data-niche="${escT(s)}" onclick="toggleNiche(this.dataset.niche)" class="opacity-70 hover:opacity-100">✕</button>
    </span>`;
  }).join('');
}
async function loadNicheChips() {
  try {
    const r = await fetch('/api/niches');
    if (r.status === 401) return;
    const d = await r.json();
    nicheMenuItems = d.niches || [];
    renderNicheMenu();
    updateNicheSelection();
  } catch (_e) {}
}
function toggleNiche(slug) {
  const key = String(slug);
  const i = currentNiches.findIndex(x => x.toLowerCase() === key.toLowerCase());
  if (i >= 0) currentNiches.splice(i, 1); else currentNiches.push(key);
  syncFilterUrl();
  loadAds();
  updateNicheSelection();
  renderNicheMenu();
}
function clearNicheFilter() {
  currentNiches = [];
  syncFilterUrl();
  loadAds();
  updateNicheSelection();
  renderNicheMenu();
}

// ── Ads-per-product filter ──
let minProductAds = 0;
const MIN_PRODUCT_ADS_CHOICES = [0, 2, 5, 10, 20];

function renderProductAdsFilter() {
  const box = document.getElementById('min-product-ads-btns');
  if (!box) return;
  box.innerHTML = MIN_PRODUCT_ADS_CHOICES.map(n => {
    const on = n === minProductAds;
    const cls = on
      ? 'bg-blue-600 text-white border-blue-600'
      : n >= 10 ? 'bg-white text-rose-700 border-rose-300 hover:bg-rose-50'
      : n >= 5 ? 'bg-white text-orange-700 border-orange-300 hover:bg-orange-50'
      : 'bg-white text-slate-700 border-[#cfe0f5] hover:bg-[#eef6ff]';
    return `<button onclick="setMinProductAds(${n})" class="text-xs px-3.5 py-1.5 rounded-lg border font-bold transition shadow-sm ${cls}">${n ? n + '+ إعلان' : 'الكل'}</button>`;
  }).join('');
  const input = document.getElementById('min-product-ads');
  input.value = minProductAds && !MIN_PRODUCT_ADS_CHOICES.includes(minProductAds) ? minProductAds : '';
}

function setMinProductAds(n) {
  n = parseInt(n) || 0;
  minProductAds = n > 1 ? n : 0;
  renderProductAdsFilter();
  loadAds();
}
document.addEventListener('DOMContentLoaded', renderProductAdsFilter);

function debounceLoadAds() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(loadAds, 300);
}

function filterByTarget(targetId, targetType, targetValue, targetLabel) {
  currentTargetFilter = { id: targetId, type: targetType, value: targetValue, label: targetLabel };
  document.getElementById('target-filter-label').innerText = targetLabel || targetValue;
  const typeAr = { page: 'صفحة', domain: 'متجر', keyword: 'كلمة', custom_url: 'رابط مخصص' };
  document.getElementById('target-filter-type').innerText = typeAr[targetType] || targetType;
  document.getElementById('target-filter-banner').classList.remove('hidden');
  showTab('ads');
  loadAds();
}

function clearTargetFilter() {
  currentTargetFilter = null;
  document.getElementById('target-filter-banner').classList.add('hidden');
  loadAds();
}

