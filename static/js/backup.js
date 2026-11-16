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
  document.getElementById('nai-free').checked = !!d.free;
  naiRenderFree(d);
  naiLast = d;
  naiRenderChain();
  for (const [name, pv] of Object.entries(d.providers || {})) {
    document.getElementById(`nai-${name}-on`).checked = !!pv.on;
    document.getElementById(`nai-${name}-key`).value = '';
    document.getElementById(`nai-${name}-models`).value = (pv.models || []).join(', ');
    document.getElementById(`nai-${name}-status`).innerHTML = pv.key_unreadable
      ? '<span class="text-rose-600 font-bold">⚠️ المفتاح لا يُقرأ — أدخله من جديد</span>'
      : pv.key_set ? `✅ مفتاح محفوظ <span class="font-mono" dir="ltr">${escT(pv.key_hint)}</span>` : 'لا مفتاح';
    document.getElementById(`nai-${name}-picks`).innerHTML = '';
  }
  const btn = document.getElementById('nai-backfill-btn');
  btn.textContent = d.backfill ? '⏸ إيقاف تصنيف المنتجات القديمة' : 'صنّف كل المنتجات الموجودة أيضاً';
  btn.dataset.on = d.backfill ? '1' : '';
  const lines = [];
  if (d.ready_error) lines.push(`<div class="text-rose-700">⚠️ ${escT(d.ready_error)} — اضبط مفتاح OpenRouter والموديل أعلاه.</div>`);
  if (d.free && d.free_error) lines.push(`<div class="text-rose-700">⚠️ تعذّر جلب الموديلات المجانية: ${escT(d.free_error)}</div>`);
  lines.push(d.enabled
    ? `<div>✅ مفعّل · ${(d.chain || []).length > 1 ? `🔗 ${naiFmt(d.chain.length)} موديل في السلسلة · التالي: ` : 'الموديل: '}<span class="font-mono" dir="ltr">${escT(d.effective_model || '—')}</span> · ${d.backfill ? 'يصنّف كل المنتجات (القديمة والجديدة)' : 'يصنّف السحوبات الجديدة فقط' + (d.since ? ' (منذ ' + escT(String(d.since).slice(0, 10)) + ')' : '')}</div>`
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

// ── model tests (dry run on 10 products, nothing saved) ──
let naiLast = {};
const naiTests = {};  // model -> result | 'running'

function naiProvider(id) {
  const p = id.split(':')[0];
  return p === 'gemini' ? 'Gemini' : p === 'groq' ? 'Groq' : 'OpenRouter';
}

function naiTestSummary(res) {
  if (res === 'running') return '<span class="text-slate-500">⏳ جارٍ الفحص…</span>';
  if (!res) return '';
  if (!res.ok) return `<span class="text-rose-700 font-bold">❌ ${escT(String(res.error || 'لا جواب صالح').slice(0, 90))}</span>`;
  const color = res.answered < res.total ? ' text-amber-700' : ' text-emerald-700';
  return `<span class="font-bold${color}">✅ ${res.answered}/${res.total} · ${res.secs} ث</span>`
    + (res.compared ? ` <span class="text-slate-500">· تطابق ${res.agree}/${res.compared} مع السابق</span>` : '');
}

function naiRenderChain() {
  const box = document.getElementById('nai-chain');
  if (!box) return;
  const chain = naiLast.chain || [];
  const rest = naiLast.resting || {};
  box.innerHTML = chain.map((m, i) => {
    const res = naiTests[m];
    const r = rest[m];
    const q = escT(JSON.stringify(m));
    return `<div class="flex items-center gap-2 flex-wrap bg-white border border-slate-200 rounded-lg px-2 py-1.5">
      <span class="text-slate-400 w-5 text-center num">${i + 1}</span>
      <span class="px-1.5 rounded bg-slate-100 text-[10px] font-bold text-slate-600">${naiProvider(m)}</span>
      <span class="font-mono truncate max-w-[18rem]" dir="ltr" title="${escT(m)}">${escT(m)}</span>
      ${m === naiLast.last_model ? '<span class="text-emerald-700">✓ آخر نجاح</span>' : ''}
      ${r ? `<span class="text-amber-700">⏸ يرتاح ${Math.ceil(r / 60)} د</span>` : ''}
      <span class="ms-auto">${naiTestSummary(res)}</span>
      ${res && res !== 'running' ? `<button type="button" onclick="naiShowReport(${q})" class="text-[#174ea6] underline">التقرير</button>` : ''}
      <button type="button" onclick="naiTest(${q})" ${res === 'running' ? 'disabled' : ''} class="sbtn !py-1 !px-2">🧪 فحص</button>
    </div>`;
  }).join('') || '<div class="text-slate-400">لا يوجد موديل جاهز — أضف مفتاحاً وموديلاً ثم احفظ.</div>';
}

async function naiTest(model) {
  naiTests[model] = 'running';
  naiRenderChain();
  try {
    const r = await fetch('/api/ai/niches/test', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                  body: JSON.stringify({model})});
    const d = await r.json();
    naiTests[model] = r.ok ? d : {ok: false, model, error: d.error || ('HTTP ' + r.status)};
  } catch (err) {
    naiTests[model] = {ok: false, model, error: err.message};
  }
  naiRenderChain();
  naiShowReport(model);
  return naiTests[model];
}

async function naiTestAll(btn) {
  const chain = naiLast.chain || [];
  if (!chain.length) return;
  if (chain.length > 5 && !confirm(`فحص ${chain.length} موديلات واحداً بعد الآخر؟ كل فحص طلب واحد من حصتك المجانية.`)) return;
  btn.disabled = true;
  try { for (const m of chain) await naiTest(m); } finally { btn.disabled = false; }
  const ok = chain.filter(m => naiTests[m] && naiTests[m].ok);
  const box = document.getElementById('nai-test-report');
  box.classList.remove('hidden');
  box.className = 'p-3 rounded-xl text-xs border bg-slate-50 border-slate-200';
  box.innerHTML = `<div class="font-bold text-[#102a56] mb-1">النتيجة العامة: ${ok.length} من ${chain.length} موديلات تعمل.</div>`
    + chain.map(m => `<div class="flex gap-2"><span class="font-mono" dir="ltr">${escT(m)}</span><span class="ms-auto">${naiTestSummary(naiTests[m])}</span></div>`).join('')
    + '<div class="text-slate-500 mt-1">اضغط «التقرير» بجانب أي موديل لترى تصنيفاته.</div>';
}

function naiShowReport(model) {
  const res = naiTests[model];
  const box = document.getElementById('nai-test-report');
  if (!res || res === 'running') return;
  box.classList.remove('hidden');
  box.className = 'p-3 rounded-xl text-xs border ' + (res.ok ? 'bg-emerald-50 border-emerald-200' : 'bg-rose-50 border-rose-200');
  const head = `<div class="flex items-center gap-2 mb-2 flex-wrap"><b class="font-mono" dir="ltr">${escT(model)}</b>
    <button type="button" onclick="document.getElementById('nai-test-report').classList.add('hidden')" class="ms-auto text-slate-500">✕</button></div>`;
  if (!res.items) {
    box.innerHTML = head + `<div class="text-rose-700 font-bold break-all" dir="auto">❌ ${escT(res.error || 'فشل')}</div>`
      + (res.secs != null ? `<div class="text-slate-500 mt-1">بعد ${res.secs} ث</div>` : '');
    return;
  }
  const facts = [
    `أجاب عن <b>${res.answered}/${res.total}</b> منتج`,
    `أعطى نيشاً لـ <b>${res.with_niche}</b>`,
    `الوقت <b>${res.secs}</b> ث`,
    res.compared ? `تطابق النيش الرئيسي مع التصنيف السابق: <b>${res.agree}/${res.compared}</b>` : '',
    res.prompt_tokens != null ? `توكنات: ${res.prompt_tokens} دخول / ${res.completion_tokens} خروج` : '',
    res.cost ? `التكلفة: $${Number(res.cost).toFixed(5)}` : 'التكلفة: مجاني',
    res.finish_reason === 'length' ? '<span class="text-amber-700">⚠️ الجواب قُطع عند حد التوكنات</span>' : '',
  ].filter(Boolean).join(' · ');
  const rows = res.items.map((it, i) => `<tr class="border-t border-slate-200 align-top">
      <td class="py-1 pe-2 text-slate-400 num">${i + 1}</td>
      <td class="py-1 pe-2"><div class="text-slate-500 text-[10px]" dir="ltr">${escT(it.store)}</div><div dir="auto">${escT(it.text)}</div></td>
      <td class="py-1 pe-2 font-bold whitespace-nowrap">${!it.answered ? '<span class="text-rose-600">لم يُجب</span>'
        : it.niches.length ? it.niches.map(n => `${escT(n.name)} <span class="text-slate-400 num">${n.confidence}%</span>`).join('<br>')
        : '<span class="text-slate-400">لا نيش مناسب</span>'}</td>
      <td class="py-1 text-slate-500 whitespace-nowrap">${it.before.length ? escT(it.before.join('، ')) : '—'}</td>
    </tr>`).join('');
  box.innerHTML = head + `<div class="mb-2 leading-relaxed">${facts}</div>`
    + (res.raw ? `<div class="mb-2 text-rose-700 break-all" dir="auto">جواب الموديل: ${escT(res.raw)}</div>` : '')
    + `<div class="overflow-x-auto"><table class="w-full text-[11px]"><thead><tr class="text-slate-500">
        <th></th><th class="text-start">المنتج</th><th class="text-start">تصنيف الموديل</th><th class="text-start">التصنيف السابق</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
}

function naiRenderFree(d) {
  const box = document.getElementById('nai-free-list');
  const rest = d.resting || {};
  box.innerHTML = (d.free_models || []).map(m => {
    const r = rest[m.id];
    const tag = m.id === d.last_model ? ' <span class="text-emerald-700">✓ آخر نجاح</span>'
      : r ? ` <span class="text-amber-700">⏸ ${Math.ceil(r / 60)} د</span>` : '';
    return `<label class="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-white border border-slate-200 cursor-pointer">
      <input type="checkbox" data-free-model="${escT(m.id)}" ${m.on ? 'checked' : ''}>
      <span class="font-mono truncate">${escT(m.id)}</span>${tag}</label>`;
  }).join('') || '<div class="text-slate-400" dir="rtl">احفظ مفتاح OpenRouter أولاً لتظهر القائمة.</div>';
  naiFreeToggle();
}

function naiFreeToggle() {
  const on = document.getElementById('nai-free').checked;
  document.getElementById('nai-free-box').classList.toggle('hidden', !on);
  document.getElementById('nai-model-label').textContent = on ? 'موديل مفضّل (يُجرَّب أولاً إن كان مجانياً)' : 'موديل التصنيف';
}

function naiFreeAll(on) {
  document.querySelectorAll('[data-free-model]').forEach(c => { c.checked = on; });
  if (typeof settingsMarkDirty === 'function') settingsMarkDirty('nai', 'ai');
}

function naiSave() {
  const boxes = [...document.querySelectorAll('[data-free-model]')];
  const body = {
    enabled: document.getElementById('nai-enabled').checked,
    model: document.getElementById('nai-model').value.trim(),
    daily: parseInt(document.getElementById('nai-daily').value || '0', 10),
    free: document.getElementById('nai-free').checked,
  };
  if (boxes.length) body.free_skip = boxes.filter(c => !c.checked).map(c => c.dataset.freeModel);
  for (const name of ['gemini', 'groq']) {
    body[name + '_on'] = document.getElementById(`nai-${name}-on`).checked;
    body[name + '_models'] = document.getElementById(`nai-${name}-models`).value;
    const key = document.getElementById(`nai-${name}-key`).value.trim();
    if (key) body[name + '_key'] = key;
  }
  return naiPost(body);
}

async function naiClearKey(name) {
  if (!confirm('حذف المفتاح المحفوظ؟')) return;
  await naiPost({[name + '_clear']: true});
}

async function naiLoadProviderModels(name, btn) {
  btn.disabled = true;
  const picks = document.getElementById(`nai-${name}-picks`);
  picks.innerHTML = '<span class="text-slate-400">⏳ …</span>';
  try {
    const r = await fetch('/api/ai/niches/models', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: name, api_key: document.getElementById(`nai-${name}-key`).value.trim()})});
    const d = await r.json();
    if (!r.ok) { picks.innerHTML = `<span class="text-rose-600 w-full break-all text-[10px]" dir="ltr">❌ ${escT(String(d.error || 'فشل الجلب').slice(0, 160))}</span>`; return; }
    document.getElementById(`nai-${name}-list`).innerHTML = d.models.map(m => `<option value="${escT(m)}">`).join('');
    picks.innerHTML = d.models.map(m =>
      `<button type="button" onclick="naiAddModel('${name}', this.textContent)" class="px-2 py-0.5 rounded bg-slate-100 hover:bg-blue-100 font-mono text-[10px]">${escT(m)}</button>`).join('')
      + '<span class="text-slate-400 text-[10px] w-full" dir="rtl">اضغط على موديل لإضافته للقائمة.</span>';
  } finally { btn.disabled = false; }
}

function naiAddModel(name, model) {
  const el = document.getElementById(`nai-${name}-models`);
  const list = el.value.split(',').map(x => x.trim()).filter(Boolean);
  if (!list.includes(model)) list.push(model);
  el.value = list.join(', ');
  if (typeof settingsMarkDirty === 'function') settingsMarkDirty('nai', 'ai');
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
