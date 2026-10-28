let liveScrapeState = {
  active: false,
  query: '',
  reported: null,
  scraped: 0,
  scanned: 0,
  stores: 0
};

function updateLiveProgressUI() {
  const box = document.getElementById('live-progress-box');
  if (!liveScrapeState.active && liveScrapeState.scraped === 0 && !liveScrapeState.scanned) {
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');
  const targetLabel = document.getElementById('live-target-label');
  if (targetLabel) {
    targetLabel.innerText = liveScrapeState.paused
      ? '⏸ متوقف مؤقتًا — الهدف:'
      : (liveScrapeState.active ? 'جاري السحب للهدف:' : 'آخر هدف:');
  }
  document.getElementById('live-target-name').innerText = liveScrapeState.query || 'سحب مباشر';
  document.getElementById('live-scraped').innerText = Number(liveScrapeState.scraped).toLocaleString('en-US');
  document.getElementById('live-stores').innerText = Number(liveScrapeState.stores).toLocaleString('en-US');

  const reported = liveScrapeState.reported;
  // Progress is measured against how many ads were EXAMINED (scanned), not how
  // many were kept, so it reaches 100% even when a launch-date filter keeps a
  // small subset of the day's ads.
  const scanned = liveScrapeState.scanned || liveScrapeState.scraped || 0;

  if (reported && reported > 0) {
    document.getElementById('live-reported').innerText = Number(reported).toLocaleString('en-US') + ' إعلان';
    const rem = Math.max(0, reported - scanned);
    document.getElementById('live-remaining').innerText = Number(rem).toLocaleString('en-US');
    let pct = Math.min(100, Math.round((scanned / reported) * 100));
    if (!liveScrapeState.active) pct = 100;
    document.getElementById('live-percent').innerText = pct + '%';
    document.getElementById('live-progress-fill').style.width = Math.max(4, pct) + '%';
    document.getElementById('live-progress-fill').classList.remove('animate-pulse');
    if (!liveScrapeState.active) {
      document.getElementById('live-status-note').innerText = `✅ اكتمل الفحص — تم حفظ ${liveScrapeState.scraped} إعلاناً مطابقاً.`;
    } else if (pct >= 100) {
      document.getElementById('live-status-note').innerText = '✅ وصل السحب للحد الأقصى أو اكتمل!';
    } else {
      document.getElementById('live-status-note').innerText = `جاري فحص النتائج والتمرير... (${pct}% — فُحص ${scanned})`;
    }
  } else {
    document.getElementById('live-reported').innerText = 'جاري الرصد...';
    document.getElementById('live-remaining').innerText = '-';
    document.getElementById('live-percent').innerText = liveScrapeState.scraped > 0 ? (liveScrapeState.scraped + ' إعلان') : '0%';
    document.getElementById('live-progress-fill').style.width = '100%';
    document.getElementById('live-progress-fill').classList.add('animate-pulse');
    document.getElementById('live-status-note').innerText = 'جاري رصد إجمالي النتائج والتمرير في فيسبوك...';
  }
}


// ── Scrape panel: two modes, smart input, unified launch dates ──
let scrapeMode = 'targeted';
let scrapePreset = 'all';
const DATE_CHIPS = [
  ['all', 'كل الفترات'], ['today', 'اليوم'], ['yesterday', 'أمس'], ['last_7d', 'آخر 7 أيام'],
  ['this_month', 'هذا الشهر'], ['day', 'يوم محدد'], ['custom', 'فترة مخصصة'],
];
const MARKET_ADS_PER_DAY = 3000;   // measured: Arabic launches per day (scanned)
const SCAN_PER_SEC = 6;            // measured from one IP

function detectScrapeInput(v) {
  v = (v || '').trim(); const low = v.toLowerCase();
  if (!v) return { kind: '', value: '' };
  if (low.includes('facebook.com') && low.includes('/ads/library')) return { kind: 'link', value: v };
  if (/^\d{9,20}$/.test(v)) return { kind: 'page', value: v };
  if (low.includes('facebook.com/') || low.includes('fb.com/')) {
    const m = v.match(/(?:id=|view_all_page_id=|page_id=)(\d{6,})/) || v.match(/-(\d{8,})\/?(?:\?|$)/) || v.match(/(\d{9,})/);
    return { kind: 'page', value: m ? m[1] : '' };
  }
  if (!/\s/.test(v) && /^(https?:\/\/)?(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(\/\S*)?$/i.test(v)) {
    return { kind: 'domain', value: low.replace(/^https?:\/\//, '').replace(/^www\./, '').split(/[\/?#]/)[0] };
  }
  return { kind: 'keyword', value: v };
}

function todayDZ() {  // YYYY-MM-DD in Algeria (UTC+1)
  return new Date(Date.now() + 3600e3).toISOString().slice(0, 10);
}
function shiftDay(iso, n) { const d = new Date(iso + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
function scrapeRange() {  // [min, max] as the server will resolve it ('' = no limit)
  const t = todayDZ();
  switch (scrapePreset) {
    case 'today': return [t, t];
    case 'yesterday': { const y = shiftDay(t, -1); return [y, y]; }
    case 'last_7d': return [shiftDay(t, -6), t];
    case 'this_month': return [t.slice(0, 8) + '01', t];
    case 'day': { const d = document.getElementById('scrape-day').value; return [d, d]; }
    case 'custom': {
      let a = document.getElementById('scrape-custom-min').value, b = document.getElementById('scrape-custom-max').value || a;
      if (a && b && a > b) [a, b] = [b, a];
      return [a, b && b > t ? t : b];
    }
    default: return ['', ''];
  }
}

function setScrapeMode(m) {
  scrapeMode = m;
  const on = 'bg-white text-blue-700 shadow-sm', off = 'text-slate-600 hover:text-slate-900';
  document.getElementById('mode-targeted').className = 'px-2.5 py-1.5 rounded-md transition ' + (m === 'targeted' ? on : off);
  document.getElementById('mode-market').className = 'px-2.5 py-1.5 rounded-md transition ' + (m === 'market' ? on : off);
  document.getElementById('targeted-box').classList.toggle('hidden', m !== 'targeted');
  document.getElementById('market-box').classList.toggle('hidden', m !== 'market');
  document.getElementById('adv-box').classList.toggle('hidden', m !== 'targeted');
  if (m === 'market' && scrapePreset === 'all') scrapePreset = 'this_month';
  renderDateChips(); renderScrapeSummary();
}

function setScrapePreset(p) { scrapePreset = p; renderDateChips(); renderScrapeSummary(); }

function renderDateChips() {
  const chips = DATE_CHIPS.filter(([k]) => !(scrapeMode === 'market' && k === 'all'));
  const sel = document.getElementById('date-chips');
  sel.innerHTML = chips.map(([k, label]) => `<option value="${k}">${label}</option>`).join('');
  if (!chips.some(([k]) => k === scrapePreset)) scrapePreset = chips.length ? chips[0][0] : 'all';
  sel.value = scrapePreset;
  document.getElementById('date-day-row').classList.toggle('hidden', scrapePreset !== 'day');
  document.getElementById('date-range-row').classList.toggle('hidden', scrapePreset !== 'custom');
  const t = todayDZ();
  for (const id of ['scrape-day', 'scrape-custom-min', 'scrape-custom-max']) document.getElementById(id).max = t;
  if (scrapePreset === 'day' && !document.getElementById('scrape-day').value) document.getElementById('scrape-day').value = shiftDay(t, -1);
}

function toggleScrapeSummary(force) {
  const box = document.getElementById('scrape-summary');
  const open = force === undefined ? box.classList.contains('hidden') : !!force;
  box.classList.toggle('hidden', !open);
  const btn = document.getElementById('summary-toggle');
  if (btn) btn.className = 'w-8 h-8 shrink-0 rounded-lg border font-black text-xs ' +
    (open ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-slate-500 border-[#cfe0f5] hover:bg-[#eef6ff]');
}

function toggleLiveDetails() {
  const d = document.getElementById('live-details');
  if (d) d.classList.toggle('hidden');
}

function renderScrapeSummary() {
  const box = document.getElementById('scrape-summary');
  const [a, b] = scrapeRange();
  const when = !a ? 'بدون تقييد تاريخ' : (a === b ? `انطلقت يوم <b>${a}</b>` : `انطلقت من <b>${a}</b> إلى <b>${b}</b>`);
  if (scrapeMode === 'market') {
    if (!a) { box.innerHTML = '⚠️ اختر يوماً أو فترة.'; return; }
    const days = Math.round((new Date(b) - new Date(a)) / 864e5) + 1;
    const mins = Math.max(1, Math.round(days * MARKET_ADS_PER_DAY / SCAN_PER_SEC / 60));
    const dur = mins >= 90 ? `~${(mins / 60).toFixed(1)} ساعة` : `~${mins} دقيقة`;
    box.innerHTML = `🌍 سيُسحب: <b>كل إعلانات المتاجر العربية في الجزائر</b> التي ${when}` +
      ` · <b>${days}</b> يوم · ~${(days * MARKET_ADS_PER_DAY).toLocaleString('en-US')} إعلان للفحص · المدة التقديرية <b>${dur}</b> من جهاز واحد` +
      (days > 62 ? '<br><span class="text-rose-700 font-bold">⚠️ الحد الأقصى 62 يوماً.</span>' : '');
    return;
  }
  const { kind, value } = detectScrapeInput(document.getElementById('q').value);
  const KIND = {
    keyword: ['🔤', 'كلمة مفتاحية', 'text-sky-700'], page: ['📄', 'صفحة فيسبوك', 'text-violet-700'],
    domain: ['🏪', 'متجر', 'text-emerald-700'], link: ['🔗', 'رابط مكتبة إعلانات', 'text-amber-700'],
  };
  const kEl = document.getElementById('q-kind');
  if (!kind) { kEl.innerHTML = ''; box.innerHTML = 'اكتب ما تريد سحبه بالأعلى.'; return; }
  if (kind === 'page' && !value) {
    kEl.innerHTML = '<span class="text-rose-700">⚠️ رابط الصفحة لا يحتوي رقمها (ID) — الصق رقم الصفحة أو رابط «كل إعلانات الصفحة» من مكتبة الإعلانات.</span>';
    box.innerHTML = ''; return;
  }
  const [ic, name, cls] = KIND[kind];
  kEl.innerHTML = `<span class="${cls}">✓ فهمتُها: ${ic} ${name} — <span dir="ltr" class="font-mono">${escT(value.length > 70 ? value.slice(0, 70) + '…' : value)}</span></span>`;
  const what = { keyword: `الإعلانات التي تحتوي «${escT(value)}»${document.getElementById('exact').checked ? ' (عبارة حرفية)' : ''}`,
                 page: 'كل إعلانات هذه الصفحة', domain: `كل الإعلانات التي تذكر المتجر <span dir="ltr">${escT(value)}</span>`,
                 link: 'نتائج رابط مكتبة الإعلانات كما هو (بنفس اللغة والفلاتر)' }[kind];
  box.innerHTML = `🔎 سيُسحب: ${what} — ${when} · المتاجر فقط` +
    (kind === 'link' && a ? '<br><span class="text-slate-500">التاريخ المختار يُطبَّق إن لم يحدد الرابط تاريخه.</span>' : '');
}

async function startEngine() {
  const [a, b] = scrapeRange();
  const body = { mode: scrapeMode === 'market' ? 'month_days' : 'targeted', date_preset: scrapePreset,
                 start_date_min: scrapePreset === 'day' ? document.getElementById('scrape-day').value : document.getElementById('scrape-custom-min').value,
                 start_date_max: document.getElementById('scrape-custom-max').value };
  if (scrapeMode === 'market') {
    if (!a) return alert('اختر يوماً أو فترة');
    const days = Math.round((new Date(b) - new Date(a)) / 864e5) + 1;
    if (!confirm(`سحب كل إعلانات المتاجر العربية في الجزائر لـ ${days} يوم (${a === b ? a : a + ' ← ' + b})؟\nيمكنك إيقافه في أي وقت، ويُكمل تلقائياً عند الانقطاع.`)) return;
  } else {
    const q = document.getElementById('q').value.trim();
    if (!q) return alert('اكتب كلمة أو رابط صفحة أو دومين متجر أو رابط مكتبة إعلانات');
    Object.assign(body, { query: q, country: document.getElementById('country-select').value,
                          exact: document.getElementById('exact').checked });
  }
  const r = await fetch('/api/start', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
  const d = await r.json();
  if (!d.ok) return alert('⚠️ ' + (d.error || 'تعذّر بدء السحب'));
  // Show the same launch-date window in the ads view.
  if (d.start_date_min) {
    document.getElementById('start-date-min').value = d.start_date_min;
    document.getElementById('start-date-max').value = d.start_date_max || '';
    loadAds();
  }
  document.getElementById('live-status-note').innerText = d.queued
    ? '🕒 السحب في قائمة الانتظار — سيبدأ تلقائياً بعد انتهاء السحب الحالي.'
    : (scrapeMode === 'market' ? `🌍 بدأ سحب السوق يوماً بيوم (${d.date_label || ''}).` : '▶ بدأ السحب.');
  loadJobs();
}
document.addEventListener('DOMContentLoaded', () => setScrapeMode('targeted'));

async function stopEngine(){
  const note = document.getElementById('live-status-note');
  if (note) { note.classList.remove('text-rose-700'); note.innerText = '⏹ جارٍ إرسال أمر الإيقاف…'; }
  let d = {};
  try {
    const r = await fetch('/api/stop', { method: 'POST' });
    d = await r.json();
  } catch (_e) {}
  liveScrapeState.paused = false;
  liveScrapeState.active = false;
  setState(false);
  loadJobs();
  if (note) {
    note.innerText = '⏹ تم إرسال أمر الإيقاف' +
      (d.cancelled ? ` — أُلغي ${d.cancelled} مهمة في الانتظار` : '') + '.';
  }
  setTimeout(() => {
    if (!liveScrapeState.active && !liveScrapeState.paused && !liveScrapeState.error) {
      document.getElementById('live-progress-box').classList.add('hidden');
    }
  }, 3000);
}
async function resetAll(){ if(!confirm('هل أنت متأكد من مسح جميع الإعلانات؟')) return; await fetch('/api/reset',{method:'POST'}); loadStats(); loadAds(); }
function exportData(f){ window.location = '/api/export/' + f + adminUserParam('?'); }
// ── SSE Events ──
let logLines = [];
function clearLogs() {
  logLines = [];
  const el = document.getElementById('log');
  if (el) el.innerText = '';
}
async function loadServerLogs() {
  try {
    const r = await fetch('/api/status');
    if (r.status === 401) return;
    const s = await r.json();
    if (Array.isArray(s.log) && s.log.length) {
      logLines = s.log.slice(-500);
      const el = document.getElementById('log');
      if (el) { el.innerText = logLines.join('\n'); el.scrollTop = el.scrollHeight; }
    }
  } catch (_e) {}
}
let es = null;
function startEventStream() {
  if (es) return;
  es = new EventSource('/api/stream');
  es.onmessage = onStreamMessage;
}
function onStreamMessage(e) {
  const m = JSON.parse(e.data); const d = m.data || {};
  if (m.type === 'log') {
    let text = String(d.text || '');
    if (!/^\d{2}:\d{2}:\d{2}/.test(text)) {
      text = '[' + new Date().toLocaleTimeString('en-GB') + '] ' + text;
    }
    logLines.push(text); logLines = logLines.slice(-500);
    const el = document.getElementById('log');
    if (el) { el.innerText = logLines.join('\n'); el.scrollTop = el.scrollHeight; }
  } else if (m.type === 'started') {
    liveScrapeState.paused = false;
    setState(true);
    liveScrapeState = { active: true, query: d.query || document.getElementById('q').value.trim(), reported: null, scraped: 0, stores: 0, error: '' };
    updateLiveProgressUI();
  } else if (m.type === 'reported') {
    liveScrapeState.reported = d.reported;
    updateLiveProgressUI();
  } else if (m.type === 'scrape_progress') {
    liveScrapeState.scraped = d.scraped;
    if (d.scanned) liveScrapeState.scanned = d.scanned;
    if (d.reported) liveScrapeState.reported = d.reported;
    if (d.stores) liveScrapeState.stores = d.stores;
    updateLiveProgressUI();
  } else if (m.type === 'slice_update') {
    document.getElementById('live-status-note').innerText =
      `🔄 جاري سحب الشريحة ${d.pass} من ${d.total_passes} (شهر ${d.label}) — تراكمي: ${d.accumulated} إعلان`;
  } else if (m.type === 'ad') {
    liveScrapeState.scraped++;
    adCache[String(d.ad_id)] = d;
    updateLiveProgressUI();
    // Show live ads only on page 1 of the "newest scraped" view, so a user
    // reading another page is not shifted under their eyes.
    adsTotal++;
    if (!groupMode && adsPage === 1 && document.getElementById('sort-by').value === 'collected_at') {
      ads.unshift(d); if (ads.length > adsPageSize) ads.pop(); renderAds();
    }
    renderAdsPager();
    document.getElementById('ads-total').innerText = adsTotal;
  } else if (m.type === 'progress') {
    loadJobs();
    liveScrapeState.active = true;
    if (d.progress) liveScrapeState.scraped = d.progress;
    if (d.reported) liveScrapeState.reported = d.reported;
    if (d.note) document.getElementById('live-status-note').innerText = d.note;
    updateLiveProgressUI();
  } else if (m.type === 'job') {
    loadJobs();
    loadKeywordChips();
    loadNicheChips();
    if (d.stored) { loadStats(); loadStores(); }
  } else if (m.type === 'error') {
    liveScrapeState.error = d.message || 'خطأ غير معروف';
    const note = document.getElementById('live-status-note');
    note.classList.add('text-rose-700');
    note.innerText = '⚠️ ' + liveScrapeState.error;
    document.getElementById('live-progress-box').classList.remove('hidden');
  } else if (m.type === 'finished') {
    liveScrapeState.active = false;
    setState(false);
    if (d.scraped) liveScrapeState.scraped = d.scraped;
    if (d.reported) liveScrapeState.reported = d.reported;
    if (d.stores) liveScrapeState.stores = d.stores;
    updateLiveProgressUI();
    const note = document.getElementById('live-status-note');
    if (d.status === 'interrupted') {
      note.innerText = `⏸ انقطع الاتصال بعد ${d.scraped} إعلان — التقدم محفوظ وسيُستأنف تلقائياً من نفس النقطة.`;
    } else if (d.status === 'stopped') {
      note.innerText = `⏹ أُوقف السحب. تم حفظ ${d.scraped} إعلاناً.`;
    } else if (d.status === 'failed') {
      note.innerText = `⛔ توقف السحب بسبب خطأ: ${d.error || ''}`;
      note.classList.add('text-rose-700');
    } else if (d.status === 'empty' && d.error) {
      liveScrapeState.error = d.error;
      note.classList.add('text-rose-700');
      note.innerText = '⚠️ ' + d.error;
    } else {
      note.innerText = `✅ اكتمل السحب بنجاح! تم حفظ ${d.scraped} إعلاناً و${d.stores} متجراً.`;
    }
  } else if (m.type === 'paused') {
    liveScrapeState.paused = true;
    const at = new Date(Date.now() + d.delay_min * 60000).toLocaleTimeString('ar-DZ', {hour: '2-digit', minute: '2-digit'});
    document.getElementById('live-progress-box').classList.remove('hidden');
    document.getElementById('live-status-note').innerText =
      `⏸ انقطع السحب بعد ${d.scraped} إعلان. التقدم محفوظ وسيُستأنف تلقائياً من نفس النقطة حوالي الساعة ${at} (محاولة ${d.attempt}/${d.max_attempts}). اضغط «إيقاف» لإلغائه.`;
    loadJobs();
  } else if (m.type === 'done') {
    setState(false);
    loadAds();
    loadKeywordChips();
    loadNicheChips();
    loadStats();
    if (liveScrapeState.error) {
      // Keep the reason visible instead of hiding it after 12s.
      const note = document.getElementById('live-status-note');
      note.classList.add('text-rose-700');
      note.innerText = '⚠️ ' + liveScrapeState.error;
      document.getElementById('live-progress-box').classList.remove('hidden');
      return;
    }
    setTimeout(() => {
      if (!liveScrapeState.active && !liveScrapeState.paused) {
        document.getElementById('live-progress-box').classList.add('hidden');
      }
    }, 12000);
  }
};

function setState(run) {
  document.getElementById('engine-state').innerText = run ? 'يعمل الآن…' : 'متوقف';
  document.getElementById('engine-state').className = 'text-xs px-3 py-1 rounded-full font-bold ' +
    (run ? 'bg-emerald-100 text-emerald-800 border border-emerald-300' : 'bg-slate-100 text-slate-600 border border-slate-200');
  document.getElementById('btn-start').disabled = run;
}

// Poll engine status periodically
setInterval(async () => {
  try {
    const r = await fetch('/api/status');
    if (!r.ok) return;  // signed out / not allowed: not a scrape error
    const s = await r.json();
    if (s.running) {
      setState(true);
      liveScrapeState.active = true;
      liveScrapeState.query = s.query || liveScrapeState.query;
      liveScrapeState.scraped = s.scraped || liveScrapeState.scraped;
      liveScrapeState.stores = s.stores || liveScrapeState.stores;
      if (s.scanned) liveScrapeState.scanned = s.scanned;
      if (s.reported) liveScrapeState.reported = s.reported;
      updateLiveProgressUI();
    } else if (liveScrapeState.active) {
      // Engine stopped: reconcile the UI even if the SSE "done" event was missed
      // (e.g. several dashboard tabs sharing the stream).
      liveScrapeState.active = false;
      liveScrapeState.scraped = s.scraped || liveScrapeState.scraped;
      liveScrapeState.stores = s.stores || liveScrapeState.stores;
      if (s.scanned) liveScrapeState.scanned = s.scanned;
      if (s.reported) liveScrapeState.reported = s.reported;
      setState(false);
      updateLiveProgressUI();
      loadAds(); loadStats();
      if (s.error) {
        liveScrapeState.error = s.error;
      }
      setTimeout(() => {
        if (!liveScrapeState.active && !liveScrapeState.paused && !liveScrapeState.error) {
          document.getElementById('live-progress-box').classList.add('hidden');
        }
      }, 12000);
    }
    // Always surface the last reason, even if the SSE "error" event was missed.
    if (s.error && !s.running) {
      const note = document.getElementById('live-status-note');
      if (note) { note.classList.add('text-rose-700'); note.innerText = '⚠️ ' + s.error; }
      const box = document.getElementById('live-progress-box');
      if (box) box.classList.remove('hidden');
    }
  } catch (_e) {}
}, 2500);
