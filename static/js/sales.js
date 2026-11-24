let salesPoll = null;

function salesMsg(text, kind) {
  const el = document.getElementById('sales-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'text-xs font-semibold mt-2 min-h-[1rem] ' +
    (kind === 'error' ? 'text-rose-600' : kind === 'ok' ? 'text-emerald-600' : 'text-slate-500');
}

function salesDays() {
  const el = document.getElementById('sales-days');
  return el ? Number(el.value) || 7 : 7;
}

function salesWhen(iso) {
  return (iso || '').replace('T', ' ').slice(0, 16);
}

async function loadSales() {
  const view = document.getElementById('view-sales');
  if (!view || !currentUser) return;
  const days = salesDays();
  try {
    const r = await fetch('/api/sales/overview?days=' + days, { credentials: 'same-origin' });
    const d = await r.json();
    if (!r.ok || !d.ok) { salesMsg((d && d.error) || 'تعذّر تحميل البيانات', 'error'); return; }

    const stores = d.stores || [];
    const today = stores.reduce((a, s) => a + (s.orders_today || 0), 0);
    const win = stores.reduce((a, s) => a + (s.orders_window || 0), 0);
    document.getElementById('sales-today').textContent = today;
    document.getElementById('sales-window').textContent = win;
    document.getElementById('sales-window-label').textContent = 'آخر ' + days + ' أيام';
    document.getElementById('sales-count').textContent = stores.length;

    const lim = d.limits || {};
    const limitsEl = document.getElementById('sales-limits');
    if (limitsEl) {
      limitsEl.textContent = lim.admin
        ? 'وضع المدير: كل المتاجر'
        : `المتاجر: ${lim.stores_used || 0}/${lim.max_stores || 5} · يُحدَّث تلقائيًا: ${lim.active_used || 0}/${lim.max_active || 1}`;
    }
    const hint = document.getElementById('sales-list-hint');
    if (hint) hint.textContent = d.admin ? 'عدد القياسات = طلبيات أُنشئت فعليًا' : '';

    renderSalesList(stores, days, !!d.admin);
    if (d.admin) renderSalesConfig(d.config || {});

    const running = stores.some(s => s.running);
    if (running && !salesPoll) {
      salesPoll = setInterval(() => {
        const v = document.getElementById('view-sales');
        if (document.hidden || !v || v.classList.contains('hidden')) return;
        loadSales();
      }, 4000);
    } else if (!running && salesPoll) {
      clearInterval(salesPoll); salesPoll = null;
    }
  } catch (e) {
    salesMsg('خطأ في الاتصال', 'error');
  }
}

function salesSpark(daysMap, days) {
  const today = new Date();
  const keys = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today.getTime() - i * 86400000);
    keys.push(d.toISOString().slice(0, 10));
  }
  const vals = keys.map(k => Number((daysMap || {})[k] || 0));
  const max = Math.max(1, ...vals);
  return '<div class="flex items-end gap-1 h-8 mt-2">' +
    vals.map((v, i) => {
      const h = v ? Math.max(10, Math.round(v / max * 100)) : 4;
      const col = v ? '#2563eb' : '#e2e8f0';
      return `<div style="height:${h}%;background:${col}" class="flex-1 rounded-sm" title="${v}"></div>`;
    }).join('') + '</div>';
}

function renderSalesList(stores, days, admin) {
  const box = document.getElementById('sales-list');
  if (!stores.length) {
    box.innerHTML = '<div class="text-center py-8 text-slate-400 font-semibold text-sm">لا توجد متاجر بعد — أضف رابط منتج للبدء.</div>';
    return;
  }
  box.innerHTML = stores.map(s => {
    const when = salesWhen(s.updated_at);
    const status = s.running
      ? '<span class="text-[10px] font-bold text-blue-700 bg-blue-50 border border-blue-200 rounded-full px-2 py-0.5">قيد التحديث…</span>'
      : (s.updated_at
        ? `<span class="text-[11px] text-slate-400 font-semibold">آخر تحديث: <span dir="ltr">${escT(when)}</span></span>`
        : '<span class="text-[11px] text-slate-400 font-semibold">بانتظار أول تحديث…</span>');
    const off = Number(s.enabled || 0) ? '' : ' opacity-60';
    const tech = admin
      ? `<div class="text-[11px] text-slate-500 font-semibold mt-1" dir="ltr">
           last: ${escT(s.last_order_number != null ? '#' + s.last_order_number : '—')}
           · probes: ${s.probes_window || 0}
           · status: ${escT(s.last_probe_status || '—')}
           ${s.last_error_short ? ' · err: ' + escT(s.last_error_short) : ''}
         </div>`
      : '';
    const probeBtn = admin
      ? `<button onclick="probeSalesStore(${s.id}, this)" ${s.running ? 'disabled' : ''} class="sbtn !py-1.5">📡 قِس</button>`
      : '';
    return `
    <div class="border border-[#cfe0f5] rounded-xl p-3 bg-white${off}">
      <div class="flex flex-col md:flex-row md:items-center gap-3">
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 flex-wrap">
            <span dir="ltr" class="font-extrabold text-[#102a56] text-sm break-all">${escT(s.store_host)}</span>
            ${s.label ? `<span class="text-[10px] text-slate-400 font-semibold">${escT(s.label)}</span>` : ''}
            ${Number(s.enabled || 0) ? '' : '<span class="text-[10px] font-bold text-slate-500 bg-slate-100 border border-slate-200 rounded-full px-2 py-0.5">متوقف</span>'}
          </div>
          <div class="text-[11px] text-slate-500 font-semibold mt-1 flex items-center gap-2 flex-wrap">${status}</div>
          ${tech}
          ${salesSpark(s.days, days)}
        </div>
        <div class="flex items-center gap-2 md:gap-3 flex-wrap">
          <div class="text-center min-w-[3.5rem]">
            <div class="text-xl font-extrabold text-[#174ea6] num">${s.orders_window || 0}</div>
            <div class="text-[10px] text-slate-400 font-bold">مبيعات ${days}ي</div>
          </div>
          <div class="text-center min-w-[3.5rem]">
            <div class="text-xl font-extrabold text-slate-700 num">${s.orders_today || 0}</div>
            <div class="text-[10px] text-slate-400 font-bold">اليوم</div>
          </div>
          ${probeBtn}
          <button onclick="toggleSalesStore(${s.id})" class="sbtn !py-1.5"
            title="${Number(s.enabled || 0) ? 'إيقاف المتابعة' : 'تفعيل المتابعة'}">${Number(s.enabled || 0) ? '⏸' : '▶'}</button>
          <button onclick="deleteSalesStore(${s.id})" class="sbtn !py-1.5 !text-rose-600 !border-rose-200" title="حذف">🗑</button>
        </div>
      </div>
    </div>`;
  }).join('');
}

function renderSalesConfig(cfg) {
  const p = document.getElementById('sales-proxy');
  if (p && document.activeElement !== p) p.value = cfg.proxy || '';
  const rot = document.getElementById('sales-rotate');
  const auto = document.getElementById('sales-auto');
  if (rot) rot.checked = !!cfg.rotate && !!cfg.rotator;
  if (auto) auto.checked = !!cfg.auto;
  if (rot) rot.disabled = !cfg.rotator;
}

async function addSalesStore(btn) {
  const url = (document.getElementById('sales-url').value || '').trim();
  const label = (document.getElementById('sales-label').value || '').trim();
  if (!url) { salesMsg('أدخل رابط المنتج', 'error'); return; }
  if (btn) btn.disabled = true;
  salesMsg('جارٍ إضافة المتجر…');
  try {
    const r = await fetch('/api/sales/stores', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, label })
    });
    const d = await r.json();
    if (!r.ok || !d.ok) { salesMsg((d && d.error) || 'تعذّر إضافة المتجر', 'error'); return; }
    document.getElementById('sales-url').value = '';
    document.getElementById('sales-label').value = '';
    salesMsg('أُضيف المتجر — ستظهر الأرقام بعد قليل.', 'ok');
    loadSales();
  } catch (e) {
    salesMsg('خطأ في الاتصال', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteSalesStore(id) {
  if (!confirm('حذف هذا المتجر من متابعتك؟')) return;
  try {
    const r = await fetch('/api/sales/stores/' + id, { method: 'DELETE', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) salesMsg((d && d.error) || 'تعذّر الحذف', 'error');
    loadSales();
  } catch (e) { salesMsg('خطأ في الاتصال', 'error'); }
}

async function toggleSalesStore(id) {
  try {
    const r = await fetch('/api/sales/stores/' + id + '/toggle', { method: 'POST', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) salesMsg((d && d.error) || 'تعذّر التغيير', 'error');
    loadSales();
  } catch (e) { salesMsg('خطأ في الاتصال', 'error'); }
}

// ── admin-only actions ──
async function probeSalesStore(id, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/sales/admin/stores/' + id + '/probe', { method: 'POST', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) salesMsg((d && d.error) || 'تعذّر بدء القياس', 'error');
    loadSales();
  } catch (e) { salesMsg('خطأ في الاتصال', 'error'); }
  finally { if (btn) btn.disabled = false; }
}

async function probeAllSales(btn) {
  if (!confirm('سيتم إنشاء طلبية قياس لكل متجر مفعّل. متابعة؟')) return;
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/sales/admin/probe-all', { method: 'POST', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    salesMsg(r.ok && d.ok ? `بدأ قياس ${d.queued} متجر…` : ((d && d.error) || 'تعذّر البدء'), r.ok && d.ok ? 'ok' : 'error');
    loadSales();
  } catch (e) { salesMsg('خطأ في الاتصال', 'error'); }
  finally { if (btn) btn.disabled = false; }
}

async function saveSalesConfig(btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/sales/admin/config', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        proxy: document.getElementById('sales-proxy').value || '',
        rotate: document.getElementById('sales-rotate').checked,
        auto: document.getElementById('sales-auto').checked
      })
    });
    const d = await r.json().catch(() => ({}));
    salesMsg(r.ok && d.ok ? 'تم حفظ الإعدادات.' : ((d && d.error) || 'تعذّر الحفظ'), r.ok && d.ok ? 'ok' : 'error');
    loadSales();
  } catch (e) { salesMsg('خطأ في الاتصال', 'error'); }
  finally { if (btn) btn.disabled = false; }
}
