// ── Ads pagination ──
let adsPage = 1;
let adsTotal = 0;
let adsPageSize = 60;
try { adsPageSize = parseInt(localStorage.getItem('adsPageSize')) || 60; } catch (_e) {}
let adsFilterKey = '';
let groupMode = true;
try { groupMode = localStorage.getItem('adsGroupMode') !== '0'; } catch (_e) {}

function setGroupMode(on) {
  groupMode = !!on;
  try { localStorage.setItem('adsGroupMode', groupMode ? '1' : '0'); } catch (_e) {}
  loadAds();
}

function adsPageCount() { return Math.max(1, Math.ceil(adsTotal / adsPageSize)); }

function goToAdsPage(n) {
  adsPage = Math.min(Math.max(1, n), adsPageCount());
  loadAds({ keepPage: true, scroll: true });
}

function setAdsPageSize(v) {
  adsPageSize = parseInt(v) || 60;
  try { localStorage.setItem('adsPageSize', adsPageSize); } catch (_e) {}
  adsPage = 1;
  loadAds({ keepPage: true });
}

function renderAdsPager() {
  const pages = adsPageCount();
  const from = adsTotal ? (adsPage - 1) * adsPageSize + 1 : 0;
  const to = Math.min(adsPage * adsPageSize, adsTotal);
  // Compact page list: 1 … p-2 p-1 [p] p+1 p+2 … last
  const nums = [];
  for (let i = 1; i <= pages; i++) {
    if (i === 1 || i === pages || Math.abs(i - adsPage) <= 2) nums.push(i);
    else if (nums[nums.length - 1] !== '…') nums.push('…');
  }
  const btn = (label, page, opts = {}) => `<button ${opts.disabled ? 'disabled' : `onclick="goToAdsPage(${page})"`}
      class="min-w-[2rem] px-2.5 py-1.5 rounded-lg border font-bold transition ${opts.active
        ? 'bg-blue-600 text-white border-blue-600'
        : opts.disabled ? 'bg-slate-50 text-slate-300 border-slate-200 cursor-not-allowed'
        : 'bg-white text-slate-700 border-[#cfe0f5] hover:bg-[#eef6ff]'}">${label}</button>`;
  const html = `
    <span>عرض <b class="num text-slate-800">${from}–${to}</b> من <b class="num text-blue-700">${adsTotal}</b></span>
    <div class="flex flex-wrap items-center gap-1" dir="rtl">
      ${btn('→ السابق', adsPage - 1, { disabled: adsPage <= 1 })}
      ${nums.map(n => n === '…' ? '<span class="px-1 text-slate-400">…</span>' : btn(n, n, { active: n === adsPage })).join('')}
      ${btn('التالي ←', adsPage + 1, { disabled: adsPage >= pages })}
    </div>
    <label class="flex items-center gap-1.5">لكل صفحة:
      <select onchange="setAdsPageSize(this.value)" class="bg-white border border-[#cfe0f5] rounded-lg px-2 py-1 font-bold text-slate-700">
        ${[30, 60, 120, 240].map(n => `<option value="${n}" ${n === adsPageSize ? 'selected' : ''}>${n}</option>`).join('')}
      </select>
    </label>`;
  document.querySelectorAll('.ads-pager').forEach(el => {
    el.innerHTML = adsTotal > 0 ? html : '';
  });
}

async function loadAds(opts = {}) {
  const q = document.getElementById('search').value;
  const dmin = document.getElementById('start-date-min').value;
  const dmax = document.getElementById('start-date-max').value;
  const mdays = document.getElementById('min-days').value || 0;
  const sort = document.getElementById('sort-by').value;
  updateFiltersCount();

  let url = `/api/ads?search=${encodeURIComponent(q)}&preset=${currentPreset}&start_date_min=${encodeURIComponent(dmin)}&start_date_max=${encodeURIComponent(dmax)}&min_days_active=${mdays}&sort_by=${sort}`;
  if (minProductAds > 1) url += `&min_product_ads=${minProductAds}`;
  if (currentKeywords.length) url += `&keywords=${encodeURIComponent(currentKeywords.join(','))}`;
  if (currentNiches.length) url += `&niche=${encodeURIComponent(currentNiches.join(','))}`;

  if (currentTargetFilter) {
    if (currentTargetFilter.id) url += `&target_id=${currentTargetFilter.id}`;
    if (currentTargetFilter.type === 'page') {
      url += `&page_id=${encodeURIComponent(currentTargetFilter.value)}`;
    } else if (currentTargetFilter.type === 'domain') {
      url += `&store_domain=${encodeURIComponent(currentTargetFilter.value)}`;
    } else if (currentTargetFilter.type === 'keyword') {
      url += `&query=${encodeURIComponent(currentTargetFilter.value)}`;
    }
  }

  url += adminUserParam('&');
  if (groupMode) url += '&group=1';

  // A change of filters/sort goes back to page 1; plain refreshes (after a
  // scrape, periodic reloads) keep the page the user is reading.
  if (url !== adsFilterKey) {
    adsFilterKey = url;
    if (!opts.keepPage) adsPage = 1;
  }

  const r = await fetch(`${url}&limit=${adsPageSize}&offset=${(adsPage - 1) * adsPageSize}`);
  const d = await r.json();
  adsTotal = d.total || 0;
  // Page beyond the end (e.g. ads were deleted): jump to the last page.
  if (adsPage > adsPageCount() && adsTotal > 0) {
    adsPage = adsPageCount();
    return loadAds({ keepPage: true, scroll: opts.scroll });
  }
  ads = d.ads;
  renderAds();
  renderAdsPager();
  document.getElementById('group-toggle').checked = groupMode;
  document.getElementById('ads-total-label').innerText = groupMode ? 'عدد المنتجات المعروضة:' : 'عدد الإعلانات المعروضة:';
  document.getElementById('ads-total-sub').innerText = groupMode && d.total_ads != null ? `(من ${d.total_ads} إعلان)` : '';
  document.getElementById('ads-total').innerText = adsTotal;
  if (opts.scroll) document.getElementById('ads-total').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function copyAdText(txt) {
  if (!txt) return alert('لا يوجد نص لنسخه');
  navigator.clipboard.writeText(txt).then(() => alert('تم نسخ نص الإعلان بنجاح'));
}

function renderAds() {
  const el = document.getElementById('ads');
  if (!ads.length) {
    el.innerHTML = '<div class="col-span-full py-16 text-center text-slate-500 font-bold bg-white border border-[#cfe0f5] rounded-2xl">لا توجد إعلانات مطابقة للفلاتر المحددة.</div>';
    return;
  }
  ads.forEach(a => { adCache[String(a.ad_id)] = a; });
  el.innerHTML = ads.map(a => adCardHtml(a)).join('');
}

// Total active ads of the advertiser's page / store domain on Meta (not only
// the ads our searches collected). Filled in by the background counter.
function sizeTone(n) {
  if (n >= 50) return 'bg-rose-50 text-rose-700 border-rose-300';
  if (n >= 20) return 'bg-orange-50 text-orange-700 border-orange-300';
  return 'bg-[#eef6ff] text-[#174ea6] border-[#cfe0f5]';
}
function domainAdsUrl(dom) {
  return `https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=DZ&is_targeted_country=false&media_type=all&q=${encodeURIComponent('"' + dom + '"')}&search_type=keyword_exact_phrase&sort_data[direction]=desc&sort_data[mode]=total_impressions`;
}
function entityBadges(a) {
  const parts = [];
  if (a.page_id) {
    parts.push(a.page_total_ads != null
      ? `<a href="${pageAdsUrl(a.page_id)}" target="_blank" rel="noopener" title="كل إعلانات هذه الصفحة على فيسبوك" class="px-2 py-0.5 rounded-lg border font-bold ${sizeTone(a.page_total_ads)}">📄 <span class="num">${a.page_total_ads}</span> إعلان نشط للصفحة</a>`
      : `<span class="px-2 py-0.5 rounded-lg border border-slate-200 bg-slate-50 text-slate-400 font-semibold" title="سيُحسب تلقائياً عند توقف السحب">📄 ⏳ عدّ إعلانات الصفحة…</span>`);
  }
  if (a.store_domain) {
    const pagesNote = a.domain_pages > 1 ? ` · عبر ${a.domain_pages} صفحات` : '';
    parts.push(a.domain_total_ads != null
      ? `<a href="${domainAdsUrl(a.store_domain)}" target="_blank" rel="noopener" title="كل إعلانات هذا المتجر في الجزائر (كل الصفحات)" class="px-2 py-0.5 rounded-lg border font-bold ${sizeTone(a.domain_total_ads)}">🌐 <span class="num">${a.domain_total_ads}</span> إعلان للمتجر${pagesNote}</a>`
      : (pagesNote ? `<span class="px-2 py-0.5 rounded-lg border border-slate-200 bg-slate-50 text-slate-500 font-semibold">🌐 ⏳${pagesNote}</span>` : ''));
  }
  return parts.length ? `<div class="flex flex-wrap gap-1.5 mb-2.5 text-[11px]">${parts.join('')}</div>` : '';
}

// Colour band by how many ads one advertiser runs for the same product.
function groupTone(n) {
  if (n >= 10) return { ring: 'border-rose-400 ring-2 ring-rose-200', badge: 'bg-rose-600', label: 'توسّع قوي' };
  if (n >= 5) return { ring: 'border-orange-400 ring-2 ring-orange-100', badge: 'bg-orange-500', label: 'يتوسّع' };
  if (n >= 2) return { ring: 'border-blue-400 ring-1 ring-blue-100', badge: 'bg-blue-600', label: '' };
  return null;
}

async function openGroupModal(key, title) {
  const modal = document.getElementById('group-modal');
  const box = document.getElementById('group-modal-ads');
  document.getElementById('group-modal-title').innerText = title;
  box.innerHTML = '<div class="col-span-full py-10 text-center text-slate-500 font-bold">… جاري التحميل</div>';
  modal.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  const r = await fetch('/api/ads/group?key=' + encodeURIComponent(key) + adminUserParam('&'));
  const d = await r.json();
  (d.ads || []).forEach(a => { adCache[String(a.ad_id)] = a; });
  box.innerHTML = (d.ads || []).map(a => adCardHtml(a, { plain: true })).join('') ||
    '<div class="col-span-full py-10 text-center text-slate-500 font-bold">لا توجد إعلانات.</div>';
}

function closeGroupModal() {
  document.getElementById('group-modal').classList.add('hidden');
  document.body.style.overflow = '';
}

function closeAdModal() {
  const m = document.getElementById('ad-modal');
  if (m) m.classList.add('hidden');
  const b = document.getElementById('ad-modal-body');
  if (b) b.innerHTML = '';
  document.body.style.overflow = '';
}

// Build the Google phrase from ONE line/sentence only (never joins two lines).
// Emojis, symbols, punctuation and line breaks act as separators; prices,
// percentages, phone numbers, URLs and hashtags are dropped.
function adSearchSegments(body) {
  const normalized = String(body || '')
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/#[^\s#]+/g, ' ')
    .replace(/[\p{Extended_Pictographic}\uFE0F\u200D\u2600-\u27BF\u2B00-\u2BFF]/gu, '\n')
    .replace(/[،,.!?؟:;؛…\-–—|•·/\\()[\]{}"'’‘“”]+/g, '\n')
    .replace(/\r/g, '\n');
  return normalized.split('\n')
    .map(s => s
      .replace(/\+?[0-9٠-٩][0-9٠-٩\s\-]{6,}/g, ' ')
      .replace(/[0-9٠-٩]+\s*(دج|د\.ج|دينار|DA|DZD|درهم|ريال|ألف|الف)/gi, ' ')
      .replace(/[0-9٠-٩]+\s*%/g, ' ')
      .replace(/\s+/g, ' ')
      .trim())
    .filter(s => {
      const w = s.split(' ').filter(Boolean);
      return w.length >= 3 && /\p{L}/u.test(s);
    });
}

function adGoogleQuery(a) {
  const segments = adSearchSegments(a && a.body);
  if (!segments.length) return '';
  // Longest single segment (most distinctive), capped at 12 words.
  segments.sort((x, y) => y.split(' ').length - x.split(' ').length);
  const phrase = segments[0].split(' ').slice(0, 12).join(' ').slice(0, 140).trim();
  const page = String((a && a.page_name) || '').replace(/"/g, ' ').replace(/\s+/g, ' ').trim();
  let q = `site:facebook.com "${phrase}"`;
  if (page) q += ` "${page}"`;
  return q;
}

function searchAdOnGoogle(adId) {
  const a = adCache[String(adId)];
  const q = a && adGoogleQuery(a);
  if (!q) return alert('لا يوجد نص كافٍ في الإعلان للبحث عنه.');
  window.open('https://www.google.com/search?q=' + encodeURIComponent(q), '_blank', 'noopener');
}
function searchAdOnBing(adId) {
  const a = adCache[String(adId)];
  const q = a && adGoogleQuery(a);
  if (!q) return alert('لا يوجد نص كافٍ في الإعلان للبحث عنه.');
  window.open('https://www.bing.com/search?q=' + encodeURIComponent(q), '_blank', 'noopener');
}
function copyAdGoogleQuery(adId) {
  const a = adCache[String(adId)];
  const q = a && adGoogleQuery(a);
  if (!q) return alert('لا يوجد نص كافٍ في الإعلان للبحث عنه.');
  navigator.clipboard.writeText(q).then(() => alert('تم نسخ استعلام البحث:\n' + q)).catch(() => alert(q));
}

function openAdDetails(adId) {
  pauseCardVideos();
  const a = adCache[String(adId)];
  if (!a) return;
  const box = document.getElementById('ad-modal-body');
  const title = document.getElementById('ad-modal-title');
  const g = a.group || null;
  title.textContent = a.page_name || 'تفاصيل الإعلان';
  lastAdBody = a.body || '';

  // Same-origin proxy: signed fbcdn URLs are blocked cross-origin by some
  // browsers (and expire), so the server streams the bytes for us.
  // Starts with the light SD copy; one click switches to HD.
  const sd = proxiedVideo(a.video_sd_url), hd = proxiedVideo(a.video_url);
  const video = sd || hd;
  const img = safeUrl(a.preview_image_url);
  const media = video
    ? `<video id="ad-modal-video" src="${video}" controls autoplay playsinline class="w-full max-h-[62vh] rounded-xl bg-black"></video>
       <div class="flex items-center justify-between gap-2 text-[11px] text-slate-400 mt-1">
         <span>إن لم يعمل التشغيل، استخدم زر «فتح في مكتبة الإعلانات».</span>
         ${sd && hd && sd !== hd ? `<button type="button" data-sd="${sd}" data-hd="${hd}" data-q="sd" onclick="switchModalQuality(this)"
           class="shrink-0 px-2.5 py-1 rounded-lg border border-[#cfe0f5] text-[#174ea6] font-bold hover:bg-[#eef6ff]">🎞️ جودة عالية (HD)</button>` : ''}
       </div>`
    : (img
        ? `<img src="${img}" alt="صورة الإعلان" class="w-full max-h-[62vh] object-contain rounded-xl bg-slate-100">`
        : `<div class="w-full h-56 rounded-xl bg-slate-100 flex items-center justify-center text-slate-400 font-bold">لا توجد وسائط</div>`);

  const kws = [...new Set(((g && g.keywords) || a.keywords || []).filter(Boolean))];
  const pageLink = a.page_id
    ? `https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL&is_targeted_country=false&media_type=all&search_type=page&sort_data[mode]=total_impressions&sort_data[direction]=desc&view_all_page_id=${encodeURIComponent(a.page_id)}`
    : '';

  const rows = [];
  const row = (label, value, cls = '') => `<div class="flex justify-between gap-3"><span class="text-slate-500">${label}</span><b class="${cls}">${value}</b></div>`;
  if (a.page_name) rows.push(row('المعلن', escT(a.page_name), 'text-[#102a56]'));
  if (a.page_id) rows.push(row('Page ID', escT(a.page_id), 'font-mono'));
  if (a.store_domain) rows.push(row('المتجر', escT(a.store_domain), 'font-mono text-blue-700'));
  if (a.platform) rows.push(row('المنصة', escT(a.platform)));
  if (a.collected_at) rows.push(row('تاريخ السحب', escT(String(a.collected_at).slice(0, 10)), 'font-mono'));
  if (a.start_date) rows.push(row('تاريخ الإطلاق', escT(a.start_date), 'font-mono'));
  if (a.days_active != null) rows.push(row('الأيام النشطة', a.days_active, 'num'));
  rows.push(row('الحالة', a.is_active ? 'نشط' : `توقف بعد ${a.days_active || 1} يوم`, a.is_active ? 'text-emerald-700' : 'text-rose-700'));
  if (!a.is_active && a.stopped_at) rows.push(row('آخر يوم شوهد نشطاً', escT(String(a.stopped_at).slice(0, 10)), 'font-mono'));
  if (a.status_checked_at) rows.push(row('آخر فحص للحالة', escT(String(a.status_checked_at).slice(0, 10)), 'font-mono'));
  if ((a.publisher_platforms || []).length) rows.push(row('المنصات', escT((a.publisher_platforms || []).join(', '))));
  if (a.cta_text) rows.push(row('CTA', escT(a.cta_text)));
  if (a.collation_count) rows.push(row('عدد النسخ', a.collation_count, 'num'));
  if (g) {
    rows.push(row('ضمن مجموعة', `${g.count} إعلان (${g.active} نشط)`, 'num'));
    if (g.first_launch) rows.push(row('أول إطلاق للمنتج', escT(String(g.first_launch).slice(0, 10)), 'font-mono'));
    if (g.last_launch) rows.push(row('آخر إطلاق للمنتج', escT(String(g.last_launch).slice(0, 10)), 'font-mono'));
    if (g.texts) rows.push(row('نصوص مختلفة', g.texts, 'num'));
  }
  if (a.page_total_ads != null) rows.push(row('إجمالي إعلانات الصفحة', a.page_total_ads, 'num'));
  if (a.domain_total_ads != null) rows.push(row('إجمالي إعلانات المتجر', a.domain_total_ads, 'num'));

  box.innerHTML = `
    ${media}
    <div class="flex flex-wrap gap-2">
      <button onclick="searchAdOnGoogle('${a.ad_id}')" class="px-3 py-2 rounded-xl bg-indigo-600 text-white text-xs font-bold hover:bg-indigo-700" title="site:facebook.com &quot;نص الإعلان&quot; &quot;اسم الصفحة&quot;">🔎 ابحث عن المنشور في جوجل</button>
      <button onclick="searchAdOnBing('${a.ad_id}')" class="px-3 py-2 rounded-xl bg-white border border-[#cfe0f5] text-[#174ea6] text-xs font-bold hover:bg-[#eef6ff]">🅱️ Bing</button>
      <button onclick="copyAdGoogleQuery('${a.ad_id}')" class="px-3 py-2 rounded-xl bg-white border border-[#cfe0f5] text-slate-700 text-xs font-bold hover:bg-slate-50">📋 نسخ الاستعلام</button>
      <a href="https://www.facebook.com/ads/library/?id=${encodeURIComponent(a.ad_id)}" target="_blank" rel="noopener" class="px-3 py-2 rounded-xl bg-blue-600 text-white text-xs font-bold hover:bg-blue-700">🕵️ فتح في مكتبة الإعلانات</a>
      ${a.link_url ? `<a href="${safeUrl(a.link_url)}" target="_blank" rel="noopener" class="px-3 py-2 rounded-xl bg-white border border-[#cfe0f5] text-[#174ea6] text-xs font-bold hover:bg-[#eef6ff]">🔗 فتح المتجر/الرابط</a>` : ''}
      ${pageLink ? `<a href="${pageLink}" target="_blank" rel="noopener" class="px-3 py-2 rounded-xl bg-white border border-[#cfe0f5] text-[#174ea6] text-xs font-bold hover:bg-[#eef6ff]">📄 إعلانات الصفحة</a>` : ''}
      <button onclick="copyAdText(lastAdBody)" class="px-3 py-2 rounded-xl bg-white border border-[#cfe0f5] text-slate-700 text-xs font-bold hover:bg-slate-50">📋 نسخ النص</button>
    </div>
    <div class="text-[11px] text-slate-600 bg-slate-50 border border-slate-200 rounded-xl p-2">
      <span class="font-bold text-slate-500">استعلام البحث (يُفتح في متصفحك):</span>
      <div class="font-mono break-all mt-0.5" dir="auto">${escT(adGoogleQuery(a))}</div>
    </div>
    ${g && g.count > 1 ? `<button data-key="${escT(g.key)}" data-title="${escT((a.page_name || '') + ' — ' + g.count + ' إعلان لنفس المنتج')}"
        onclick="closeAdModal(); openGroupModal(this.dataset.key, this.dataset.title)"
        class="w-full py-2 rounded-xl ${(groupTone(g.count) || {}).badge || 'bg-blue-600'} hover:opacity-90 text-white font-bold text-xs">👁️ عرض كل الـ ${g.count} إعلان لهذا المنتج</button>` : ''}
    ${kws.length ? `<div class="flex flex-wrap gap-1.5">${kws.map(k => keywordChipHtml(k)).join('')}</div>` : ''}
    ${adNicheChips(a).replace('mb-2.5', '')}
    ${entityBadges(escAd(a)).replace('mb-2.5', '')}
    <div class="grid sm:grid-cols-2 gap-2 text-xs text-slate-700 bg-[#f8fbff] border border-[#cfe0f5] rounded-xl p-3">${rows.join('')}</div>
    <div>
      <div class="text-xs font-bold text-slate-500 mb-1">نص الإعلان</div>
      <div class="text-sm text-slate-800 whitespace-pre-line leading-relaxed max-h-64 overflow-auto bg-slate-50 border border-slate-200 rounded-xl p-3">${escT(a.body || '—')}</div>
    </div>`;
  document.getElementById('ad-modal').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeGroupModal(); closeAdModal(); }
});

// Ad data comes from Facebook advertisers (and the extension): treat every
// field as hostile. Text is HTML-escaped; links must be http(s).
function safeUrl(u) {
  const v = String(u == null ? '' : u).trim();
  return /^https?:\/\//i.test(v) ? escT(v) : '';
}
function escAd(a) {
  const out = Object.assign({}, a);
  for (const k of ['ad_id', 'page_id', 'page_name', 'store_domain', 'platform', 'cta_text',
                   'start_date', 'collected_at', 'body']) {
    out[k] = a[k] == null ? a[k] : escT(a[k]);
  }
  out.link_url = safeUrl(a.link_url);
  out.preview_image_url = safeUrl(a.preview_image_url);
  return out;
}

function dzToday() {
  const d = new Date(Date.now() + 60 * 60 * 1000); // Algeria = UTC+1 (no DST)
  return d.toISOString().slice(0, 10);
}
function isNewToday(a) {
  const fs = String((a && a.first_seen) || '').slice(0, 10);
  return !!fs && fs === dzToday();
}

// Video URLs go through our own proxy. The card uses Meta's light SD copy
// when it exists (plays fast on weak connections); HD stays in the modal.
function proxiedVideo(u) {
  return /^https?:\/\//i.test(u || '') ? `/api/video?u=${encodeURIComponent(u)}` : '';
}
function cardVideoSrc(a) {
  return proxiedVideo(a.video_sd_url) || proxiedVideo(a.video_url);
}
// Switches the modal player between SD and HD, keeping the position.
function switchModalQuality(btn) {
  const v = document.getElementById('ad-modal-video');
  if (!v) return;
  const t = v.currentTime || 0;
  const toHd = btn.dataset.q !== 'hd';
  v.src = toHd ? btn.dataset.hd : btn.dataset.sd;
  v.addEventListener('loadedmetadata', () => { v.currentTime = t; v.play().catch(() => {}); }, { once: true });
  btn.dataset.q = toHd ? 'hd' : 'sd';
  btn.textContent = toHd ? '⚡ جودة خفيفة (SD)' : '🎞️ جودة عالية (HD)';
}

function statusCheckSummary(raw) {
  let s = null;
  try { s = raw ? JSON.parse(raw) : null; } catch (e) { s = null; }
  if (!s) return 'لم يُشغَّل بعد.';
  const when = String(s.finished_at || '').slice(0, 16).replace('T', ' ');
  const tail = s.status === 'interrupted' ? ' — توقف بسبب حجب مؤقت وسيُستأنف.' : '';
  return `آخر فحص ${when}: ${s.confirmed || 0} نشط مؤكد · ${s.stopped || 0} توقف · ${s.new || 0} جديد · ${s.requests || 0} طلب${tail}`;
}
async function startStatusCheck(btn) {
  btn.disabled = true;
  try {
    const r = await fetch('/api/status-check/start', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    alert(d.ok ? (d.queued ? '🔎 أُضيف فحص الحالة إلى قائمة الانتظار.' : '🔎 بدأ فحص الحالة.') : (d.error || 'تعذّر البدء'));
  } finally { btn.disabled = false; }
}

// Plays the ad video inside its card (one card video at a time).
function playCardVideo(btn) {
  pauseCardVideos();
  const box = btn.parentElement;
  const v = document.createElement('video');
  v.src = btn.dataset.src;
  v.controls = true; v.autoplay = true; v.playsInline = true;
  v.className = 'card-video absolute inset-0 w-full h-full object-contain bg-black';
  v.onerror = () => { v.replaceWith(Object.assign(document.createElement('div'), {
    className: 'absolute inset-0 flex items-center justify-center bg-black/70 text-white text-xs font-bold p-3 text-center',
    textContent: 'تعذّر تشغيل الفيديو — جرّب «فتح الإعلان ↗»' })); };
  box.appendChild(v);
  btn.remove();
}
function pauseCardVideos() {
  document.querySelectorAll('video.card-video').forEach(v => { if (!v.paused) v.pause(); });
}

const AR_MONTHS = ['يناير', 'فبراير', 'مارس', 'أبريل', 'ماي', 'جوان', 'جويلية', 'أوت', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر'];
function fmtDayAr(d) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(d || ''));
  return m ? `${parseInt(m[3], 10)} ${AR_MONTHS[parseInt(m[2], 10) - 1]}` : '';
}

// Compact ad card: media, who, three numbers, two lines of copy, actions.
// Everything else (keywords, niches, CTA, group stats…) lives in the details modal.
function adCardHtml(raw, opts = {}) {
    const a = escAd(raw);
    const g = (!opts.plain && a.group && a.group.count > 1) ? a.group : null;
    const tone = g ? groupTone(g.count) : null;
    const days = Math.max(a.days_active || 1, g ? g.max_days : 0);
    const isWin = days >= 5 && a.is_active;
    const isNew = days <= 3 && a.is_active;
    const adFbUrl = `https://www.facebook.com/ads/library/?id=${encodeURIComponent(raw.ad_id || '')}`;
    const previewImg = a.preview_image_url || '';
    const videoSrc = cardVideoSrc(raw);
    const active = g ? g.active > 0 : a.is_active;
    const iconBtn = 'w-9 h-9 shrink-0 flex items-center justify-center rounded-lg border border-[#d7e3f4] text-[#174ea6] hover:bg-[#eef5ff] transition';

    return `
    <article class="bg-white border ${tone ? tone.ring : 'border-[#d7e3f4]'} rounded-2xl overflow-hidden flex flex-col shadow-[0_6px_18px_rgba(23,43,77,0.05)] hover:shadow-md transition">
      <div class="relative w-full aspect-[4/3] shrink-0 overflow-hidden bg-[#eef3f9] flex items-center justify-center">
        ${previewImg ? `
          <img src="${previewImg}" alt="صورة الإعلان" loading="lazy" class="absolute inset-0 w-full h-full object-cover" onerror="this.remove()">
        ` : `<span class="text-3xl opacity-40">🖼️</span>`}
        ${videoSrc ? `
          <button type="button" data-src="${videoSrc}" onclick="playCardVideo(this)" title="تشغيل الفيديو"
            class="absolute inset-0 flex items-center justify-center group">
            <span class="w-14 h-14 rounded-full bg-black/55 group-hover:bg-black/75 text-white flex items-center justify-center text-2xl shadow-lg transition">▶</span>
          </button>` : ''}
        ${isWin ? '<span class="absolute top-2.5 right-2.5 text-[11px] px-2.5 py-0.5 rounded-full bg-amber-500 text-white font-bold shadow-sm pointer-events-none">🏆 رابح</span>'
          : (isNew ? '<span class="absolute top-2.5 right-2.5 text-[11px] px-2.5 py-0.5 rounded-full bg-sky-500 text-white font-bold shadow-sm pointer-events-none">🚀 جديد</span>' : '')}
        ${g ? `
          <button type="button" data-key="${escT(g.key)}" data-title="${escT((raw.page_name || '') + ' — ' + g.count + ' إعلان لنفس المنتج')}"
            onclick="openGroupModal(this.dataset.key, this.dataset.title)" title="عرض كل إعلانات هذا المنتج"
            class="absolute top-2.5 left-2.5 z-10 px-2.5 py-0.5 rounded-full ${tone.badge} text-white text-[11px] font-bold shadow-sm hover:opacity-90">
            📦 <span class="num">${g.count}</span> إعلانات
          </button>` : ''}
      </div>

      <div class="p-3.5 flex flex-col gap-2 flex-1">
        <div>
          <div class="flex items-center justify-between gap-2">
            <h3 class="font-bold text-[15px] text-[#102a56] truncate" title="${a.page_name || ''}">${a.page_name || 'صفحة غير معروفة'}</h3>
            ${active
              ? `<span class="shrink-0 text-[11px] font-bold text-emerald-600" title="${raw.status_checked_at ? 'آخر تأكيد: ' + escT(String(raw.status_checked_at).slice(0, 10)) : 'لم يُفحص بعد'}">● نشط</span>`
              : `<span class="shrink-0 text-[11px] font-bold text-slate-500" title="${raw.stopped_at ? 'آخر يوم شوهد نشطاً: ' + escT(String(raw.stopped_at).slice(0, 10)) : ''}">⚫ توقف بعد ${days} ${days === 1 ? 'يوم' : 'أيام'}</span>`}
          </div>
          <div class="text-[11.5px] text-slate-500 font-mono truncate" dir="ltr" style="text-align:right" title="${a.store_domain || ''}">${a.store_domain || 'بدون متجر'}</div>
        </div>

        <div class="flex items-center gap-3 text-[11.5px] text-slate-600 flex-wrap">
          ${a.start_date ? `<span title="تاريخ الإطلاق">📅 ${fmtDayAr(a.start_date)}</span>` : ''}
          <span title="الأيام النشطة">⏱️ <b class="num text-slate-800">${days}</b> ${days === 1 ? 'يوم' : 'أيام'}</span>
          ${a.domain_total_ads != null ? `<span title="إجمالي إعلانات المتجر">🏪 <b class="num text-slate-800">${a.domain_total_ads}</b> للمتجر</span>` : ''}
          ${isNewToday(a) ? '<span class="px-1.5 rounded bg-emerald-100 text-emerald-700 font-bold" title="سُحب اليوم">🆕</span>' : ''}
        </div>

        ${a.body ? `<p class="text-[12.5px] text-slate-700 leading-relaxed line-clamp-2">${escT((raw.body || '').slice(0, 220))}</p>` : ''}

        <div class="flex items-center gap-1.5 mt-auto pt-1">
          <button type="button" onclick="openAdDetails('${a.ad_id}')"
            class="flex-1 h-9 rounded-lg bg-[#174ea6] hover:bg-[#123f87] text-white font-bold text-xs transition">التفاصيل</button>
          ${a.link_url ? `<a href="${a.link_url}" target="_blank" rel="noopener" title="فتح المتجر" class="${iconBtn}">🛒</a>` : ''}
          <a href="${adFbUrl}" target="_blank" rel="noopener" title="فتح الإعلان في مكتبة الإعلانات" class="${iconBtn}">↗</a>
          <button type="button" data-text="${a.body || ''}" onclick="copyAdText(this.dataset.text)" title="نسخ النص" class="${iconBtn}">📋</button>
          ${a.page_id ? `<button type="button" data-page="${a.page_id}" data-name="${a.page_name || ''}" onclick="trackPage(this.dataset.page, this.dataset.name)" title="تتبع الصفحة" class="${iconBtn}">👁️</button>`
            : (a.store_domain ? `<button type="button" data-domain="${a.store_domain}" onclick="trackDomain(this.dataset.domain)" title="تتبع المتجر" class="${iconBtn}">👁️</button>` : '')}
        </div>
      </div>
    </article>`;
}

