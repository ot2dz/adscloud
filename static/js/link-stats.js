(function () {
  function fmt(n) {
    return Number(n || 0).toLocaleString('en-US');
  }

  async function loadPageStats() {
    const box = document.getElementById('page-stats');
    if (!box) return;
    const pid = (typeof currentTargetFilter !== 'undefined' && currentTargetFilter &&
                 currentTargetFilter.type === 'page') ? currentTargetFilter.value : '';
    if (!pid) { box.classList.add('hidden'); return; }
    try {
      const res = await fetch('/api/link-stats?page_id=' + encodeURIComponent(pid));
      if (!res.ok) { box.classList.add('hidden'); return; }
      const data = await res.json();
      const s = data.latest;
      if (!s) { box.classList.add('hidden'); return; }
      const when = String(s.collected_at || '').replace('T', ' ').slice(0, 16);
      box.innerHTML =
        '🔗 <b>إحصائيات الرابط</b> · ' +
        '👁️ ' + fmt(s.views) + ' مشاهدة · ' +
        '👍 ' + fmt(s.reactions) + ' تفاعل · ' +
        '🕒 آخر تحديث: ' + when +
        '<span class="block text-slate-500 mt-1">ℹ️ تفاعل المنشور ≠ دليل على أن المنتج رابح</span>';
      box.classList.remove('hidden');
    } catch (_e) {
      box.classList.add('hidden');
    }
  }

  window.loadPageStats = loadPageStats;

  const pid = new URLSearchParams(location.search).get('page_id');
  if (!pid) return;

  const apply = () => {
    if (typeof currentUser === 'undefined' || !currentUser || typeof loadAds !== 'function') {
      setTimeout(apply, 1200);
      return;
    }
    currentTargetFilter = { id: null, type: 'page', value: pid, label: 'صفحة ' + pid };
    const banner = document.getElementById('target-filter-banner');
    const label = document.getElementById('target-filter-label');
    const type = document.getElementById('target-filter-type');
    if (banner) banner.classList.remove('hidden');
    if (label) label.textContent = 'صفحة ' + pid;
    if (type) type.textContent = 'صفحة';
    loadAds();
    setTimeout(loadPageStats, 800);
  };
  setTimeout(apply, 900);
})();
