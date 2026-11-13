// ── Winning products: ⭐ on ad cards + the "المنتجات الرابحة" tab ──
const WIN_STAGES = { watching: '👀 أراقب', testing: '🧪 أجرّب', ordered: '📦 طلبت عينة', dropped: '🚫 تركته' };
let winKeys = {};          // product_key -> saved id (current user)
let winProducts = [];

function winKeyOf(raw) { return raw.product_key || (raw.group && raw.group.key) || ''; }

async function loadWinningKeys() {
  try {
    const d = await (await fetch('/api/winning/keys')).json();
    winKeys = d.keys || {};
    refreshWinStars();
  } catch (e) { /* stars just stay empty */ }
}

function winStarHtml(raw, cls) {
  const key = winKeyOf(raw);
  const saved = key && winKeys[key];
  return `<button type="button" data-ad="${escT(raw.ad_id)}" data-pkey="${escT(key)}" onclick="toggleWinning(this)"
    title="${saved ? 'محفوظ في المنتجات الرابحة — اضغط للإزالة' : 'احفظ كمنتج رابح وتابعه'}"
    class="${cls} win-star ${saved ? 'bg-amber-100 border-amber-300' : ''}">${saved ? '⭐' : '☆'}</button>`;
}

function refreshWinStars() {
  document.querySelectorAll('button.win-star').forEach(b => {
    const saved = b.dataset.pkey && winKeys[b.dataset.pkey];
    b.textContent = saved ? '⭐' : '☆';
    b.classList.toggle('bg-amber-100', !!saved);
    b.classList.toggle('border-amber-300', !!saved);
    b.title = saved ? 'محفوظ في المنتجات الرابحة — اضغط للإزالة' : 'احفظ كمنتج رابح وتابعه';
  });
}

async function toggleWinning(btn) {
  const key = btn.dataset.pkey;
  if (key && winKeys[key]) {
    if (!confirm('إزالة هذا المنتج من المنتجات الرابحة؟ (تُحذف ملاحظاتك عنه)')) return;
    const r = await fetch('/api/winning/' + winKeys[key], { method: 'DELETE' });
    if (!(await r.json()).ok) return alert('تعذّرت الإزالة');
    delete winKeys[key];
  } else {
    btn.disabled = true;
    const r = await fetch('/api/winning', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                            body: JSON.stringify({ ad_id: btn.dataset.ad }) });
    const d = await r.json();
    btn.disabled = false;
    if (!d.ok) return alert(d.error || 'تعذّر الحفظ');
    winKeys[d.product.product_key] = d.product.id;
  }
  refreshWinStars();
  if (!document.getElementById('view-winning').classList.contains('hidden')) loadWinning();
}

function winDate(s) {
  if (!s) return '—';
  const d = new Date(String(s).length <= 10 ? s + 'T00:00:00Z' : s);
  if (isNaN(d)) return escT(String(s).slice(0, 10));
  return d.toLocaleDateString('ar-DZ', { timeZone: 'Africa/Algiers', day: 'numeric', month: 'short' });
}
function winDateTime(s) {
  const d = new Date(s);
  if (isNaN(d)) return escT(String(s || '').slice(0, 16));
  return d.toLocaleString('ar-DZ', { timeZone: 'Africa/Algiers', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function winEventText(e) {
  const det = e.detail || {};
  if (e.event === 'launched') {
    return det.relaunch_of
      ? `🔁 <b>أعاد إطلاق المنتج</b>${det.downtime_days != null ? ` بعد توقف ${det.downtime_days} يوم` : ''}`
      : '🆕 أطلق إعلاناً جديداً';
  }
  if (e.event === 'stopped') return `⏸️ أوقف إعلاناً${det.days_active ? ` بعد ${det.days_active} يوم تشغيل` : ''}`;
  if (e.event === 'resumed') return `▶️ أعاد تشغيل إعلان${det.downtime_days != null ? ` بعد ${det.downtime_days} يوم` : ''}`;
  return escT(e.event);
}

async function loadWinning() {
  const el = document.getElementById('winning-list');
  try {
    const d = await (await fetch('/api/winning' + adminUserParam('?'))).json();
    winProducts = d.products || [];
    const sel = document.getElementById('winning-stage-filter');
    if (sel.options.length <= 1) {
      Object.entries(WIN_STAGES).forEach(([v, t]) => sel.insertAdjacentHTML('beforeend', `<option value="${v}">${t}</option>`));
    }
    const info = document.getElementById('winning-check-info');
    info.textContent = d.check_open ? '⏳ فحص المنتجات الرابحة قيد التنفيذ…'
      : (d.last_check ? 'آخر فحص: ' + winDateTime(d.last_check) : 'لم يُجرَ أي فحص بعد — أول فحص خلال دقائق من حفظ أول منتج.');
    const btn = document.getElementById('winning-check-btn');
    if (btn) btn.classList.toggle('hidden', !isAdmin());
    renderWinning();
  } catch (e) {
    el.innerHTML = '<div class="text-rose-600 text-sm font-bold">تعذّر تحميل المنتجات الرابحة.</div>';
  }
}

function renderWinning() {
  const el = document.getElementById('winning-list');
  const stage = document.getElementById('winning-stage-filter').value;
  const list = winProducts.filter(p => !stage || p.stage === stage);
  if (!list.length) {
    el.innerHTML = `<div class="md:col-span-2 bg-white border border-dashed border-[#cfe0f5] rounded-2xl p-8 text-center text-slate-500 text-sm font-semibold">
      ${winProducts.length ? 'لا منتجات بهذه الحالة.' : 'لا منتجات رابحة بعد. اضغط ☆ في بطاقة أي إعلان في بنك الإعلانات لحفظ منتجه هنا.'}</div>`;
    return;
  }
  el.innerHTML = list.map(winCardHtml).join('');
}

function winCardHtml(p) {
  const active = Number(p.active_ads) > 0;
  const w = p.week || {};
  const le = p.last_event;
  const title = escT(p.page_name || 'صفحة غير معروفة');
  const body = escT((p.body || '').slice(0, 160));
  const stageOpts = Object.entries(WIN_STAGES).map(([v, t]) => `<option value="${v}" ${p.stage === v ? 'selected' : ''}>${t}</option>`).join('');
  const weekParts = [];
  if (w.launched) weekParts.push(`<span class="text-emerald-700">+${w.launched} جديد</span>`);
  if (w.resumed) weekParts.push(`<span class="text-sky-700">▶️ ${w.resumed} عاد</span>`);
  if (w.stopped) weekParts.push(`<span class="text-slate-500">⏸️ ${w.stopped} توقف</span>`);
  return `
  <article class="bg-white border ${active ? 'border-emerald-200' : 'border-[#d7e3f4]'} rounded-2xl overflow-hidden shadow-[0_6px_18px_rgba(23,43,77,0.05)] flex flex-col">
    <div class="flex gap-3 p-3.5">
      <div class="w-28 h-28 shrink-0 rounded-xl overflow-hidden bg-[#eef3f9] flex items-center justify-center">
        ${p.image ? `<img src="${escT(p.image)}" loading="lazy" class="w-full h-full object-cover" onerror="this.remove()">` : '<span class="text-3xl opacity-40">🖼️</span>'}
      </div>
      <div class="min-w-0 flex-1 space-y-1">
        <div class="flex items-center justify-between gap-2">
          <h3 class="font-bold text-[15px] text-[#102a56] truncate" title="${title}">${title}</h3>
          ${active
            ? `<span class="shrink-0 text-[11px] font-bold px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700">🟢 نشط · ${p.active_ads}</span>`
            : `<span class="shrink-0 text-[11px] font-bold px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">🔴 متوقف</span>`}
        </div>
        <div class="text-[11.5px] text-slate-500 font-mono truncate" dir="ltr" style="text-align:right">${escT(p.store_domain || '')}</div>
        ${body ? `<p class="text-[12px] text-slate-700 leading-relaxed line-clamp-2">${body}</p>` : ''}
        ${p.owner_email && isAdmin() ? `<div class="text-[10.5px] text-slate-400">👤 ${escT(p.owner_email)}</div>` : ''}
      </div>
    </div>
    <div class="px-3.5 grid grid-cols-4 gap-2 text-center">
      <div class="bg-slate-50 rounded-lg py-1.5"><div class="num font-extrabold text-slate-800">${p.total_ads}</div><div class="text-[10.5px] text-slate-500">إعلان كلي</div></div>
      <div class="bg-slate-50 rounded-lg py-1.5"><div class="num font-extrabold text-slate-800">${p.max_days || 0}</div><div class="text-[10.5px] text-slate-500">أقصى أيام</div></div>
      <div class="bg-slate-50 rounded-lg py-1.5"><div class="font-bold text-slate-800 text-[12px]">${winDate(p.first_launch)}</div><div class="text-[10.5px] text-slate-500">أول إطلاق</div></div>
      <div class="bg-slate-50 rounded-lg py-1.5"><div class="font-bold text-slate-800 text-[12px]">${winDate(p.last_launch)}</div><div class="text-[10.5px] text-slate-500">آخر إطلاق</div></div>
    </div>
    <div class="px-3.5 pt-2 text-[11.5px] font-semibold text-slate-600 flex flex-wrap gap-x-3 gap-y-1">
      ${weekParts.length ? `<span>آخر 7 أيام: ${weekParts.join(' · ')}</span>` : '<span class="text-slate-400">لا تغيير في آخر 7 أيام</span>'}
      ${le ? `<span>آخر حدث: ${winEventText(le)} <span class="text-slate-400">(${winDateTime(le.event_at)})</span></span>` : ''}
    </div>
    <div class="p-3.5 space-y-2 mt-auto">
      <div class="flex items-center gap-2">
        <select onchange="saveWinning(${p.id}, {stage: this.value})" class="text-xs bg-white text-slate-700 font-bold rounded-lg px-2.5 py-2 border border-[#b7c9e2]">${stageOpts}</select>
        <span id="win-saved-${p.id}" class="text-[11px] text-emerald-600 font-bold"></span>
      </div>
      <textarea id="win-notes-${p.id}" rows="2" placeholder="ملاحظاتك: سعر الشراء، المورد، طلبت عينة…" onblur="saveWinningNotes(${p.id})"
        class="w-full text-[12.5px] rounded-lg border border-[#cfe0f5] px-3 py-2 focus:outline-none focus:border-blue-400">${escT(p.notes || '')}</textarea>
      <div class="flex items-center gap-1.5 flex-wrap">
        <button type="button" data-key="${escT(p.product_key)}" data-title="${title} — كل إعلانات المنتج"
          onclick="openGroupModal(this.dataset.key, this.dataset.title)" class="h-8 px-3 rounded-lg bg-[#174ea6] hover:bg-[#123f87] text-white font-bold text-xs">📦 كل الإعلانات</button>
        <button type="button" onclick="toggleWinTimeline(${p.id}, this)" class="h-8 px-3 rounded-lg border border-[#cfe0f5] text-[#174ea6] font-bold text-xs hover:bg-[#eef5ff]">🕒 السجل</button>
        ${p.link_url ? `<a href="${safeUrl(p.link_url)}" target="_blank" rel="noopener" class="h-8 px-3 rounded-lg border border-[#cfe0f5] text-[#174ea6] font-bold text-xs hover:bg-[#eef5ff] flex items-center">🛒 صفحة المنتج</a>` : ''}
        <button type="button" onclick="removeWinning(${p.id})" class="h-8 px-3 rounded-lg border border-rose-200 text-rose-600 font-bold text-xs hover:bg-rose-50 ms-auto">🗑 إزالة</button>
      </div>
      <div id="win-timeline-${p.id}" class="hidden"></div>
    </div>
  </article>`;
}

async function saveWinning(id, body) {
  const r = await fetch('/api/winning/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                                                body: JSON.stringify(body) });
  const d = await r.json();
  if (!d.ok) return alert(d.error || 'تعذّر الحفظ');
  const p = winProducts.find(x => x.id === id);
  if (p) Object.assign(p, { notes: d.product.notes, stage: d.product.stage });
  const tag = document.getElementById('win-saved-' + id);
  if (tag) { tag.textContent = '✓ حُفظ'; setTimeout(() => { tag.textContent = ''; }, 1500); }
}

function saveWinningNotes(id) {
  const el = document.getElementById('win-notes-' + id);
  const p = winProducts.find(x => x.id === id);
  if (!el || (p && (p.notes || '') === el.value)) return;
  saveWinning(id, { notes: el.value });
}

async function removeWinning(id) {
  if (!confirm('إزالة هذا المنتج من المنتجات الرابحة؟ (تُحذف ملاحظاتك عنه)')) return;
  const r = await fetch('/api/winning/' + id, { method: 'DELETE' });
  if (!(await r.json()).ok) return alert('تعذّرت الإزالة');
  Object.keys(winKeys).forEach(k => { if (winKeys[k] === id) delete winKeys[k]; });
  refreshWinStars();
  loadWinning();
}

async function toggleWinTimeline(id, btn) {
  const box = document.getElementById('win-timeline-' + id);
  if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  box.innerHTML = '<div class="text-xs text-slate-400 py-2">جاري التحميل…</div>';
  const key = btn.closest('article, div[data-win]')?.querySelector('[data-key]')?.dataset.key || '';
  const [d, g] = await Promise.all([
    fetch('/api/winning/' + id + '/events').then(r => r.json()),
    key ? fetch('/api/ads/group?key=' + encodeURIComponent(key) + adminUserParam('&')).then(r => r.json()).catch(() => ({})) : Promise.resolve({}),
  ]);
  const ev = (d.events || []);
  const story = (g.ads || []).length
    ? `<div class="space-y-2 py-2"><div class="text-[11px] font-bold text-slate-600">📝 ماذا فعل المعلن بهذا المنتج</div>
        ${adStoryHtml(g.ads)}${adTimelineHtml(g.ads)}</div>` : '';
  box.innerHTML = story + (ev.length
    ? `<ol class="border-s-2 border-[#cfe0f5] ms-2 space-y-1.5 py-1">${ev.map(e => `
        <li class="ps-3 text-[12px] text-slate-700"><span class="text-slate-400 font-mono text-[11px]">${winDateTime(e.event_at)}</span> · ${winEventText(e)}</li>`).join('')}</ol>`
    : '<div class="text-xs text-slate-400 py-2">لا أحداث مسجلة بعد لهذا المنتج.</div>');
}

async function checkWinningNow(btn) {
  btn.disabled = true;
  const d = await (await fetch('/api/winning/check', { method: 'POST' })).json();
  btn.disabled = false;
  if (!d.ok) return alert(d.error || 'تعذّر بدء الفحص');
  loadWinning();
}
