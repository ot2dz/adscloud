// ── User categories: nested folders for competitors (kind "target") and
// winning products (kind "winning"). One item can be in several categories.
let catData = { categories: [], items: { target: {}, winning: {} } };
const catFilter = { target: null, winning: null };  // selected category id per tab
const CAT_COLORS = ['#2563eb', '#059669', '#d97706', '#dc2626', '#7c3aed', '#0891b2', '#db2777', '#475569'];

async function catLoad() {
  try {
    const r = await fetch('/api/categories');
    if (r.ok) catData = await r.json();
  } catch (_e) { /* keep the last state */ }
  ['target', 'winning'].forEach(catRenderBar);
}

function catById(id) { return catData.categories.find(c => Number(c.id) === Number(id)); }
function catChildren(id) { return catData.categories.filter(c => (c.parent_id || null) === (id || null)); }
function catPath(id) {
  const out = []; let c = catById(id); const seen = new Set();
  while (c && !seen.has(c.id)) { seen.add(c.id); out.unshift(c.name); c = catById(c.parent_id); }
  return out.join(' › ');
}
function catDescendants(id) {
  const out = [Number(id)]; const stack = [Number(id)];
  while (stack.length) { const c = stack.pop(); catChildren(c).forEach(k => { out.push(Number(k.id)); stack.push(Number(k.id)); }); }
  return out;
}
function catOf(kind, itemId) { return (catData.items[kind] || {})[String(itemId)] || []; }
function catTotal(kind, id) {
  // items in this category or any sub-category
  const ids = new Set(catDescendants(id));
  return Object.values(catData.items[kind] || {}).filter(cs => cs.some(c => ids.has(Number(c)))).length;
}

// Does this item pass the tab's category filter?
function catMatches(kind, itemId) {
  const sel = catFilter[kind];
  if (!sel) return true;
  if (sel === 'none') return !catOf(kind, itemId).length;
  const ids = new Set(catDescendants(sel));
  return catOf(kind, itemId).some(c => ids.has(Number(c)));
}

function catChip(c, kind, active) {
  const n = catTotal(kind, c.id);
  return `<button type="button" onclick="catSelect('${kind}', ${Number(c.id)})"
    class="px-2.5 py-1 rounded-full border text-[11.5px] font-bold transition ${active ? 'text-white' : 'bg-white hover:bg-slate-50 text-slate-700'}"
    style="border-color:${escT(c.color || '#94a3b8')};${active ? 'background:' + escT(c.color || '#475569') : ''}">
    ${catChildren(c.id).length ? '📁' : '🏷️'} ${escT(c.name)} <span class="num opacity-70">${n}</span></button>`;
}

// Chips: top level, then the children of the selected branch, level by level.
function catRenderBar(kind) {
  const el = document.getElementById('cat-bar-' + kind);
  if (!el) return;
  const sel = catFilter[kind];
  const chain = [];
  let c = sel && sel !== 'none' ? catById(sel) : null;
  while (c) { chain.unshift(Number(c.id)); c = catById(c.parent_id); }
  const rows = [];
  const top = catChildren(null);
  rows.push(`<div class="flex flex-wrap gap-1.5 items-center">
      <button type="button" onclick="catSelect('${kind}', null)" class="px-2.5 py-1 rounded-full border text-[11.5px] font-bold ${!sel ? 'bg-[#174ea6] text-white border-[#174ea6]' : 'bg-white text-slate-700 border-slate-300'}">الكل</button>
      ${top.map(t => catChip(t, kind, chain.includes(Number(t.id)))).join('')}
      <button type="button" onclick="catSelect('${kind}', 'none')" class="px-2.5 py-1 rounded-full border text-[11.5px] font-bold ${sel === 'none' ? 'bg-slate-700 text-white border-slate-700' : 'bg-white text-slate-500 border-dashed border-slate-300'}">بدون تصنيف</button>
      <button type="button" onclick="catManage()" class="ms-auto px-2.5 py-1 rounded-lg border border-[#b7c9e2] text-[11.5px] font-bold text-[#174ea6] bg-white hover:bg-[#eef6ff]">⚙️ إدارة التصنيفات</button>
    </div>`);
  chain.forEach(id => {
    const kids = catChildren(id);
    if (kids.length) {
      rows.push(`<div class="flex flex-wrap gap-1.5 items-center ps-4 border-s-2 border-slate-200">
        <span class="text-[11px] text-slate-400 font-bold">${escT(catById(id).name)} ›</span>
        ${kids.map(k => catChip(k, kind, chain.includes(Number(k.id)))).join('')}</div>`);
    }
  });
  el.innerHTML = `<div class="space-y-1.5">${rows.join('')}</div>`;
}

function catSelect(kind, id) {
  catFilter[kind] = (catFilter[kind] === id && id !== null) ? (id === 'none' ? null : (catById(id)?.parent_id || null)) : id;
  catRenderBar(kind);
  if (kind === 'target' && typeof renderTargets === 'function') renderTargets();
  if (kind === 'winning' && typeof renderWinning === 'function') renderWinning();
}

// Small colored tags shown on a row / card.
function catTagsHtml(kind, itemId) {
  return catOf(kind, itemId).map(id => {
    const c = catById(id);
    return c ? `<span class="text-[10px] px-2 py-0.5 rounded-full font-bold text-white" style="background:${escT(c.color || '#64748b')}" title="${escT(catPath(id))}">${escT(c.name)}</span>` : '';
  }).join(' ');
}

function catButtonHtml(kind, itemId, cls) {
  return `<button type="button" onclick="catPick('${kind}', ${Number(itemId)})" title="التصنيفات"
    class="${cls || 'text-xs px-2.5 py-1 rounded-lg bg-white hover:bg-[#eef6ff] text-slate-700 border border-[#cfe0f5] font-bold transition shadow-sm'}">🏷️ تصنيف</button>`;
}

// ── modals ──
function catModal(title, inner) {
  document.getElementById('cat-modal')?.remove();
  const w = document.createElement('div');
  w.id = 'cat-modal';
  w.className = 'fixed inset-0 z-[70] bg-slate-900/40 flex items-center justify-center p-4';
  w.onclick = e => { if (e.target === w) w.remove(); };
  w.innerHTML = `<div class="bg-white rounded-2xl shadow-xl w-full max-w-md p-5 space-y-3 text-sm max-h-[85vh] overflow-auto" dir="rtl">
      <div class="flex items-center justify-between"><h3 class="font-extrabold text-[#102a56]">${title}</h3>
        <button onclick="document.getElementById('cat-modal').remove()" class="text-slate-400 hover:text-slate-700 font-bold">✕</button></div>
      ${inner}</div>`;
  document.body.appendChild(w);
  return w;
}

function catTreeRows(render, parent = null, depth = 0) {
  return catChildren(parent).map(c => render(c, depth) + catTreeRows(render, c.id, depth + 1)).join('');
}

function catPick(kind, itemId) {
  if (!catData.categories.length) { catManage(); return; }
  const mine = new Set(catOf(kind, itemId).map(Number));
  const rows = catTreeRows((c, d) => `<label class="flex items-center gap-2 py-1 cursor-pointer" style="padding-inline-start:${d * 18}px">
      <input type="checkbox" value="${Number(c.id)}" ${mine.has(Number(c.id)) ? 'checked' : ''} class="w-4 h-4">
      <span class="w-2.5 h-2.5 rounded-full" style="background:${escT(c.color || '#94a3b8')}"></span>
      <span class="font-semibold text-slate-700">${escT(c.name)}</span></label>`);
  const w = catModal('🏷️ اختر التصنيفات', `<div class="text-[11px] text-slate-500 font-semibold">يمكن وضعه في أكثر من تصنيف.</div>
      <div id="cat-pick-list" class="border border-[#e3ecf8] rounded-xl p-2">${rows}</div>
      <div class="flex gap-2 justify-between">
        <button onclick="catManage()" class="px-3 py-2 rounded-xl border border-[#b7c9e2] font-bold text-[#174ea6] text-xs">⚙️ تصنيف جديد</button>
        <button id="cat-pick-save" class="px-4 py-2 rounded-xl bg-[#174ea6] hover:bg-[#123f87] text-white font-bold text-xs">حفظ</button>
      </div>`);
  w.querySelector('#cat-pick-save').onclick = async () => {
    const ids = [...w.querySelectorAll('#cat-pick-list input:checked')].map(i => Number(i.value));
    const r = await fetch('/api/categories/assign', {method: 'PUT', headers: {'Content-Type': 'application/json'},
                                                     body: JSON.stringify({kind, item_id: itemId, category_ids: ids})});
    const d = await r.json();
    if (!r.ok) { alert(d.error || 'فشل الحفظ'); return; }
    w.remove();
    await catLoad();
    if (kind === 'target' && typeof renderTargets === 'function') renderTargets();
    if (kind === 'winning' && typeof renderWinning === 'function') renderWinning();
  };
}

function catManage() {
  const opts = (sel) => '<option value="">— مستوى رئيسي —</option>' + catTreeRows((c, d) =>
    `<option value="${Number(c.id)}" ${Number(sel) === Number(c.id) ? 'selected' : ''}>${'— '.repeat(d)}${escT(c.name)}</option>`);
  const colors = CAT_COLORS.map((c, i) => `<label class="cursor-pointer"><input type="radio" name="cat-color" value="${c}" ${i === 0 ? 'checked' : ''} class="peer hidden">
      <span class="block w-6 h-6 rounded-full border-2 border-white ring-1 ring-slate-300 peer-checked:ring-2 peer-checked:ring-slate-800" style="background:${c}"></span></label>`).join('');
  const list = catTreeRows((c, d) => `<div class="flex items-center gap-2 py-1.5 border-b border-slate-100" style="padding-inline-start:${d * 18}px">
      <span class="w-2.5 h-2.5 rounded-full shrink-0" style="background:${escT(c.color || '#94a3b8')}"></span>
      <span class="font-semibold text-slate-700 flex-1 truncate">${escT(c.name)}</span>
      <button onclick="catRename(${Number(c.id)})" class="text-[11px] text-[#174ea6] font-bold">تعديل</button>
      <button onclick="catDelete(${Number(c.id)})" class="text-[11px] text-rose-600 font-bold">حذف</button></div>`)
    || '<div class="text-[12px] text-slate-400 py-2">لا تصنيفات بعد.</div>';
  catModal('⚙️ إدارة التصنيفات', `
    <div class="space-y-2 rounded-xl border border-[#d7e3f4] p-3">
      <input id="cat-new-name" maxlength="60" placeholder="اسم التصنيف (مثلاً: مطبخ، منافسون خطيرون…)" class="w-full border border-[#b7c9e2] rounded-lg px-3 py-2 text-sm">
      <select id="cat-new-parent" class="w-full border border-[#b7c9e2] rounded-lg px-3 py-2 text-sm">${opts(null)}</select>
      <div class="flex items-center gap-2">${colors}</div>
      <button onclick="catCreate()" class="w-full py-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-bold text-xs">➕ إضافة تصنيف</button>
    </div>
    <div>${list}</div>
    <div class="text-[11px] text-slate-500">حذف تصنيف يحذف فروعه أيضاً، لكن المنافسين والمنتجات لا تُحذف.</div>`);
}

async function catCreate() {
  const name = document.getElementById('cat-new-name').value.trim();
  if (!name) return;
  const parent = document.getElementById('cat-new-parent').value || null;
  const color = (document.querySelector('input[name="cat-color"]:checked') || {}).value || '';
  const r = await fetch('/api/categories', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                            body: JSON.stringify({name, parent_id: parent ? Number(parent) : null, color})});
  const d = await r.json();
  if (!r.ok) { alert(d.error || 'فشل'); return; }
  await catLoad();
  catManage();
}

async function catRename(id) {
  const c = catById(id);
  const name = prompt('الاسم الجديد:', c ? c.name : '');
  if (!name) return;
  const r = await fetch('/api/categories/' + id, {method: 'PATCH', headers: {'Content-Type': 'application/json'},
                                                  body: JSON.stringify({name})});
  if (!r.ok) { alert((await r.json()).error || 'فشل'); return; }
  await catLoad(); catManage();
}

async function catDelete(id) {
  const n = catDescendants(id).length;
  if (!confirm(n > 1 ? `حذف التصنيف و${n - 1} فرع داخله؟` : 'حذف هذا التصنيف؟')) return;
  await fetch('/api/categories/' + id, {method: 'DELETE'});
  if (catDescendants(id).includes(Number(catFilter.target))) catFilter.target = null;
  if (catDescendants(id).includes(Number(catFilter.winning))) catFilter.winning = null;
  await catLoad(); catManage();
  if (typeof renderTargets === 'function') renderTargets();
  if (typeof renderWinning === 'function') renderWinning();
}
