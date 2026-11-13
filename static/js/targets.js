// ── Competitors (per user) ──
async function loadTargets() {
  const r = await fetch('/api/targets' + adminUserParam('?')); const d = await r.json();
  const el = document.getElementById('targets-list');
  document.getElementById('schedule-off-note').classList.toggle('hidden', !!d.schedule_enabled);
  const list = d.targets || [];
  document.getElementById('targets-count').innerText = list.length ? `(${list.length})` : '';
  if (!list.length) {
    el.innerHTML = '<tr><td colspan="6" class="text-center py-8 text-slate-400 font-semibold">لا يوجد منافسون بعد. أضف أول صفحة أو متجر بالأعلى، أو تتبّع صفحة من إضافة المتصفح.</td></tr>';
    return;
  }
  const typeBadge = {
    page: 'bg-violet-50 text-violet-700 border-violet-200',
    domain: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    keyword: 'bg-sky-50 text-sky-700 border-sky-200',
    custom_url: 'bg-amber-50 text-amber-700 border-amber-200'
  };
  const typeAr = { page: 'صفحة', domain: 'متجر', keyword: 'كلمة', custom_url: 'رابط مخصص' };

  el.innerHTML = list.map(t => {
    // User-typed values: escaped for HTML and passed to handlers via data-*.
    const dataAttrs = `data-id="${Number(t.id)}" data-type="${escT(t.type)}" data-val="${escT(t.target_value)}" data-label="${escT(t.label || t.target_value)}"`;
    const adsCount = t.ads_count || 0;
    const act = t.activity;
    const activityCell = act
      ? `<div class="flex items-center gap-2">
           <span class="text-lg font-black num ${act.active_ads_count != null ? 'text-blue-700' : 'text-slate-300'}">${act.active_ads_count != null ? act.active_ads_count : '—'}</span>
           <span class="text-[10px] text-slate-500 leading-tight">إعلان نشط<br>${act.last_snapshot_at ? new Date(act.last_snapshot_at).toLocaleDateString('ar-DZ') : 'لا لقطات بعد'}</span>
         </div>
         <div class="w-28">${trackedSparkline(act.snapshots)}</div>`
      : '<span class="text-[11px] text-slate-400">—</span>';
    return `
    <tr class="hover:bg-slate-50/80 transition ${t.enabled ? '' : 'opacity-60'}">
      <td class="py-3 px-5"><span class="text-[10px] px-2.5 py-0.5 rounded-full font-bold border ${typeBadge[t.type] || 'bg-slate-100 text-slate-700'}">${typeAr[t.type] || escT(t.type)}</span>
        ${t.source === 'extension' ? '<div class="text-[10px] text-slate-400 mt-1">🧩 من الإضافة</div>' : ''}</td>
      <td class="px-5 py-2">
        <div class="flex items-center gap-2">
          <div class="font-bold text-[#102a56] hover:text-blue-600 cursor-pointer flex items-center gap-1.5 transition text-sm" ${dataAttrs} onclick="filterByTargetEl(this)" title="انقر لعرض إعلانات هذا المنافس فقط">
            <span>${escT(t.label || t.target_value)}</span>
            <span class="text-blue-500 text-xs">↗</span>
          </div>
          ${adsCount > 0 ? `<span class="text-[10px] px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 num font-bold">${adsCount} إعلان</span>` : `<span class="text-[10px] px-2 py-0.5 rounded-full bg-slate-100 text-slate-500 num">0</span>`}
        </div>
        <div class="text-[11px] text-slate-500 truncate max-w-xs font-mono">${escT(t.target_value)}</div>
        ${isAdmin() && !currentAdminUserFilter() ? `<div class="text-[10px] text-slate-400 mt-0.5" dir="ltr">👤 ${(t.tracked_by || []).map(escT).join('، ') || '—'}</div>` : ''}
      </td>
      <td class="px-5">${activityCell}</td>
      <td class="px-5 text-xs num text-slate-600 font-medium">كل ${t.interval_hours || 12} س</td>
      <td class="px-5 text-xs text-slate-500 font-mono">${t.last_scraped_at ? t.last_scraped_at.slice(0, 16).replace('T', ' ') : 'لم يُفحص بعد'}</td>
      <td class="px-5 text-center">
        <div class="flex items-center justify-center gap-2 flex-wrap">
          <button ${dataAttrs} onclick="filterByTargetEl(this)" class="text-xs px-2.5 py-1 rounded-lg bg-white hover:bg-[#eef6ff] text-[#174ea6] border border-[#9fbbe0] font-bold transition shadow-sm">👁️ إعلاناته</button>
          <button ${dataAttrs} onclick="openTargetActivityEl(this)" class="text-xs px-2.5 py-1 rounded-lg bg-white hover:bg-[#eef6ff] text-slate-700 border border-[#cfe0f5] font-bold transition shadow-sm">📈 سجل النشاط</button>
          <button onclick="scanTarget(${t.id})" class="text-xs px-2.5 py-1 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-bold transition shadow-sm">⚡ فحص الآن</button>
          <input type="checkbox" ${t.enabled ? 'checked' : ''} onchange="toggleTarget(${t.id}, this.checked)" title="تفعيل/إيقاف الفحص الدوري" class="cursor-pointer rounded text-blue-600">
          <button onclick="delTarget(${t.id})" class="text-rose-600 hover:text-rose-700 text-xs px-1.5 font-bold" title="إلغاء التتبع">✕</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

function filterByTargetEl(el) {
  const d = el.dataset;
  filterByTarget(Number(d.id), d.type, d.val, d.label);
}

function currentAdminUserFilter() {
  const sel = document.getElementById('admin-user-filter');
  return sel ? sel.value : '';
}

async function scanAllTargets() {
  const r = await fetch('/api/targets/scan-all' + adminUserParam('?'), { method: 'POST' });
  const d = await r.json();
  alert(d.enqueued ? `⚡ تمت إضافة ${d.enqueued} فحص إلى قائمة الانتظار.` : 'كل منافسيك في قائمة الانتظار بالفعل أو معطّلون.');
  loadTargets();
}

async function addTarget() {
  const type = document.getElementById('new-target-type').value;
  const val = document.getElementById('new-target-value').value.trim();
  const label = document.getElementById('new-target-label').value.trim();
  const interval = parseFloat(document.getElementById('new-target-interval').value) || 12;
  if (!val) return alert('الرجاء إدخال الهدف أولاً');

  const r = await fetch('/api/targets', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ type: type, target_value: val, label: label, interval_hours: interval, country: 'DZ' })
  });
  const d = await r.json();
  if (!d.ok) return alert(d.error || 'فشلت الإضافة');
  document.getElementById('new-target-value').value = '';
  document.getElementById('new-target-label').value = '';
  loadTargets();
  alert('✅ تمت الإضافة — بدأ أول فحص لهذا المنافس.');
}

async function delTarget(id) {
  const everyone = isAdmin() && !currentAdminUserFilter();
  if (!confirm(everyone ? 'حذف هذا المنافس لجميع المستخدمين؟' : 'إلغاء تتبّع هذا المنافس؟')) return;
  await fetch('/api/targets/' + id + adminUserParam('?'), { method: 'DELETE' });
  loadTargets();
}
async function toggleTarget(id, en) {
  await fetch('/api/targets/' + id + adminUserParam('?'), { method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ enabled: en }) });
  loadTargets();
}
async function scanTarget(id) {
  const r = await fetch('/api/targets/' + id + '/scan' + adminUserParam('?'), { method: 'POST' });
  const d = await r.json();
  if (d.already_queued) alert('⏳ هذا المنافس في قائمة الانتظار بالفعل.');
  else if (d.ok) alert('⚡ تمت إضافة الفحص إلى قائمة الانتظار — سيقارن النتائج ويعلّم الإعلانات المتوقفة.');
}

function trackDomain(dom) {
  showTab('targets');
  document.getElementById('new-target-type').value = 'domain';
  document.getElementById('new-target-value').value = dom;
  document.getElementById('new-target-label').value = dom;
}
function trackPage(pid, name) {
  showTab('targets');
  document.getElementById('new-target-type').value = 'page';
  document.getElementById('new-target-value').value = pid;
  document.getElementById('new-target-label').value = name || pid;
}

// ── Tracked pages (browser extension) ──
function trackedSparkline(snaps){
  const vals = (snaps||[]).map(s => Number(s.active_ads_count||0));
  if (vals.length < 2) return '';
  const w = 200, h = 40, max = Math.max(1, ...vals), step = w/(vals.length-1);
  const pts = vals.map((v,i) => `${(i*step).toFixed(1)},${(h-(v/max)*(h-6)-3).toFixed(1)}`).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" class="w-full h-10 mt-1"><polyline fill="none" stroke="#2563eb" stroke-width="2" points="${pts}"/></svg>`;
}
function pageAdsUrl(pid){
  return `https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL&is_targeted_country=false&media_type=all&search_type=page&sort_data[mode]=total_impressions&sort_data[direction]=desc&view_all_page_id=${pid}`;
}
async function toggleSnapshots(pid){
  const box = document.getElementById('snap-'+pid);
  if (!box) return;
  if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
  try {
    const r = await fetch('/api/tracked-pages/'+encodeURIComponent(pid)+'/snapshots'); const d = await r.json();
    const snaps = d.snapshots || [];
    box.innerHTML = snaps.length
      ? '<div class="max-h-44 overflow-auto border border-[#cfe0f5] rounded-xl mt-1"><table class="w-full text-xs"><thead class="bg-slate-50 text-slate-500"><tr><th class="text-right px-2 py-1.5 font-bold">الوقت</th><th class="text-right px-2 font-bold">نشط</th><th class="text-right px-2 font-bold">إجمالي</th></tr></thead><tbody>' +
        snaps.map(s => `<tr class="border-t border-slate-100"><td class="px-2 py-1 font-mono">${new Date(s.captured_at).toLocaleString('ar')}</td><td class="px-2 num font-bold text-emerald-700">${s.active_ads_count}</td><td class="px-2 num text-slate-600">${s.total_ads_count}</td></tr>`).join('') +
        '</tbody></table></div>'
      : '<div class="text-xs text-slate-400 py-3 text-center">لا لقطات محفوظة بعد.</div>';
    box.classList.remove('hidden');
  } catch (e) { box.textContent = 'تعذر تحميل اللقطات'; box.classList.remove('hidden'); }
}

// ── competitor activity timeline ──────────────────────────────────────
function openTargetActivityEl(el) {
  openActivity('/api/targets/' + encodeURIComponent(el.dataset.id) + '/activity',
    el.dataset.label || 'سجل النشاط');
}
function openGlobalActivityEl(el) {
  openActivity('/api/global/targets/' + encodeURIComponent(el.dataset.id) + '/activity',
    el.dataset.label || 'سجل النشاط');
}
function closeActivityModal() {
  const m = document.getElementById('activity-modal');
  if (m) { m.classList.add('hidden'); m.classList.remove('flex'); }
}
function _actFmt(v) {
  if (!v) return '—';
  const d = new Date(v);
  return isNaN(d.getTime()) ? escT(String(v).slice(0, 16).replace('T', ' ')) : d.toLocaleString('ar-DZ');
}
function _actChip(label, value, tone) {
  return `<div class="rounded-xl border ${tone} px-3 py-2">
    <div class="text-[10px] font-bold opacity-80">${label}</div>
    <div class="text-lg font-black num">${value || 0}</div></div>`;
}
function _actDate(v) {
  if (!v) return '—';
  return String(v).slice(0, 10);
}
function _actAdRow(a) {
  const status = a.current_active
    ? { label: 'نشط الآن', cls: 'bg-emerald-100 text-emerald-800 border-emerald-200' }
    : (a.current_active === false
      ? { label: 'متوقف', cls: 'bg-rose-100 text-rose-800 border-rose-200' }
      : { label: 'غير معروف', cls: 'bg-slate-100 text-slate-600 border-slate-200' });
  const periods = (a.periods || []).map(p => {
    const start = _actDate(p.start);
    const end = p.open ? 'الآن' : _actDate(p.end);
    const extra = p.open ? 'يعمل الآن' : 'متوقف';
    const detected = (!p.open && p.stop_detected_at)
      ? ` · اكتُشف التوقف: ${_actDate(p.stop_detected_at)}` : '';
    return `<div class="text-[11px] text-slate-600 flex gap-1.5">
      <span class="text-slate-400">•</span>
      <span>من <b class="font-mono">${start}</b> إلى <b class="font-mono">${end}</b>
      — ${extra} · <b class="num">${p.days || 0}</b> يوم${detected}</span></div>`;
  }).join('');
  const notes = [];
  if (a.last_stop_at) notes.push(`آخر توقف: ${_actDate(a.last_stop_at)}` +
    (a.last_stop_detected_at ? ` (اكتُشف: ${_actDate(a.last_stop_detected_at)})` : ''));
  if (a.relaunch_of) {
    const old = (a.periods || []).find(p => p.relaunch_of);
    notes.push(`بديل لإعلان توقف سابقاً: ${escT(a.relaunch_of)}` +
      (old && old.previous_stopped_at ? ` (توقف: ${_actDate(old.previous_stopped_at)})` : ''));
  }
  if ((a.relaunched_by || []).length) {
    notes.push(`أُعيد إطلاقه بإعلان جديد: ${a.relaunched_by.map(escT).join('، ')}`);
  }
  const adUrl = 'https://www.facebook.com/ads/library/?id=' + encodeURIComponent(a.ad_id || '');
  const body = escT(String(a.body || '').slice(0, 130));
  return `<div class="px-4 py-3 border-b border-slate-100">
    <div class="flex items-center justify-between gap-2 flex-wrap">
      <div class="flex items-center gap-2 min-w-0">
        <span class="text-[10px] font-bold px-2 py-0.5 rounded-full border ${status.cls}">${status.label}</span>
        <span class="text-[12px] font-bold text-[#102a56] truncate">${escT(a.page_name || 'إعلان')}</span>
        <span class="text-[10px] text-slate-400 font-mono">${escT(a.ad_id || '')}</span>
      </div>
      <div class="text-[10px] text-slate-500 font-bold">
        الدورات: <b class="num">${a.cycles || 0}</b> · إجمالي التشغيل: <b class="num">${a.total_running_days || 0}</b> يوم
      </div>
    </div>
    ${body ? `<div class="text-[11.5px] text-slate-700 mt-1 line-clamp-1">${body}</div>` : ''}
    <div class="mt-1.5 space-y-0.5">${periods || '<div class="text-[11px] text-slate-400">لا فترات مسجلة بعد.</div>'}</div>
    ${notes.length ? `<div class="text-[10.5px] text-slate-600 mt-1">${notes.join(' · ')}</div>` : ''}
    <div class="flex gap-3 mt-1 text-[10.5px] font-bold">
      ${a.ad_id ? `<a href="${adUrl}" target="_blank" rel="noopener" class="text-blue-700 hover:underline">فتح الإعلان</a>` : ''}
      ${a.link_url ? `<a href="${escT(a.link_url)}" target="_blank" rel="noopener" class="text-slate-600 hover:underline">رابط المنتج</a>` : ''}
    </div>
  </div>`;
}
function _actEvent(e) {
  const d = e.detail || {};
  const isStop = e.event === 'stopped';
  const isResume = e.event === 'resumed';
  const tone = isStop
    ? { box: 'border-rose-200 bg-rose-50', text: 'text-rose-800' }
    : (isResume
      ? { box: 'border-amber-200 bg-amber-50', text: 'text-amber-800' }
      : { box: 'border-emerald-200 bg-emerald-50', text: 'text-emerald-800' });
  const title = isStop ? 'توقف الإعلان' : (isResume ? 'عاد للعمل' : 'إطلاق إعلان جديد');
  const primary = [];
  if (isStop && e.period_start) {
    primary.push(`كان شغالاً من ${_actDate(e.period_start)} إلى ${_actDate(e.period_end)} (${e.period_days || 0} يوم) ثم توقف.`);
  } else if (isResume) {
    primary.push(`كان متوقفاً${d.previous_stopped_at ? ' منذ ' + _actDate(d.previous_stopped_at) : ''}` +
      `${d.downtime_days != null ? ' وعاد بعد ' + d.downtime_days + ' يوم' : ' وعاد للعمل'}.`);
  } else if (!isStop) {
    primary.push(`أُطلق الإعلان${d.start_date ? ' بتاريخ ' + _actDate(d.start_date) : ''}.`);
  }
  const notes = [];
  if (isStop) {
    if (e.stop_detected_at) notes.push('اكتُشف التوقف: ' + _actDate(e.stop_detected_at));
    if (d.last_seen_active) notes.push('آخر ظهور نشط: ' + _actDate(d.last_seen_active));
    if (d.days_active != null) notes.push('مدة التشغيل: ' + d.days_active + ' يوم');
  }
  if (d.relaunch_of) notes.push('بديل لإعلان توقف سابقاً: ' + d.relaunch_of +
    (d.previous_stopped_at ? ' (توقف: ' + _actDate(d.previous_stopped_at) + ')' : '') +
    (d.downtime_days != null ? ' — مدة التوقف ' + d.downtime_days + ' يوم' : ''));
  const body = String(e.body || '').slice(0, 150);
  const adUrl = 'https://www.facebook.com/ads/library/?id=' + encodeURIComponent(e.ad_id || '');
  return `<div class="border ${tone.box} rounded-xl p-3">
    <div class="flex items-center justify-between gap-2 flex-wrap">
      <span class="text-xs font-black ${tone.text}">${title}${d.relaunch_of ? ' · إعادة إطلاق' : ''}</span>
      <span class="text-[10px] text-slate-500 font-mono">${_actFmt(e.event_at)}</span>
    </div>
    <div class="text-[11px] text-slate-600 mt-1">${escT(e.page_name || '')}${e.store_domain ? ' · ' + escT(e.store_domain) : ''}</div>
    ${primary.length ? `<div class="text-[11.5px] font-bold text-slate-700 mt-1">${primary.map(escT).join(' ')}</div>` : ''}
    ${body ? `<div class="text-[11.5px] text-slate-700 mt-1 line-clamp-2">${escT(body)}</div>` : ''}
    ${notes.length ? `<div class="text-[11px] text-slate-600 mt-1">${notes.map(escT).join(' · ')}</div>` : ''}
    <div class="flex gap-3 mt-1.5 text-[11px] font-bold">
      ${e.ad_id ? `<a href="${adUrl}" target="_blank" rel="noopener" class="text-blue-700 hover:underline">فتح الإعلان</a>` : ''}
      ${e.link_url ? `<a href="${escT(e.link_url)}" target="_blank" rel="noopener" class="text-slate-600 hover:underline">رابط المنتج/المتجر</a>` : ''}
    </div>
  </div>`;
}
// ── Written report: what the advertiser did, product by product ──
function _actShort(d) {
  const x = new Date(String(d || '').slice(0, 10) + 'T00:00:00');
  return isNaN(x) ? '—' : `${String(x.getDate()).padStart(2, '0')}/${String(x.getMonth() + 1).padStart(2, '0')}/${x.getFullYear()}`;
}
function _actProductName(a) {
  try {
    const u = new URL(a.link_url);
    const path = decodeURIComponent(u.pathname).replace(/\/+$/, '').split('/').pop();
    if (path) return path.replace(/[-_]+/g, ' ').slice(0, 60);
  } catch (_e) { /* no link */ }
  return a.store_domain || 'منتج بدون رابط';
}
function _actReportHtml(ads) {
  if (!ads.length) return '<div class="text-[12px] text-slate-500 font-semibold">لا يوجد نشاط مسجل بعد.</div>';
  // Every running period of every ad, grouped by product.
  const prods = {};
  ads.forEach(a => {
    const key = a.product_key || a.link_url || a.ad_id;
    const pr = prods[key] = prods[key] || {name: _actProductName(a), link: a.link_url, runs: [], ads: new Set()};
    pr.ads.add(a.ad_id);
    (a.periods || []).forEach(p => pr.runs.push({a, p, st: String(p.start || '').slice(0, 10)}));
  });
  const list = Object.values(prods).filter(p => p.runs.length);
  list.forEach(p => {
    p.runs.sort((x, y) => x.st.localeCompare(y.st));
    p.last = p.runs.reduce((m, r) => (r.p.open ? '9999' : String(r.p.end || '').slice(0, 10)) > m ? (r.p.open ? '9999' : String(r.p.end || '').slice(0, 10)) : m, '');
    p.active = p.runs.some(r => r.p.open);
  });
  list.sort((a, b) => (b.active - a.active) || b.last.localeCompare(a.last));

  const since = new Date(); since.setDate(since.getDate() - 30);
  const s30 = since.toISOString().slice(0, 10);
  const runs = list.flatMap(p => p.runs);
  const launched30 = runs.filter(r => r.st >= s30).length;
  const stopped30 = runs.filter(r => !r.p.open && String(r.p.end || '').slice(0, 10) >= s30).length;
  const head = `<div class="text-[12.5px] text-slate-700 font-semibold leading-relaxed rounded-xl bg-slate-50 border border-slate-200 p-3">
      خلال آخر 30 يوماً: أطلق <b class="num">${launched30}</b> إعلاناً وأوقف <b class="num">${stopped30}</b>.
      يعمل الآن على <b class="num">${list.filter(p => p.active).length}</b> منتج من أصل <b class="num">${list.length}</b>.</div>`;

  const blocks = list.slice(0, 30).map(p => {
    let prevEnd = null;
    const lines = p.runs.map((r, i) => {
      const body = String(r.a.body || '').replace(/\s+/g, ' ').trim().slice(0, 70);
      const gap = (prevEnd && r.st > prevEnd)
        ? Math.round((new Date(r.st) - new Date(prevEnd)) / 864e5) : null;
      if (!r.p.open) {
        const e = String(r.p.end || '').slice(0, 10);
        if (!prevEnd || e > prevEnd) prevEnd = e;
      } else prevEnd = '9999';
      const verb = i === 0 ? 'أطلق إعلاناً' : (gap && gap >= 3 ? `بعد <b class="num">${gap}</b> يوم توقف، أطلق إعلاناً جديداً` : 'أطلق إعلاناً آخر');
      const run = r.p.open
        ? `<span class="text-emerald-700 font-bold">ما زال يعمل منذ <b class="num">${r.p.days || 1}</b> يوم</span>`
        : `اشتغل <b class="num">${r.p.days || 1}</b> يوم ثم <span class="text-rose-700 font-bold">توقف في ${_actShort(r.p.end)}</span>`;
      return `<li class="leading-relaxed"><span class="num text-slate-500">${_actShort(r.st)}</span> — ${verb}${body ? ` «${escT(body)}${(r.a.body || '').length > 70 ? '…' : ''}»` : ''} — ${run}.</li>`;
    }).join('');
    const timeline = typeof adTimelineHtml === 'function'
      ? adTimelineHtml(p.runs.map(r => ({start_date: r.st, days_active: r.p.days || 1, is_active: !!r.p.open}))) : '';
    return `<div class="rounded-xl border ${p.active ? 'border-emerald-200' : 'border-slate-200'} p-3 space-y-2">
        <div class="flex items-center justify-between gap-2 flex-wrap">
          <div class="font-bold text-[#102a56] text-[13px]">${p.active ? '🟢' : '⚫'} ${escT(p.name)}</div>
          <div class="text-[10.5px] text-slate-500 font-bold"><b class="num">${p.ads.size}</b> إعلان · <b class="num">${p.runs.length}</b> فترة تشغيل
            ${p.link ? ` · <a href="${escT(p.link)}" target="_blank" rel="noopener" class="text-blue-700 hover:underline">رابط المنتج</a>` : ''}</div>
        </div>
        <ol class="text-[12px] text-slate-700 space-y-1 list-none">${lines}</ol>
        ${timeline}
      </div>`;
  }).join('');
  return head + blocks + (list.length > 30 ? `<div class="text-[11px] text-slate-400">+${list.length - 30} منتج آخر في القائمة أدناه.</div>` : '');
}

async function openActivity(url, title) {
  const modal = document.getElementById('activity-modal');
  document.getElementById('activity-modal-title').innerText = title;
  document.getElementById('activity-summary').innerHTML = '';
  document.getElementById('activity-report').innerHTML =
    '<div class="py-4 text-center text-slate-500 font-bold text-sm">… جاري التحميل</div>';
  document.getElementById('activity-ads').innerHTML =
    '<div class="py-6 text-center text-slate-500 font-bold text-sm">… جاري التحميل</div>';
  document.getElementById('activity-timeline').innerHTML =
    '<div class="py-10 text-center text-slate-500 font-bold text-sm">… جاري التحميل</div>';
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  try {
    const r = await fetch(url);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'تعذر التحميل');
    const s = d.summary || {};
    document.getElementById('activity-summary').innerHTML =
      _actChip('نشط الآن', s.active_ads, 'border-emerald-200 bg-emerald-50 text-emerald-800') +
      _actChip('متوقف', s.stopped_ads, 'border-rose-200 bg-rose-50 text-rose-800') +
      _actChip('إطلاق 7 أيام', s.launched_7d, 'border-sky-200 bg-sky-50 text-sky-800') +
      _actChip('توقف 7 أيام', s.stopped_7d, 'border-amber-200 bg-amber-50 text-amber-800') +
      _actChip('عاد للعمل 7 أيام', s.resumed_7d, 'border-slate-200 bg-slate-50 text-slate-700');
    const ads = d.ads || [];
    document.getElementById('activity-report').innerHTML = _actReportHtml(ads);
    document.getElementById('activity-ads').innerHTML = ads.length
      ? ads.map(_actAdRow).join('')
      : '<div class="py-6 text-center text-slate-500 font-bold text-sm">لا توجد إعلانات مسجلة بعد.</div>';
    const events = (d.events || []).map(e => ({ t: e.event_at, kind: 'event', e }));
    const scans = (d.scans || []).map(x => ({ t: x.captured_at, kind: 'scan', s: x }));
    const items = events.concat(scans).sort((a, b) => String(b.t || '').localeCompare(String(a.t || '')));
    const box = document.getElementById('activity-timeline');
    if (!items.length) {
      box.innerHTML = '<div class="py-10 text-center text-slate-500 font-bold text-sm">لا يوجد سجل بعد. سيظهر هنا أول إطلاق/توقف بعد الفحص القادم.</div>';
      return;
    }
    box.innerHTML = items.map(x => x.kind === 'event'
      ? _actEvent(x.e)
      : `<div class="flex items-center justify-between gap-2 text-[11px] text-slate-500 border border-slate-200 rounded-lg px-3 py-1.5 bg-white">
           <span>فحص: <b class="num text-slate-700">${x.s.active_ads_count || 0}</b> نشط من <b class="num">${x.s.total_ads_count || 0}</b></span>
           <span class="font-mono">${_actFmt(x.s.captured_at)}</span>
         </div>`).join('');
  } catch (e) {
    const msg = '<div class="py-10 text-center text-rose-600 font-bold text-sm">' + escT(e.message || 'تعذر تحميل السجل') + '</div>';
    document.getElementById('activity-report').innerHTML = msg;
    document.getElementById('activity-ads').innerHTML = msg;
    document.getElementById('activity-timeline').innerHTML = msg;
  }
}
