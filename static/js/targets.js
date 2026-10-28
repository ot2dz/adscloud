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
          ${t.type === 'page' ? `<button data-val="${escT(t.target_value)}" onclick="toggleSnapshots(this.dataset.val)" class="text-xs px-2.5 py-1 rounded-lg bg-white hover:bg-[#eef6ff] text-slate-700 border border-[#cfe0f5] font-bold transition shadow-sm">📈 سجل النشاط</button>` : ''}
          <button onclick="scanTarget(${t.id})" class="text-xs px-2.5 py-1 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-bold transition shadow-sm">⚡ فحص الآن</button>
          <input type="checkbox" ${t.enabled ? 'checked' : ''} onchange="toggleTarget(${t.id}, this.checked)" title="تفعيل/إيقاف الفحص الدوري" class="cursor-pointer rounded text-blue-600">
          <button onclick="delTarget(${t.id})" class="text-rose-600 hover:text-rose-700 text-xs px-1.5 font-bold" title="إلغاء التتبع">✕</button>
        </div>
      </td>
    </tr>
    ${t.type === 'page' ? `<tr><td colspan="6" class="px-5"><div id="snap-${escT(t.target_value)}" class="hidden pb-3"></div></td></tr>` : ''}`;
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
  else if (d.ok) alert('⚡ تمت إضافة الفحص إلى قائمة الانتظار.');
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
