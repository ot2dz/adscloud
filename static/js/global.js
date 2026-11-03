// Foreign market side tab: manual store scans + product analysis.
async function loadGlobal() {
  await loadGlobalTargets();
  await loadGlobalStores();
  await loadGlobalProducts();
}

async function loadGlobalTargets() {
  const r = await fetch('/api/global/targets');
  const d = await r.json();
  const rows = d.targets || [];
  const el = document.getElementById('global-targets-list');
  const counter = document.getElementById('global-targets-count');
  if (counter) counter.innerText = rows.length ? `(${rows.length})` : '';
  if (!rows.length) {
    el.innerHTML = '<tr><td colspan="4" class="py-8 text-center text-slate-500 font-bold">لا توجد متاجر خارجية بعد. أضف دوميناً في الأعلى.</td></tr>';
    return;
  }
  el.innerHTML = rows.map(t => `
    <tr>
      <td class="py-3 px-5">
        <div class="font-bold text-[#102a56] text-sm">${escT(t.label || t.target_value)}</div>
        <div class="text-[11px] text-slate-500 font-mono">${escT(t.target_value)}</div>
      </td>
      <td class="px-5 text-xs font-bold num">${t.ads_count || 0}</td>
      <td class="px-5 text-xs text-slate-500 font-mono">${t.last_scraped_at ? escT(String(t.last_scraped_at).slice(0, 16).replace('T', ' ')) : 'لم يُفحص بعد'}</td>
      <td class="px-5 text-center whitespace-nowrap">
        <button data-id="${Number(t.id)}" data-label="${escT(t.label || t.target_value)}" onclick="openGlobalActivityEl(this)" class="text-xs px-2.5 py-1 rounded-lg bg-white hover:bg-[#eef6ff] text-slate-700 border border-[#cfe0f5] font-bold transition">سجل النشاط</button>
        <button onclick="scanGlobalTarget(${Number(t.id)}, this)" class="text-xs px-2.5 py-1 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-bold transition">فحص الآن</button>
        <button onclick="delGlobalTarget(${Number(t.id)})" class="text-rose-600 hover:text-rose-700 text-xs px-2 font-bold" title="حذف">حذف</button>
      </td>
    </tr>`).join('');
}

async function loadGlobalStores() {
  const sel = document.getElementById('global-store-filter');
  if (!sel) return;
  const current = sel.value;
  const r = await fetch('/api/global/stores');
  const d = await r.json();
  const stores = d.stores || [];
  sel.innerHTML = '<option value="">كل المتاجر</option>' + stores.map(s =>
    `<option value="${escT(s.store_domain)}">${escT(s.store_domain)} (${s.total_ads || 0})</option>`).join('');
  if (current && stores.some(s => s.store_domain === current)) sel.value = current;
}

async function loadGlobalProducts() {
  const el = document.getElementById('global-products-list');
  if (!el) return;
  const store = document.getElementById('global-store-filter').value;
  const sort = document.getElementById('global-sort').value;
  const minAds = document.getElementById('global-min-ads').value || 0;
  const exportLink = document.getElementById('global-export');
  if (exportLink) exportLink.href = '/api/global/products.csv' + (store ? '?store=' + encodeURIComponent(store) : '');
  el.innerHTML = '<tr><td colspan="9" class="py-8 text-center text-slate-500 font-bold">… جاري التحميل</td></tr>';
  const qs = new URLSearchParams({ sort, min_ads: minAds, limit: '100' });
  if (store) qs.set('store', store);
  const r = await fetch('/api/global/products?' + qs.toString());
  const d = await r.json();
  const products = d.products || [];
  const counter = document.getElementById('global-products-count');
  if (counter) counter.innerText = `${products.length} من ${d.total || 0} منتج · ${d.total_ads || 0} إعلان`;
  if (!products.length) {
    el.innerHTML = '<tr><td colspan="9" class="py-8 text-center text-slate-500 font-bold">لا توجد منتجات بعد. افحص متجراً أو غيّر الفلتر.</td></tr>';
    return;
  }
  el.innerHTML = products.map(p => {
    const g = p.group || {};
    const h = p.hunter || null;
    const img = p.preview_image_url
      ? `<img src="${escT(p.preview_image_url)}" alt="" loading="lazy" class="w-12 h-12 rounded-lg object-cover bg-slate-100 shrink-0" onerror="this.remove()">`
      : '<div class="w-12 h-12 rounded-lg bg-slate-100 shrink-0"></div>';
    const body = escT(String(p.body || '').slice(0, 120));
    return `
      <tr class="hover:bg-[#f8fbff]">
        <td class="py-3 px-5">
          <div class="flex items-center gap-2.5 max-w-md">
            ${img}
            <div class="min-w-0">
              <div class="text-[12.5px] font-bold text-[#102a56] line-clamp-2">${body || '—'}</div>
              <div class="text-[11px] text-slate-500 truncate">${escT(p.page_name || '')}</div>
            </div>
          </div>
        </td>
        <td class="px-3 text-[11px] font-mono text-slate-600">${escT(p.store_domain || '')}</td>
        <td class="px-3 text-center font-bold num">${g.count || 0}</td>
        <td class="px-3 text-center num text-emerald-700">${g.active || 0}</td>
        <td class="px-3 text-center num">${g.max_days || 0}</td>
        <td class="px-3 text-right text-[11px] font-mono">${escT(String(g.last_launch || '').slice(0, 10))}</td>
        <td class="px-3 text-center num">${g.texts || 0}</td>
        <td class="px-3 text-center">
          <span class="text-[11px] font-bold px-2 py-0.5 rounded-full ${h && h.score >= 70 ? 'bg-emerald-100 text-emerald-800' : (h && h.score >= 40 ? 'bg-amber-100 text-amber-800' : 'bg-slate-100 text-slate-600')}">
            ${h ? h.score : (p.hunter_score || 0)}
          </span>
        </td>
        <td class="px-5 text-center whitespace-nowrap">
          <button onclick="openGroupModal('${escT(g.key || '')}', '${escT((p.page_name || '') + ' — ' + (g.count || 0) + ' إعلان')}', 'global')"
            class="text-xs px-2.5 py-1 rounded-lg bg-[#174ea6] hover:bg-[#123f87] text-white font-bold transition">عرض الإعلانات</button>
          ${p.link_url ? `<a href="${escT(p.link_url)}" target="_blank" rel="noopener" class="text-xs text-blue-700 font-bold px-2">المنتج</a>` : ''}
        </td>
      </tr>`;
  }).join('');
}

async function addGlobalTarget() {
  const val = document.getElementById('global-target-value').value.trim();
  const label = document.getElementById('global-target-label').value.trim();
  if (!val) return alert('اكتب دومين المتجر أولاً.');
  const r = await fetch('/api/global/targets', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value: val, label }),
  });
  const d = await r.json().catch(() => ({}));
  if (!d.ok) return alert(d.error || 'تعذر إضافة المتجر.');
  document.getElementById('global-target-value').value = '';
  document.getElementById('global-target-label').value = '';
  await loadGlobal();
  alert('أُضيف المتجر. اضغط «فحص الآن» لسحب إعلاناته.');
}

async function scanGlobalTarget(id, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/global/targets/' + id + '/scan', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    alert(d.already_queued ? 'الفحص في قائمة الانتظار بالفعل.' : (d.ok ? 'أُضيف الفحص. ستظهر النتائج بعد انتهائه.' : 'تعذر بدء الفحص.'));
    setTimeout(loadGlobal, 15000);
    setTimeout(loadGlobal, 45000);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function scanAllGlobal() {
  const r = await fetch('/api/global/scan-all', { method: 'POST' });
  const d = await r.json().catch(() => ({}));
  alert(d.enqueued ? `أُضيف ${d.enqueued} فحص.` : 'لا توجد متاجر جاهزة للفحص الآن.');
  loadGlobalTargets();
}

async function delGlobalTarget(id) {
  if (!confirm('حذف هذا المتجر من السوق الخارجي؟')) return;
  await fetch('/api/global/targets/' + id, { method: 'DELETE' });
  loadGlobal();
}
