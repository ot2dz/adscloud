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
// Whole-market scans store their date window inside the keyword
// ("🔍 كل إعلانات السوق (متاجر فقط) 2026-10-01 ← 2026-10-20"). The window is
// already on the ad, so display one clean tag and keep the raw variants only
// for filtering.
const KW_DATE_SUFFIX = /\s*\d{4}-\d{2}-\d{2}\s*[^\w\s]*\s*\d{4}-\d{2}-\d{2}\s*$/u;
function cleanKeywordLabel(k) {
  return String(k == null ? '' : k).replace(KW_DATE_SUFFIX, '').trim();
}
function keywordGroups(list) {
  const groups = new Map();
  for (const raw of list || []) {
    const k = String(raw == null ? '' : raw).trim();
    if (!k) continue;
    const label = cleanKeywordLabel(k);
    if (!label) continue;
    const key = label.toLowerCase();
    let g = groups.get(key);
    if (!g) { g = { label, variants: [] }; groups.set(key, g); }
    if (!g.variants.includes(k)) g.variants.push(k);
  }
  return [...groups.values()];
}
function keywordChipHtml(k, opts = {}) {
  const label = opts.label || cleanKeywordLabel(k) || k;
  const variants = (Array.isArray(opts.variants) && opts.variants.length) ? opts.variants : [k];
  const active = variants.some(v => kwActive(v));
  const cnt = opts.count != null ? ` <span class="num opacity-70">${opts.count}</span>` : '';
  return `<button type="button" data-kws="${escT(JSON.stringify(variants))}" onclick="toggleKeywordGroup(this.dataset.kws)" title="${escT(opts.title || 'فلترة بهذه الكلمة')}"
    class="px-2.5 py-0.5 rounded-full border text-[11px] font-bold transition max-w-[240px] truncate ${kwTone(label)} ${active ? 'ring-2 ring-blue-400 ring-offset-1' : 'hover:brightness-95'}">🔍 ${escT(label)}${cnt}</button>`;
}
function adKeywordChips(a, opts = {}) {
  const list = (opts.group && a.group && a.group.keywords) || a.keywords || [];
  const groups = keywordGroups(list);
  if (!groups.length) return '';
  const max = 3;
  const shown = groups.slice(0, max);
  const rest = groups.length - shown.length;
  return `<div class="flex flex-wrap gap-1.5 mb-2.5">${shown.map(g => keywordChipHtml(g.label, { label: g.label, variants: g.variants })).join('')}${
    rest > 0 ? `<span class="px-2 py-0.5 rounded-full border border-slate-200 bg-slate-50 text-slate-600 text-[11px] font-bold" title="${escT(groups.map(g => g.label).join(' · '))}">+${rest}</span>` : ''
  }</div>`;
}
// Card face: show ONE scrape tag (the most specific one). The remaining tags
// live in the ad-details modal, so the card stays readable.
function cardKeywordBadge(list) {
  const groups = keywordGroups(list);
  if (!groups.length) return '';
  const ordered = groups.slice().sort((x, y) => {
    const mx = /السوق/.test(x.label) ? 1 : 0;
    const my = /السوق/.test(y.label) ? 1 : 0;
    return mx - my || x.label.length - y.label.length || x.label.localeCompare(y.label);
  });
  const first = ordered[0];
  const extra = ordered.length > 1 ? `+${ordered.length - 1}` : '';
  return `<div class="mb-2.5">
    <span class="inline-flex items-center gap-1 max-w-full px-2 py-0.5 rounded-full border ${kwTone(first.label)} text-[10.5px] font-bold" title="${escT(ordered.map(g => g.label).join(' · '))}">
      🔍 <span class="truncate">${escT(first.label)}</span>${extra ? `<span class="opacity-70 shrink-0">${extra}</span>` : ''}
    </span>
  </div>`;
}
function groupedKeywordMenu(items) {
  const groups = new Map();
  for (const it of items || []) {
    const label = cleanKeywordLabel(it.keyword);
    if (!label) continue;
    const key = label.toLowerCase();
    let g = groups.get(key);
    if (!g) {
      g = { label, variants: [], total_ads: 0, store_ads: 0, winning: 0 };
      groups.set(key, g);
    }
    g.variants.push(String(it.keyword));
    // The same ad appears in several windows: take the largest window count
    // instead of summing it (summing would double-count the ad).
    g.total_ads = Math.max(g.total_ads, Number(it.total_ads || 0));
    g.store_ads = Math.max(g.store_ads, Number(it.store_ads || 0));
    g.winning = Math.max(g.winning, Number(it.winning || 0));
  }
  return [...groups.values()];
}
function renderKeywordMenu() {
  const box = document.getElementById('kw-items');
  if (!box) return;
  const q = ((document.getElementById('kw-search') || {}).value || '').trim().toLowerCase();
  const items = groupedKeywordMenu(kwMenuItems).filter(x => !q || x.label.toLowerCase().includes(q));
  box.innerHTML = items.length ? items.map(x => `
    <label class="flex items-center justify-between gap-2 px-2 py-1.5 rounded-lg hover:bg-slate-50 cursor-pointer text-xs">
      <span class="flex items-center gap-2 min-w-0">
        <input type="checkbox" data-kws="${escT(JSON.stringify(x.variants))}" onclick="toggleKeywordGroup(this.dataset.kws)" ${x.variants.some(v => kwActive(v)) ? 'checked' : ''} class="rounded text-blue-600">
        <span class="truncate">${escT(x.label)}</span>
      </span>
      <span class="num text-[10px] text-slate-500 shrink-0" title="إعلانات: ${x.total_ads} · متاجر: ${x.store_ads || 0} · رابحة: ${x.winning || 0}">${x.total_ads}</span>
    </label>`).join('')
    : '<div class="text-xs text-slate-400 p-2">لا نتائج</div>';
}
function updateKeywordSelection() {
  const cnt = document.getElementById('kw-count');
  if (cnt) cnt.textContent = keywordGroups(currentKeywords).length;
  const sel = document.getElementById('kw-selected');
  if (!sel) return;
  sel.innerHTML = keywordGroups(currentKeywords).map(g => `
    <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border ${kwTone(g.label)} text-[10.5px] font-bold max-w-[190px]">
      <span class="truncate">${escT(g.label)}</span>
      <button type="button" data-kws="${escT(JSON.stringify(g.variants))}" onclick="toggleKeywordGroup(this.dataset.kws)" class="opacity-70 hover:opacity-100">✕</button>
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
// One displayed tag can stand for several stored variants (e.g. the same
// whole-market label seen in different date windows): toggle them together.
function toggleKeywordGroup(raw) {
  let variants = [];
  try { variants = JSON.parse(raw); } catch (_e) { variants = [raw]; }
  if (!Array.isArray(variants)) variants = [variants];
  variants = variants.map(v => String(v == null ? '' : v)).filter(Boolean);
  if (!variants.length) return;
  const active = variants.every(v => kwActive(v));
  if (active) {
    const drop = new Set(variants.map(v => v.toLowerCase()));
    currentKeywords = currentKeywords.filter(x => !drop.has(String(x).toLowerCase()));
  } else {
    const have = new Set(currentKeywords.map(x => String(x).toLowerCase()));
    for (const v of variants) {
      const key = v.toLowerCase();
      if (!have.has(key)) { currentKeywords.push(v); have.add(key); }
    }
  }
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

