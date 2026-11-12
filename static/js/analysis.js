// ── AI competitor analysis (store link → dossier → report) ──
let anPollTimer = null;

function anFmt(n) { return (n == null ? '—' : Number(n).toLocaleString('en-US')); }

// Small Markdown renderer. Everything is HTML-escaped FIRST (the report can
// quote competitor ad texts), then only a fixed set of tags is produced.
function mdToHtml(md) {
  const inline = s => escT(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_]+)__/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
    .replace(/\[(T\d+)\]/g, '<span class="an-ref">$1</span>');
  const lines = String(md || '').replace(/\r/g, '').split('\n');
  const out = [];
  let i = 0;
  const isTableSep = l => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*```/.test(l)) {
      const buf = []; i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++; out.push(`<pre>${escT(buf.join('\n'))}</pre>`); continue;
    }
    let m;
    if ((m = l.match(/^(#{1,6})\s+(.*)$/))) {
      const lv = Math.min(4, m[1].length + 1);
      out.push(`<h${lv}>${inline(m[2])}</h${lv}>`); i++; continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l)) { out.push('<hr>'); i++; continue; }
    if (l.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = cells(l); i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(cells(lines[i++]));
      out.push('<div class="an-table"><table><thead><tr>' + head.map(h => `<th>${inline(h)}</th>`).join('') +
        '</tr></thead><tbody>' + rows.map(r => '<tr>' + r.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') +
        '</tbody></table></div>');
      continue;
    }
    if (/^\s*>/.test(l)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
      out.push(`<blockquote>${buf.map(inline).join('<br>')}</blockquote>`); continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(l)) {
      const ordered = /^\s*\d+[.)]\s+/.test(l);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        const raw = lines[i++];
        const depth = Math.min(3, Math.floor((raw.match(/^\s*/)[0].length) / 2));
        items.push(`<li style="margin-inline-start:${depth * 1.2}rem">${inline(raw.replace(/^\s*([-*+]|\d+[.)])\s+/, ''))}</li>`);
      }
      out.push(ordered ? `<ol>${items.join('')}</ol>` : `<ul>${items.join('')}</ul>`); continue;
    }
    if (!l.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*([-*+]|\d+[.)])\s+|\s*>|\s*```)/.test(lines[i])
           && !(lines[i].includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1]))) buf.push(lines[i++]);
    out.push(`<p>${buf.map(inline).join('<br>')}</p>`);
  }
  return out.join('\n');
}

function analyzeStore(domain) {
  showTab('analysis');
  document.getElementById('an-store').value = domain || '';
  anPreview();
}

function anStoreValue() { return document.getElementById('an-store').value.trim(); }

async function anPreview() {
  const store = anStoreValue();
  const box = document.getElementById('an-preview');
  if (!store) { box.classList.add('hidden'); return; }
  const btn = document.getElementById('an-preview-btn');
  btn.disabled = true; btn.textContent = '⏳ جاري الفحص...';
  box.classList.remove('hidden');
  box.innerHTML = '<div class="text-xs text-slate-500 font-bold">⏳ جاري جمع إعلانات المتجر...</div>';
  try {
    const r = await fetch('/api/analysis/preview', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({store, lang: document.getElementById('an-lang').value,
                            note: document.getElementById('an-note').value})
    });
    const d = await r.json();
    box.innerHTML = anPreviewHtml(d);
  } catch (e) {
    box.innerHTML = `<div class="p-3 rounded-xl bg-rose-50 text-rose-700 text-xs font-bold">خطأ: ${escT(e.message)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = '🔍 فحص الإعلانات';
  }
}

function anPreviewHtml(d) {
  const s = d.store || {};
  if (s.error) return `<div class="p-3 rounded-xl bg-rose-50 text-rose-700 text-xs font-bold">${escT(s.error_text || s.error)}</div>`;
  if (!d.ads_found) {
    const sug = (s.suggestions || []).map(x =>
      `<button type="button" data-d="${escT(x.store_domain)}" onclick="analyzeStore(this.dataset.d)" class="px-2 py-1 rounded-lg border border-[#b7c9e2] font-mono hover:bg-[#eef6ff]" dir="ltr">${escT(x.store_domain)} <span class="text-slate-400">(${x.ads})</span></button>`).join(' ');
    return `<div class="p-3 rounded-xl bg-amber-50 border border-amber-200 text-amber-800 text-xs font-bold space-y-2">
      <div>لا توجد إعلانات للمتجر <span class="font-mono" dir="ltr">${escT(s.host)}</span> في قاعدة البيانات. اسحب إعلاناته أولاً من تبويب تتبع المنافسين.</div>
      ${sug ? `<div class="flex flex-wrap gap-1.5 items-center">هل تقصد: ${sug}</div>` : ''}</div>`;
  }
  const notes = [];
  if ((s.notes || []).includes('subdomains_included')) notes.push(`يشمل ${s.domains.length} دومينات فرعية: <span class="font-mono" dir="ltr">${s.domains.map(escT).join(', ')}</span>`);
  if ((s.notes || []).includes('many_subdomains')) notes.push('هذا الدومين له متاجر فرعية كثيرة (منصة؟) — حُلّل الدومين نفسه فقط.');
  if (d.texts_omitted) notes.push(`أُرسلت أهم ${d.texts_sent} نصاً فريداً فقط، وأُهمل ${d.texts_omitted} (يمكن رفع الحد من الإعدادات).`);
  const ctx = d.context && d.tokens_in + d.max_output_tokens > d.context
    ? `<div class="text-rose-700">⚠️ الحجم يتجاوز سياق الموديل (${anFmt(d.context)} توكن) — خفّض عدد النصوص من الإعدادات.</div>` : '';
  const cost = d.cost_usd ? ` · التكلفة التقريبية: <b>$${d.cost_usd.input}</b> مدخلات، حتى <b>$${d.cost_usd.max}</b> كحد أقصى` : '';
  const stat = (label, v, tone) => `<div class="rounded-xl border ${tone || 'border-[#d7e3f4] bg-[#f8fbff]'} px-3 py-2"><div class="text-[10px] text-slate-500 font-bold">${label}</div><div class="text-base font-extrabold text-[#102a56] num">${v}</div></div>`;
  const top = (d.top_texts || []).map(t => `<div class="flex gap-2 items-start py-1.5 border-b border-slate-100 last:border-0">
      <span class="an-ref shrink-0">${t.id}</span>
      <span class="shrink-0 text-[10px] font-bold text-slate-500 num">×${t.ads} · ${t.active} نشط · ${t.max_days} يوم</span>
      <span class="text-slate-700 truncate" dir="auto">${escT(t.text || '(بدون نص)')}</span></div>`).join('');
  const blocked = d.ready_error
    ? `<div class="p-2.5 rounded-xl bg-rose-50 text-rose-700 font-bold">⚠️ ${escT(d.ready_error)} — ${isAdmin() ? 'اضبطه من الإعدادات ← الذكاء الاصطناعي.' : 'اطلب من المسؤول ضبطه.'}</div>` : '';
  const quota = d.quota_left != null ? ` · متبقٍ اليوم: <b>${d.quota_left}</b>` : '';
  return `<div class="space-y-3 text-xs">
    <div class="font-bold text-slate-700">المتجر: <span class="font-mono text-blue-700" dir="ltr">${escT(s.host)}</span> · ${escT(s.platform || '')}
      · أول إطلاق ${escT(d.first_launch || '—')} · آخر إطلاق ${escT(d.last_launch || '—')}</div>
    <div class="grid grid-cols-2 md:grid-cols-5 gap-2">
      ${stat('إعلانات موجودة', anFmt(d.ads_found))}
      ${stat('نشطة', anFmt(d.active), 'border-emerald-200 bg-emerald-50')}
      ${stat('نصوص فريدة', anFmt(d.unique_texts))}
      ${stat('إعلانات مكررة النص', anFmt(d.duplicate_ads))}
      ${stat('توكنات تقريبية', '≈ ' + anFmt(d.tokens_in), 'border-violet-200 bg-violet-50')}
    </div>
    <div class="text-slate-500 font-semibold">≈ ${anFmt(d.tokens_in)} توكن مدخلات + حتى ${anFmt(d.max_output_tokens)} للرد · الموديل: <span class="font-mono" dir="ltr">${escT(d.model || '—')}</span>${cost}${quota}</div>
    ${ctx}
    ${notes.map(n => `<div class="text-slate-600">ℹ️ ${n}</div>`).join('')}
    ${top ? `<div class="rounded-xl border border-[#d7e3f4] px-3 py-1">${top}</div>` : ''}
    ${blocked}
    <button id="an-run-btn" onclick="anRun(this)" ${d.ready_error ? 'disabled' : ''}
      class="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed rounded-xl px-6 py-2.5 font-bold text-white transition text-xs shadow-sm">🤖 حلّل الآن</button>
  </div>`;
}

async function anRun(btn) {
  btn.disabled = true; btn.textContent = '⏳ جاري الإرسال...';
  try {
    const r = await fetch('/api/analysis/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({store: anStoreValue(), lang: document.getElementById('an-lang').value,
                            note: document.getElementById('an-note').value})
    });
    const d = await r.json();
    if (!r.ok || !d.id) { alert(d.error || 'فشل التحليل'); btn.disabled = false; btn.textContent = '🤖 حلّل الآن'; return; }
    btn.textContent = '✅ بدأ التحليل';
    anOpen(d.id);
    anLoadReports();
  } catch (e) {
    alert('خطأ: ' + e.message); btn.disabled = false; btn.textContent = '🤖 حلّل الآن';
  }
}

async function anOpen(id) {
  clearTimeout(anPollTimer);
  const wrap = document.getElementById('an-report-wrap');
  const head = document.getElementById('an-report-head');
  const body = document.getElementById('an-report');
  wrap.classList.remove('hidden');
  const r = await fetch('/api/analysis/reports/' + id);
  if (!r.ok) { body.innerHTML = '<p>التقرير غير موجود.</p>'; return; }
  const d = await r.json();
  const when = (d.created_at || '').slice(0, 16).replace('T', ' ');
  const usage = d.prompt_tokens != null
    ? ` · ${anFmt(d.prompt_tokens)} + ${anFmt(d.completion_tokens)} توكن` + (d.cost != null ? ` · $${Number(d.cost).toFixed(4)}` : '') : '';
  head.innerHTML = `<div>
      <div class="font-extrabold text-[#102a56]">تقرير: <span class="font-mono" dir="ltr">${escT(d.store_domain)}</span></div>
      <div class="text-[11px] text-slate-500 font-semibold mt-0.5">${escT(when)} UTC · ${anFmt(d.ads_count)} إعلان · ${anFmt(d.unique_texts)} نص فريد · <span class="font-mono" dir="ltr">${escT(d.model || '')}</span>${usage}</div>
      ${d.note ? `<div class="text-[11px] text-slate-600 mt-1" dir="auto">📝 ${escT(d.note)}</div>` : ''}
    </div>
    <div class="flex gap-2">
      ${d.status === 'done' ? `<button onclick="anCopy()" class="text-xs px-3 py-1.5 rounded-lg border border-[#b7c9e2] font-bold text-slate-700 hover:bg-slate-50">📋 نسخ</button>` : ''}
      <button onclick="document.getElementById('an-report-wrap').classList.add('hidden')" class="text-xs px-3 py-1.5 rounded-lg border border-[#b7c9e2] font-bold text-slate-700 hover:bg-slate-50">✕</button>
    </div>`;
  if (d.status === 'running') {
    body.innerHTML = '<div class="flex items-center gap-2 text-slate-500 font-bold"><span class="an-spin"></span> الذكاء الاصطناعي يحلل الإعلانات... قد يستغرق دقيقة أو دقيقتين. يمكنك مغادرة الصفحة، التقرير يُحفظ تلقائياً.</div>';
    anPollTimer = setTimeout(() => { if (!document.getElementById('view-analysis').classList.contains('hidden')) anOpen(id); else anOpen._pending = id; }, 4000);
    return;
  }
  if (d.status === 'error') {
    body.innerHTML = `<div class="p-3 rounded-xl bg-rose-50 text-rose-700 text-xs font-bold" dir="auto">❌ فشل التحليل: ${escT(d.error)}</div>`;
    anLoadReports();
    return;
  }
  anOpen._text = d.report || '';
  body.innerHTML = mdToHtml(d.report);
  anLoadReports();
}

function anCopy() {
  navigator.clipboard.writeText(anOpen._text || '').then(() => alert('✅ تم نسخ التقرير'));
}

async function anLoadReports() {
  const box = document.getElementById('an-reports');
  if (!box) return;
  const r = await fetch('/api/analysis/reports');
  const d = await r.json();
  const rows = d.reports || [];
  if (!rows.length) { box.innerHTML = '<div class="text-slate-400 font-semibold">لا توجد تقارير بعد.</div>'; return; }
  const badge = s => s === 'done' ? '<span class="px-2 py-0.5 rounded-lg bg-emerald-50 text-emerald-700 font-bold">جاهز</span>'
    : s === 'running' ? '<span class="px-2 py-0.5 rounded-lg bg-blue-50 text-blue-700 font-bold">⏳ جارٍ</span>'
    : '<span class="px-2 py-0.5 rounded-lg bg-rose-50 text-rose-700 font-bold">فشل</span>';
  box.innerHTML = rows.map(x => `<div class="flex items-center gap-2 flex-wrap border border-[#e3ecf8] rounded-xl px-3 py-2 hover:bg-[#f8fbff]">
      <button onclick="anOpen(${x.id})" class="font-mono font-bold text-blue-700 hover:underline" dir="ltr">${escT(x.store_domain)}</button>
      ${badge(x.status)}
      <span class="text-slate-500 font-semibold num">${escT((x.created_at || '').slice(0, 16).replace('T', ' '))}</span>
      <span class="text-slate-500 font-semibold">${anFmt(x.ads_count)} إعلان · ${anFmt(x.unique_texts)} نص</span>
      <span class="text-slate-400 font-mono truncate max-w-[220px]" dir="ltr">${escT(x.model || '')}</span>
      ${x.note ? `<span class="text-slate-500 truncate max-w-[260px]" dir="auto">📝 ${escT(x.note)}</span>` : ''}
      <span class="ms-auto flex gap-1.5">
        <button data-d="${escT(x.store_domain)}" onclick="document.getElementById('an-store').value=this.dataset.d; anPreview()" title="تحليل جديد" class="px-2 py-1 rounded-lg border border-[#b7c9e2] font-bold hover:bg-white">🔄</button>
        ${x.status !== 'running' ? `<button onclick="anDelete(${x.id})" title="حذف" class="px-2 py-1 rounded-lg border border-rose-200 text-rose-700 font-bold hover:bg-rose-50">🗑️</button>` : ''}
      </span>
    </div>`).join('');
}

async function anDelete(id) {
  if (!confirm('حذف هذا التقرير؟')) return;
  await fetch('/api/analysis/reports/' + id, {method: 'DELETE'});
  anLoadReports();
}

function loadAnalysis() {
  anLoadReports();
  if (anOpen._pending) { const id = anOpen._pending; anOpen._pending = null; anOpen(id); }
}

// ── Settings → AI ──
let aiProviders = {};

async function aiLoadConfig() {
  const r = await fetch('/api/ai/config');
  if (!r.ok) return;
  const c = await r.json();
  aiProviders = c.providers || {};
  document.getElementById('ai-provider').value = c.provider;
  document.getElementById('ai-base-url').value = c.base_url || '';
  document.getElementById('ai-base-url').placeholder = (aiProviders[c.provider] || {}).base_url || 'https://.../v1';
  document.getElementById('ai-model').value = c.model || '';
  document.getElementById('ai-api-key').value = '';
  document.getElementById('ai-max-out').value = c.max_output_tokens;
  document.getElementById('ai-max-texts').value = c.max_texts;
  document.getElementById('ai-temperature').value = c.temperature;
  document.getElementById('ai-daily-limit').value = c.daily_limit;
  const ks = document.getElementById('ai-key-status');
  ks.innerHTML = c.key_unreadable ? '<span class="text-rose-600 font-bold">⚠️ المفتاح المحفوظ لا يمكن فك تشفيره (تغيّر SECRET_KEY) — أدخله من جديد.</span>'
    : c.key_set ? `✅ مفتاح محفوظ (مشفّر): <span class="font-mono" dir="ltr">${escT(c.key_hint)}</span> — اتركه فارغاً للإبقاء عليه.`
    : 'لا يوجد مفتاح محفوظ.';
}

function aiProviderChanged() {
  const p = document.getElementById('ai-provider').value;
  document.getElementById('ai-base-url').placeholder = (aiProviders[p] || {}).base_url || 'https://.../v1';
  if (p !== 'custom') document.getElementById('ai-base-url').value = '';
}

function aiForm() {
  return {
    provider: document.getElementById('ai-provider').value,
    base_url: document.getElementById('ai-base-url').value.trim(),
    api_key: document.getElementById('ai-api-key').value.trim(),
    model: document.getElementById('ai-model').value.trim(),
    max_output_tokens: document.getElementById('ai-max-out').value,
    max_texts: document.getElementById('ai-max-texts').value,
    temperature: document.getElementById('ai-temperature').value,
    daily_limit: document.getElementById('ai-daily-limit').value,
  };
}

function aiResult(ok, html) {
  const el = document.getElementById('ai-test-result');
  el.classList.remove('hidden');
  el.className = 'p-3 rounded-xl text-xs leading-relaxed font-bold ' + (ok ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700');
  el.innerHTML = html;
}

async function aiSave() {
  const r = await fetch('/api/ai/config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(aiForm())});
  const d = await r.json();
  if (d.ok) { aiResult(true, '✅ تم حفظ إعدادات الذكاء الاصطناعي.'); aiLoadConfig(); }
  else aiResult(false, '❌ ' + escT(d.error || 'فشل الحفظ'));
}

async function aiClearKey() {
  if (!confirm('حذف مفتاح API المحفوظ؟')) return;
  await fetch('/api/ai/config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({clear_key: true})});
  aiLoadConfig();
}

async function aiTest(btn) {
  btn.disabled = true;
  aiResult(true, '⏳ جاري الاختبار...');
  try {
    const r = await fetch('/api/ai/test', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(aiForm())});
    const d = await r.json();
    if (d.ok) aiResult(true, `✅ الاتصال يعمل — الموديل <span class="font-mono" dir="ltr">${escT(d.model)}</span> رد: "${escT(d.reply)}"`);
    else aiResult(false, '❌ ' + escT(d.error));
  } finally { btn.disabled = false; }
}

async function aiLoadModels(btn) {
  btn.disabled = true;
  try {
    const r = await fetch('/api/ai/models', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(aiForm())});
    const d = await r.json();
    if (!r.ok) { aiResult(false, '❌ ' + escT(d.error)); return; }
    const list = document.getElementById('ai-model-list');
    list.innerHTML = d.models.map(m => `<option value="${escT(m.id)}">${escT(m.name)}${m.price_in != null ? ` — $${m.price_in}/$${m.price_out} per 1M` : ''}${m.context ? ` · ${m.context} ctx` : ''}</option>`).join('');
    aiResult(true, `✅ ${d.models.length} موديل متاح — اكتب في خانة الموديل لاختيار واحد.`);
  } finally { btn.disabled = false; }
}
