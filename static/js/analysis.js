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
    <div class="flex gap-2.5 flex-wrap items-center">
      <button id="an-run-btn" onclick="anRun(this, 'report')" ${d.ready_error ? 'disabled' : ''}
        class="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed rounded-xl px-6 py-2.5 font-bold text-white transition text-xs shadow-sm">🤖 حلّل الآن</button>
      <button id="an-audit-btn" onclick="anRun(this, 'audit')" ${d.ready_error ? 'disabled' : ''}
        class="bg-violet-600 hover:bg-violet-700 disabled:opacity-50 disabled:cursor-not-allowed rounded-xl px-6 py-2.5 font-bold text-white transition text-xs shadow-sm">🧪 تدقيق المنتجات + القرار والتسعير</button>
    </div>
    ${anProductsHtml(d.products || [])}
    ${anGeminiHtml()}
    <div class="text-[11px] text-slate-500 font-semibold leading-relaxed">🧪 التدقيق: يستخرج حتى <b>${anFmt(d.audit_max_products)}</b> منتجات، يبحث عن كل منتج ${d.web_search ? '<b>في الويب</b> (Reddit / Amazon / AliExpress)' : '<span class="text-amber-700">بدون بحث ويب (يتطلب OpenRouter)</span>'}، ثم يحسب الأسعار الثلاثة والنقاط ببايثون ويعطي جدول قرارات. استدعاء واحد للموديل لكل منتج — التكلفة أعلى من التقرير العادي.</div>
  </div>`;
}

// ── Products of the store: lifecycle (market proof) + competitor price ──
function anProductsHtml(products) {
  if (!products.length) return '';
  const tone = s => ({relaunched: 'bg-emerald-100 text-emerald-800', scaling: 'bg-emerald-100 text-emerald-800',
    running_winner: 'bg-emerald-50 text-emerald-700', finished_winner: 'bg-sky-50 text-sky-800',
    test_success_waiting: 'bg-violet-50 text-violet-700', running: 'bg-slate-50 text-slate-700',
    finished_small: 'bg-sky-50 text-sky-700', failed_test: 'bg-rose-50 text-rose-700'}[s] || 'bg-amber-50 text-amber-800');
  const rows = products.map(p => `<tr class="border-b border-slate-100 last:border-0" data-path="${escT(p.path)}">
      <td class="py-1.5 pe-2"><span class="px-2 py-0.5 rounded-lg font-bold whitespace-nowrap ${tone(p.stage)}" title="${escT(p.meaning || '')}">${escT(p.label || '—')}</span></td>
      <td class="py-1.5 pe-2 max-w-[260px] truncate" dir="ltr">${p.url ? `<a href="${escT(p.url)}" target="_blank" rel="noopener nofollow" class="font-mono text-blue-700 hover:underline">${escT(p.path)}</a>` : `<span class="font-mono">${escT(p.path)}</span>`}</td>
      <td class="py-1.5 pe-2 num text-slate-500 whitespace-nowrap">${anFmt(p.ads)} إعلان · ${anFmt(p.active)} نشط · ${anFmt(p.max_days)} يوم</td>
      <td class="py-1.5"><input type="number" min="100" step="100" placeholder="السعر دج" value="${p.price || ''}"
        data-src="${escT(p.price_source || '')}" oninput="this.dataset.src='manual'"
        class="an-price w-28 bg-white border border-[#b7c9e2] rounded-lg px-2 py-1 text-xs num"></td>
      <td class="py-1.5 ps-2 an-price-status text-[10px] font-bold text-slate-400 whitespace-nowrap"></td>
    </tr>`).join('');
  return `<div class="rounded-xl border border-[#d7e3f4] p-3 space-y-2">
    <div class="flex items-center gap-2 flex-wrap">
      <div class="font-extrabold text-[#102a56]">📦 منتجات المتجر: حالة السوق وسعر المنافس</div>
      <button type="button" onclick="anFetchPrices(this)" class="ms-auto bg-white hover:bg-[#eef6ff] border border-[#9fbbe0] text-[#174ea6] rounded-lg px-3 py-1.5 font-bold text-xs">🏷️ جلب الأسعار من صفحات المنتجات</button>
    </div>
    <div class="text-[11px] text-slate-500 font-semibold leading-relaxed">حالة السوق محسوبة من تواريخ الإعلانات: الإعلان المتوقف بعد مدة طويلة = رابح انتهى مخزونه، وتست قوي توقف مؤخراً = غالباً ينتظر الاستيراد. السعر يُستعمل في التدقيق وفي بيانات Gemini. ${anExt ? `<span class="text-emerald-700">🧩 إضافة Chrome متصلة (v${escT(anExt.version || '')}): الأسعار تُجلب من متصفحك.</span>` : 'بدون إضافة Chrome تُجلب من الخادم، وبعض المتاجر تحجبه — ثبّت الإضافة أو اكتب السعر بيدك.'}</div>
    <div class="overflow-x-auto"><table class="w-full text-xs"><tbody id="an-products">${rows}</tbody></table></div>
  </div>`;
}

// The AdsCloud Chrome extension (adlibrary/) announces itself on this page and
// can read prices from the user's own browser, where stores that refuse the
// server's IP open normally. Without it, the server fetches.
let anExt = null;  // {version} once the extension answered
const anExtWaiters = {};
window.addEventListener('message', ev => {
  if (ev.source !== window || ev.origin !== window.location.origin) return;
  const m = ev.data;
  if (!m || m.source !== 'adscloud-ext') return;
  if (m.type === 'ready' && (m.features || []).includes('prices')) anExt = {version: m.version};
  if (m.type === 'prices' && anExtWaiters[m.id]) { anExtWaiters[m.id](m); delete anExtWaiters[m.id]; }
});
window.postMessage({source: 'adscloud-page', type: 'ping'}, window.location.origin);

function anExtFetchPrices(urls) {
  return new Promise((resolve, reject) => {
    const id = 'p' + Date.now() + Math.random().toString(36).slice(2, 7);
    const timer = setTimeout(() => { delete anExtWaiters[id]; reject(new Error('الإضافة لم ترد')); },
                             30000 + urls.length * 17000);
    anExtWaiters[id] = m => { clearTimeout(timer); m.ok ? resolve(m.results || []) : reject(new Error(m.error || 'فشل')); };
    window.postMessage({source: 'adscloud-page', type: 'fetch-prices', id, urls}, window.location.origin);
  });
}

async function anServerFetchPrices(trs) {
  const r = await fetch('/api/analysis/prices', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({store: anStoreValue(), paths: trs.map(tr => tr.dataset.path)})
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'فشل الجلب');
  return d.prices || [];
}

function anCollectPrices() {
  const out = {};
  document.querySelectorAll('#an-products tr[data-path]').forEach(tr => {
    const inp = tr.querySelector('.an-price');
    const v = parseInt((inp && inp.value) || '', 10);
    if (v >= 100) out[tr.dataset.path] = {price: v, source: inp.dataset.src === 'page' ? 'page' : 'manual'};
  });
  return out;
}

async function anFetchPrices(btn) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '⏳ جاري الجلب...';
  const trs = [...document.querySelectorAll('#an-products tr[data-path]')];
  trs.forEach(tr => { tr.querySelector('.an-price-status').textContent = '⏳'; });
  const viaExt = !!anExt;
  try {
    let list;
    if (viaExt) {
      btn.textContent = '⏳ متصفحك يجلب الأسعار...';
      const withUrl = trs.map(tr => ({path: tr.dataset.path, a: tr.querySelector('a[href]')})).filter(x => x.a);
      const res = await anExtFetchPrices(withUrl.map(x => x.a.href));
      const byUrl = Object.fromEntries(res.map(x => [x.url, x]));
      list = withUrl.map(x => Object.assign({}, byUrl[x.a.href] || {status: 'error'}, {path: x.path}));
    } else {
      list = await anServerFetchPrices(trs);
    }
    const byPath = Object.fromEntries(list.map(x => [x.path, x]));
    let ok = 0, blocked = 0;
    trs.forEach(tr => {
      const x = byPath[tr.dataset.path];
      const st = tr.querySelector('.an-price-status');
      const inp = tr.querySelector('.an-price');
      if (!x) { st.textContent = 'بلا رابط'; return; }
      if (x.status === 'ok' && x.price) {
        if (!inp.value || inp.dataset.src !== 'manual') { inp.value = x.price; inp.dataset.src = 'page'; }
        st.textContent = viaExt ? '✅ من متصفحك' : '✅ من الصفحة'; st.className = 'py-1.5 ps-2 an-price-status text-[10px] font-bold text-emerald-700 whitespace-nowrap'; ok++;
      } else if (x.status === 'blocked') {
        st.textContent = viaExt ? '🔒 محجوب حتى من متصفحك — اكتبه يدوياً'
          : '🔒 محجوب عن الخادم — ثبّت إضافة Chrome أو اكتبه يدوياً'; st.title = x.detail || ''; st.className = 'py-1.5 ps-2 an-price-status text-[10px] font-bold text-amber-700 whitespace-nowrap'; blocked++;
      } else {
        st.textContent = x.status === 'not_found' ? 'لم يُعثر على سعر' : 'خطأ'; st.title = x.detail || '';
      }
    });
    btn.textContent = `✅ ${ok} سعر` + (blocked ? ` · ${blocked} محجوب` : '') + (viaExt ? ' (من متصفحك)' : '');
    setTimeout(() => { btn.textContent = label; }, 4000);
  } catch (e) {
    alert('خطأ: ' + e.message); btn.textContent = label;
  } finally { btn.disabled = false; }
}

// ── Free route: Google AI Studio (Gemini) — copy, paste there, paste the answer back ──
let anGem = null;  // {store, system, data, filename, ads, unique_texts, tokens_est}

function anGeminiHtml() {
  const btn = 'bg-white hover:bg-sky-100 border border-sky-300 text-sky-800 rounded-xl px-3 py-2 font-bold text-xs transition';
  return `<div class="rounded-xl border border-sky-200 bg-sky-50/60 p-3 space-y-2.5">
    <div class="font-extrabold text-sky-900">✨ مجاناً عبر Google AI Studio (Gemini)</div>
    <ol class="list-decimal ps-5 space-y-0.5 text-slate-700 font-semibold leading-relaxed">
      <li>افتح AI Studio وأنشئ محادثة جديدة.</li>
      <li>انسخ <b>تعليمات النظام</b> والصقها في خانة <span dir="ltr">System instructions</span> (مرة واحدة، تبقى محفوظة).</li>
      <li>فعّل من الأدوات: <span dir="ltr">Grounding with Google Search</span> و <span dir="ltr">Code execution</span> (و <span dir="ltr">URL context</span> إن وُجد).</li>
      <li>انسخ <b>بيانات المتجر</b> والصقها كرسالة (أو حمّلها كملف وارفعه) ثم أرسل.</li>
      <li>الصق جواب Gemini في الخانة أسفله واحفظه ليبقى في تقاريرك.</li>
    </ol>
    <div class="flex flex-wrap gap-2">
      <a href="https://aistudio.google.com/prompts/new_chat" target="_blank" rel="noopener" class="${btn}">🌐 فتح AI Studio</a>
      <button type="button" onclick="anGemCopy('system', this)" class="${btn}">📋 نسخ تعليمات النظام</button>
      <button type="button" onclick="anGemCopy('data', this)" class="${btn}">📋 نسخ بيانات المتجر</button>
      <button type="button" onclick="anGemDownload(this)" class="${btn}">⬇️ تحميل البيانات (.txt)</button>
    </div>
    <div id="an-gem-info" class="text-[11px] text-slate-500 font-semibold"></div>
    <textarea id="an-gem-result" rows="3" dir="auto" placeholder="الصق هنا جواب Gemini كاملاً..."
      class="w-full bg-white border border-sky-200 rounded-xl px-3 py-2 text-slate-800 placeholder-slate-400 text-sm focus:outline-none focus:ring-1 focus:ring-sky-500"></textarea>
    <button type="button" onclick="anGemSave(this)" class="bg-sky-700 hover:bg-sky-800 text-white rounded-xl px-4 py-2 font-bold text-xs">💾 حفظ النتيجة في التقارير</button>
  </div>`;
}

async function anGemLoad() {
  const store = anStoreValue();
  const note = document.getElementById('an-note').value;
  const prices = anCollectPrices();
  const pkey = JSON.stringify(prices);
  if (anGem && anGem.store_input === store && anGem.note === note && anGem.pkey === pkey) return anGem;
  const r = await fetch('/api/analysis/export', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({store, note, prices})
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'فشل تجهيز البيانات');
  anGem = Object.assign(d, {store_input: store, note, pkey});
  const info = document.getElementById('an-gem-info');
  if (info) info.innerHTML = `${anFmt(d.ads)} إعلان · ${anFmt(d.unique_texts)} نص فريد (كلها، بلا حد) · ≈ ${anFmt(d.tokens_est)} توكن`;
  return anGem;
}

async function anGemCopy(which, btn) {
  const label = btn.textContent;
  try {
    const d = await anGemLoad();
    await navigator.clipboard.writeText(which === 'system' ? d.system : d.data);
    btn.textContent = '✅ تم النسخ';
  } catch (e) {
    alert('خطأ: ' + e.message);
  }
  setTimeout(() => { btn.textContent = label; }, 2000);
}

async function anGemDownload(btn) {
  try {
    const d = await anGemLoad();
    const blob = new Blob([d.data], {type: 'text/plain;charset=utf-8'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = d.filename || 'store.txt';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) {
    alert('خطأ: ' + e.message);
  }
}

async function anGemSave(btn) {
  const text = document.getElementById('an-gem-result').value.trim();
  if (text.length < 20) { alert('الصق جواب Gemini أولاً.'); return; }
  btn.disabled = true;
  try {
    const r = await fetch('/api/analysis/import', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({store: anStoreValue(), report: text, note: document.getElementById('an-note').value,
                            ads: anGem ? anGem.ads : 0, unique_texts: anGem ? anGem.unique_texts : 0})
    });
    const d = await r.json();
    if (!r.ok || !d.id) { alert(d.error || 'فشل الحفظ'); return; }
    document.getElementById('an-gem-result').value = '';
    anOpen(d.id);
    anLoadReports();
  } finally { btn.disabled = false; }
}

async function anRun(btn, kind) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '⏳ جاري الإرسال...';
  try {
    const r = await fetch('/api/analysis/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({store: anStoreValue(), lang: document.getElementById('an-lang').value,
                            note: document.getElementById('an-note').value, kind: kind || 'report',
                            prices: anCollectPrices()})
    });
    const d = await r.json();
    if (!r.ok || !d.id) { alert(d.error || 'فشل التحليل'); btn.disabled = false; btn.textContent = label; return; }
    btn.textContent = '✅ بدأ التحليل';
    anOpen(d.id);
    anLoadReports();
  } catch (e) {
    alert('خطأ: ' + e.message); btn.disabled = false; btn.textContent = label;
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
      <div class="font-extrabold text-[#102a56]">${d.kind === 'audit' ? '🧪 تدقيق المنتجات' : 'تقرير'}: <span class="font-mono" dir="ltr">${escT(d.store_domain)}</span></div>
      <div class="text-[11px] text-slate-500 font-semibold mt-0.5">${escT(when)} UTC · ${anFmt(d.ads_count)} إعلان · ${anFmt(d.unique_texts)} نص فريد · <span class="font-mono" dir="ltr">${escT(d.model || '')}</span>${usage}</div>
      ${d.note ? `<div class="text-[11px] text-slate-600 mt-1" dir="auto">📝 ${escT(d.note)}</div>` : ''}
    </div>
    <div class="flex gap-2">
      ${d.status === 'done' ? `<button onclick="anCopy()" class="text-xs px-3 py-1.5 rounded-lg border border-[#b7c9e2] font-bold text-slate-700 hover:bg-slate-50">📋 نسخ</button>` : ''}
      <button onclick="document.getElementById('an-report-wrap').classList.add('hidden')" class="text-xs px-3 py-1.5 rounded-lg border border-[#b7c9e2] font-bold text-slate-700 hover:bg-slate-50">✕</button>
    </div>`;
  if (d.status === 'running') {
    const wait = d.kind === 'audit'
      ? 'جاري تدقيق المنتجات (بحث + حسابات) — عدة دقائق حسب عدد المنتجات. يظهر الجدول تدريجياً، ويمكنك مغادرة الصفحة.'
      : 'الذكاء الاصطناعي يحلل الإعلانات... قد يستغرق دقيقة أو دقيقتين. يمكنك مغادرة الصفحة، التقرير يُحفظ تلقائياً.';
    body.innerHTML = `<div class="flex items-center gap-2 text-slate-500 font-bold"><span class="an-spin"></span> ${wait}</div>`
      + (d.report ? `<div class="mt-4">${mdToHtml(d.report)}</div>` : '');
    anPollTimer = setTimeout(() => { if (!document.getElementById('view-analysis').classList.contains('hidden')) anOpen(id); else anOpen._pending = id; }, 4000);
    return;
  }
  if (d.status === 'error') {
    body.innerHTML = `<div class="p-3 rounded-xl bg-rose-50 text-rose-700 text-xs font-bold" dir="auto">❌ فشل التحليل: ${escT(d.error)}</div>`
      + (d.report ? `<div class="mt-4">${mdToHtml(d.report)}</div>` : '');
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
      ${x.provider === 'manual' ? '<span class="px-2 py-0.5 rounded-lg bg-sky-50 text-sky-700 font-bold">✨ Gemini</span>'
        : x.kind === 'audit' ? '<span class="px-2 py-0.5 rounded-lg bg-violet-50 text-violet-700 font-bold">🧪 تدقيق</span>' : ''}
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
  document.getElementById('ai-web-search').value = c.web_search ? '1' : '0';
  document.getElementById('ai-audit-max').value = c.audit_max_products;
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
    web_search: document.getElementById('ai-web-search').value === '1',
    audit_max_products: document.getElementById('ai-audit-max').value,
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
