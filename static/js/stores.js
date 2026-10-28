async function loadStores() {
  const r = await fetch('/api/stores' + adminUserParam('?')); const d = await r.json();
  document.getElementById('stores').innerHTML = d.stores.map(s => `
    <tr class="hover:bg-slate-50 transition">
      <td class="py-3 px-4 font-bold text-blue-700 font-mono">${escT(s.store_domain)}</td>
      <td class="text-slate-600 px-4">${escT(s.platform || '')}</td>
      <td class="num font-bold text-slate-800 px-4">${s.total_ads}</td>
      <td class="text-xs text-slate-500 font-mono px-4">${(s.last_seen || '').slice(0, 10)}</td>
      <td class="text-center px-4">
        <button data-domain="${escT(s.store_domain)}" onclick="trackDomain(this.dataset.domain)" class="text-xs px-3 py-1.5 rounded-lg bg-white hover:bg-[#eef6ff] text-[#174ea6] border border-[#9fbbe0] font-bold transition shadow-sm">+ تتبع هذا المتجر</button>
      </td>
    </tr>`).join('') || '<tr><td colspan="5" class="text-center py-6 text-slate-400 font-semibold">لا توجد متاجر مكتشفة بعد.</td></tr>';
}
