// ── Settings: AI niche classification ──
function naiFmt(n) { return Number(n || 0).toLocaleString('en-US'); }

async function naiLoad() {
  const box = document.getElementById('nai-status');
  if (!box) return;
  const r = await fetch('/api/ai/niches');
  if (!r.ok) return;
  const d = await r.json();
  document.getElementById('nai-enabled').checked = !!d.enabled;
  document.getElementById('nai-model').value = d.model || '';
  document.getElementById('nai-daily').value = d.daily;
  const btn = document.getElementById('nai-backfill-btn');
  btn.textContent = d.backfill ? '⏸ إيقاف تصنيف المنتجات القديمة' : 'صنّف كل المنتجات الموجودة أيضاً';
  btn.dataset.on = d.backfill ? '1' : '';
  const lines = [];
  if (d.ready_error) lines.push(`<div class="text-rose-700">⚠️ ${escT(d.ready_error)} — اضبط مفتاح OpenRouter والموديل أعلاه.</div>`);
  lines.push(d.enabled
    ? `<div>✅ مفعّل · الموديل: <span class="font-mono" dir="ltr">${escT(d.effective_model || '—')}</span> · ${d.backfill ? 'يصنّف كل المنتجات (القديمة والجديدة)' : 'يصنّف السحوبات الجديدة فقط' + (d.since ? ' (منذ ' + escT(String(d.since).slice(0, 10)) + ')' : '')}</div>`
    : '<div>⏸ معطّل — المنتجات الجديدة تُصنَّف بالكلمات المفتاحية فقط.</div>');
  lines.push(`<div>اليوم: <b class="num">${naiFmt(d.done_today)}</b> منتج من حد <b class="num">${naiFmt(d.daily)}</b>`
    + (d.cost_today ? ` · التكلفة ≈ <b class="num">$${Number(d.cost_today).toFixed(4)}</b>` : '')
    + ` · بانتظار التصنيف: <b class="num">${naiFmt(d.pending)}</b> منتج`
    + ` · غير مصنّف في كل القاعدة: <b class="num">${naiFmt(d.pending_all)}</b> · النيشات: <b class="num">${naiFmt(d.niches)}</b></div>`);
  if (d.last_error) lines.push(`<div class="text-amber-700">آخر خطأ: ${escT(d.last_error)}</div>`);
  box.innerHTML = lines.join('');
}

async function naiPost(body) {
  const r = await fetch('/api/ai/niches', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const d = await r.json();
  if (!r.ok) { alert(d.error || 'فشل الحفظ'); return null; }
  naiLoad();
  return d;
}

function naiSave() {
  return naiPost({
    enabled: document.getElementById('nai-enabled').checked,
    model: document.getElementById('nai-model').value.trim(),
    daily: parseInt(document.getElementById('nai-daily').value || '0', 10),
  });
}

function naiBackfill(btn) {
  const on = !btn.dataset.on;
  if (on && !confirm('تصنيف كل المنتجات الموجودة في القاعدة؟ سيستهلك من رصيد OpenRouter (في حدود الحد اليومي).')) return;
  naiPost({backfill: on});
}

// ── Settings: backup restore ──
let restoreToken = '';
let restoreTimer = null;

async function restoreUpload(btn) {
  const input = document.getElementById('restore-file');
  const box = document.getElementById('restore-box');
  if (!input.files.length) { alert('اختر ملف النسخة الاحتياطية (.zip)'); return; }
  btn.disabled = true; btn.textContent = '⏳ جاري الرفع والفحص...';
  box.classList.remove('hidden');
  box.innerHTML = '⏳ جاري الرفع...';
  try {
    const fd = new FormData(); fd.append('file', input.files[0]);
    const r = await fetch('/api/backup/restore/upload', {method: 'POST', body: fd});
    const d = r.status === 413 ? {error: 'الملف أكبر من الحد المسموح على الخادم'} : await r.json();
    if (!r.ok) { box.innerHTML = `<div class="text-rose-700">❌ ${escT(d.error || 'فشل')}</div>`; return; }
    restoreToken = d.token;
    const rows = Object.entries(d.tables || {}).map(([t, n]) =>
      `<span class="inline-block px-2 py-0.5 rounded bg-white border border-slate-200 me-1 mb-1 ${d.will_restore.includes(t) ? '' : 'opacity-50'}">${escT(t)}: <b class="num">${naiFmt(n)}</b></span>`).join('');
    box.innerHTML = `<div>نسخة بتاريخ <b class="num">${escT(String(d.created_at || '').slice(0, 16).replace('T', ' '))}</b> UTC (${escT(d.backend || '')})</div>
      <div>${rows}</div>
      ${d.skipped.length ? `<div class="text-slate-500">لن تُستعاد: ${d.skipped.map(escT).join('، ')}</div>` : ''}
      <div class="text-rose-700">ستُستبدل ${d.will_restore.length} جداول بمحتوى هذه النسخة. اكتب <b>استعادة</b> للتأكيد:</div>
      <div class="flex gap-2"><input id="restore-confirm" class="border border-rose-300 rounded-lg px-2 py-1 text-xs" placeholder="استعادة">
        <button onclick="restoreStart(this)" class="bg-rose-600 hover:bg-rose-700 text-white rounded-lg px-4 py-1.5 font-bold text-xs">استعادة الآن</button></div>`;
  } catch (e) {
    box.innerHTML = `<div class="text-rose-700">❌ ${escT(e.message)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = 'فحص الملف';
  }
}

async function restoreStart(btn) {
  const confirmText = document.getElementById('restore-confirm').value.trim();
  btn.disabled = true;
  const r = await fetch('/api/backup/restore/start', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                      body: JSON.stringify({token: restoreToken, confirm: confirmText})});
  const d = await r.json();
  if (!r.ok) { alert(d.error || 'فشل'); btn.disabled = false; return; }
  restorePoll();
}

async function restorePoll() {
  clearTimeout(restoreTimer);
  const box = document.getElementById('restore-box');
  const d = await (await fetch('/api/backup/restore/status')).json();
  if (d.running) {
    box.innerHTML = `⏳ جاري الاستعادة... الجدول: <b>${escT(d.table || '')}</b> · <b class="num">${naiFmt(d.rows)}</b> صف`;
    restoreTimer = setTimeout(restorePoll, 1500);
    return;
  }
  if (d.error) {
    box.innerHTML = `<div class="text-rose-700">❌ فشلت الاستعادة ولم يتغير شيء: ${escT(d.error)}</div>`;
    return;
  }
  const total = Object.values(d.done || {}).reduce((a, b) => a + b, 0);
  box.innerHTML = `<div class="text-emerald-700">✅ تمت الاستعادة: <b class="num">${naiFmt(total)}</b> صف في ${Object.keys(d.done || {}).length} جداول. أعد تحميل الصفحة.</div>`;
}
