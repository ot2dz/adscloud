let salesPoll = null;
let salesCache = [];
let salesToday = '';
let salesAdmin = false;

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

function salesWhen(s) {
  return s.updated_at_local || (s.updated_at || '').replace('T', ' ').slice(0, 16);
}

function salesWeekday(isoDate) {
  try {
    return new Date(isoDate + 'T12:00:00Z').toLocaleDateString('ar-DZ',
      { weekday: 'long', timeZone: 'Africa/Algiers' });
  } catch (e) { return ''; }
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
    salesCache = stores;
    salesToday = d.today || '';
    salesAdmin = !!d.admin;

    const measuredToday = stores.filter(s => s.today_measured);
    document.getElementById('sales-today').textContent =
      measuredToday.length ? measuredToday.reduce((a, s) => a + (s.orders_today || 0), 0) : '—';
    const measuredYesterday = stores.filter(s => s.yesterday_measured);
    document.getElementById('sales-yesterday').textContent =
      measuredYesterday.length ? measuredYesterday.reduce((a, s) => a + (s.orders_yesterday || 0), 0) : '—';
    document.getElementById('sales-window').textContent =
      stores.reduce((a, s) => a + (s.orders_window || 0), 0);
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
    if (hint) {
      hint.textContent = stores.length
        ? (d.admin ? 'عدد القياسات = طلبيات أُنشئت فعليًا' : 'اضغط «📅 تفاصيل» لمبيعات أي يوم')
        : '';
    }

    renderSalesList(stores, days, !!d.admin);
    if (d.admin) renderSalesConfig(d.config || {});

    const running = stores.some(s => s.running);
    if (running && !salesPoll) {
      salesPoll = setInterval(() => {
        const v = document.getElementById('view-sales');
        if (document.hidden || !v || v.classList.contains('hidden')) return;
        if (document.getElementById('sales-modal')) return;
        loadSales();
      }, 4000);
    } else if (!running && salesPoll) {
      clearInterval(salesPoll); salesPoll = null;
    }
  } catch (e) {
    salesMsg('خطأ في الاتصال', 'error');
  }
}

function salesSpark(daily, days) {
  const rows = Array.isArray(daily) ? daily.slice(-days) : [];
  if (!rows.length) return '<div class="h-8 mt-2"></div>';
  const vals = rows.map(r => Number(r.orders || 0));
  const max = Math.max(1, ...vals);
  return '<div class="flex items-end gap-1 h-8 mt-2">' +
    rows.map((r, i) => {
      const v = Number(r.orders || 0);
      let h = v ? Math.max(10, Math.round(v / max * 100)) : 4;
      const col = !r.measured ? '#eef2f7' : (v ? '#2563eb' : '#dbeafe');
      const label = `${r.date}${r.measured ? '' : ' (لا توجد بيانات)'}: ${v}`;
      return `<div style="height:${h}%;background:${col}" class="flex-1 rounded-sm" title="${escT(label)}"></div>`;
    }).join('') + '</div>';
}

function renderSalesList(stores, days, admin) {
  const box = document.getElementById('sales-list');
  if (!stores.length) {
    box.innerHTML = '<div class="text-center py-8 text-slate-400 font-semibold text-sm">لا توجد متاجر بعد — أضف رابط منتج للبدء.</div>';
    return;
  }
  box.innerHTML = stores.map(s => {
    const status = s.running
      ? '<span class="text-[10px] font-bold text-blue-700 bg-blue-50 border border-blue-200 rounded-full px-2 py-0.5">قيد التحديث…</span>'
      : (salesWhen(s)
        ? `<span class="text-[11px] text-slate-400 font-semibold">آخر تحديث: <span dir="ltr">${escT(salesWhen(s))}</span></span>`
        : '<span class="text-[11px] text-slate-400 font-semibold">بانتظار أول تحديث…</span>');
    const off = Number(s.enabled || 0) ? '' : ' opacity-60';
    const todayTxt = s.today_measured ? (s.orders_today || 0) : '—';
    const yesterdayTxt = s.yesterday_measured ? (s.orders_yesterday || 0) : '—';
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
          ${salesSpark(s.daily, days)}
        </div>
        <div class="flex items-center gap-2 md:gap-3 flex-wrap">
          <div class="text-center min-w-[3.5rem]">
            <div class="text-xl font-extrabold text-[#174ea6] num">${s.orders_window || 0}</div>
            <div class="text-[10px] text-slate-400 font-bold">مبيعات ${days}ي</div>
          </div>
          <div class="text-center min-w-[3.5rem]">
            <div class="text-xl font-extrabold text-[#0f766e] num">${yesterdayTxt}</div>
            <div class="text-[10px] text-slate-400 font-bold">أمس</div>
          </div>
          <div class="text-center min-w-[3.5rem]">
            <div class="text-xl font-extrabold ${s.today_measured ? 'text-slate-700' : 'text-slate-300'} num">${todayTxt}</div>
            <div class="text-[10px] text-slate-400 font-bold">اليوم</div>
          </div>
          <button onclick="openSalesDetails(${s.id})" class="sbtn !py-1.5">📅 تفاصيل</button>
          ${probeBtn}
          <button onclick="toggleSalesStore(${s.id})" class="sbtn !py-1.5"
            title="${Number(s.enabled || 0) ? 'إيقاف المتابعة' : 'تفعيل المتابعة'}">${Number(s.enabled || 0) ? '⏸' : '▶'}</button>
          <button onclick="deleteSalesStore(${s.id})" class="sbtn !py-1.5 !text-rose-600 !border-rose-200" title="حذف">🗑</button>
        </div>
      </div>
    </div>`;
  }).join('');
}

// ── per-day detail (modal with a date picker + table) ──
function closeSalesDetails() {
  const m = document.getElementById('sales-modal');
  if (m) m.remove();
}

function salesPickDate(value) {
  const data = window.__salesDetail;
  if (!data) return;
  const row = (data.daily || []).find(r => r.date === value);
  const el = document.getElementById('sales-pick-result');
  if (!el) return;
  if (!row) { el.innerHTML = 'لا توجد بيانات لهذا التاريخ'; return; }
  el.innerHTML = row.measured
    ? `مبيعات <b class="num text-[#174ea6]">${row.orders}</b> طلب`
    : '<span class="text-slate-400">لا توجد بيانات لهذا اليوم بعد</span>';
}

async function openSalesDetails(id) {
  const s = (salesCache || []).find(x => Number(x.id) === Number(id));
  if (!s) return;
  let daily = (s.daily || []).slice();
  try {  // the card shows the selected range; the detail always opens the last 30 days
    const r = await fetch('/api/sales/stores/' + id + '/series?days=30', { credentials: 'same-origin' });
    const d = await r.json();
    if (r.ok && d.ok && Array.isArray(d.daily)) daily = d.daily;
  } catch (e) { /* fall back to the cached 7/14/30-day range */ }
  closeSalesDetails();
  const picked = daily.length ? daily[daily.length - 1].date : (salesToday || '');
  window.__salesDetail = { id: s.id, store_host: s.store_host, daily: daily };
  const total30 = daily.reduce((a, r) => a + (r.measured ? Number(r.orders || 0) : 0), 0);
  const rows = daily.slice().reverse().map(r => `
    <tr class="border-t border-slate-100">
      <td class="py-1.5 px-2 text-xs text-slate-600" dir="ltr">${escT(r.date)}</td>
      <td class="py-1.5 px-2 text-[11px] text-slate-400">${escT(salesWeekday(r.date))}</td>
      <td class="py-1.5 px-2 text-left font-bold ${r.measured ? 'text-[#102a56]' : 'text-slate-300'} num">${r.measured ? r.orders : '—'}</td>
    </tr>`).join('');
  const min = daily.length ? daily[0].date : '';
  const max = daily.length ? daily[daily.length - 1].date : (salesToday || '');
  const modal = document.createElement('div');
  modal.id = 'sales-modal';
  modal.className = 'fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4';
  modal.onclick = (e) => { if (e.target === modal) closeSalesDetails(); };
  modal.innerHTML = `
    <div class="bg-white rounded-2xl shadow-xl max-w-md w-full max-h-[85vh] overflow-auto p-5" dir="rtl">
      <div class="flex items-start justify-between gap-2">
        <div>
          <div class="font-extrabold text-[#102a56] text-sm break-all" dir="ltr">${escT(s.store_host)}</div>
          <div class="text-[11px] text-slate-400 font-semibold mt-1">مبيعات كل يوم · آخر 30 يومًا: <b class="text-[#174ea6] num">${total30}</b></div>
        </div>
        <button onclick="closeSalesDetails()" class="text-slate-400 hover:text-slate-700 text-xl leading-none">✕</button>
      </div>
      <div class="mt-4 bg-[#f8fbff] border border-[#cfe0f5] rounded-xl p-3">
        <label class="slabel">اختر تاريخًا</label>
        <input type="date" value="${escT(picked)}" min="${escT(min)}" max="${escT(max)}"
               onchange="salesPickDate(this.value)"
               class="sinput !text-left" dir="ltr">
        <div id="sales-pick-result" class="text-xs font-semibold text-slate-600 mt-2"></div>
      </div>
      <table class="w-full mt-4">
        <thead>
          <tr class="text-[11px] text-slate-400 font-bold">
            <th class="text-start px-2 py-1">التاريخ</th>
            <th class="text-start px-2 py-1"></th>
            <th class="text-left px-2 py-1">المبيعات</th>
          </tr>
        </thead>
        <tbody>${rows || '<tr><td colspan="3" class="text-center py-6 text-slate-400 text-sm">لا توجد بيانات بعد</td></tr>'}</tbody>
      </table>
    </div>`;
  document.body.appendChild(modal);
  if (picked) salesPickDate(picked);
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
    closeSalesDetails();
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
