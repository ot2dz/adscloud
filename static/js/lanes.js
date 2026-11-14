// ── Settings: parallel scrape lanes for the whole-market scan ──
let lanesData = [];
let lanesHasStatus = false;
const LANE_SRC = {
  scrape: 'خط السحب الحالي (بروكسي السحب أعلاه)',
  status: 'خط فحص الحالة (التابلت) — يوقف الفحص أثناء السحب',
  custom: 'بروكسي آخر',
};

async function lanesLoad() {
  const r = await fetch('/api/scrape-lanes');
  if (!r.ok) return;
  const d = await r.json();
  lanesData = (d.lanes || []).map((ln, i) => Object.assign({}, ln, {keep_index: i}));
  lanesHasStatus = !!d.has_status_line;
  lanesRender();
  const box = document.getElementById('lanes-active');
  const act = d.active || [];
  box.innerHTML = `الخطوط العاملة الآن: <b class="num">${act.length}</b> — ` + act.map(a =>
    `<span class="inline-block px-2 py-0.5 rounded bg-white border border-slate-200 me-1 mb-1">${escT(a.name)} · <span class="font-mono" dir="ltr">${escT(a.proxy)}</span>${a.rotator ? ' · 🔄' : ''}</span>`).join('')
    + (act.length < 2 ? '<div class="text-slate-500 mt-1">أضف خطاً ثانياً على الأقل ليصبح السحب متوازياً.</div>' : '');
}

function lanesRender() {
  const el = document.getElementById('lanes-list');
  if (!el) return;
  if (!lanesData.length) {
    el.innerHTML = '<div class="text-slate-400">لا خطوط محددة — يُستعمل خط السحب الحالي وحده (كما كان).</div>';
    return;
  }
  const inp = 'bg-[#f8fbff] border border-[#b7c9e2] rounded-lg px-2 py-1.5 text-xs';
  el.innerHTML = lanesData.map((ln, i) => `
    <div class="border border-[#d7e3f4] rounded-xl p-2.5 space-y-2 ${ln.enabled === false ? 'opacity-50' : ''}">
      <div class="flex items-center gap-2 flex-wrap">
        <input type="checkbox" ${ln.enabled !== false ? 'checked' : ''} onchange="lanesData[${i}].enabled=this.checked; lanesRender()" title="تفعيل">
        <input value="${escT(ln.name || '')}" oninput="lanesData[${i}].name=this.value" placeholder="اسم الخط (مثلاً: خط العمل)" class="${inp} w-48">
        <span class="text-slate-600 font-semibold">${escT(LANE_SRC[ln.source] || ln.source)}</span>
        <button onclick="lanesData.splice(${i},1); lanesRender()" class="ms-auto text-rose-600 font-bold">✕</button>
      </div>
      ${ln.source === 'status' && !lanesHasStatus ? '<div class="text-amber-700">⚠️ لا يوجد بروكسي مخصص لفحص الحالة في الإعدادات — هذا الخط سيُتجاهل.</div>' : ''}
      ${ln.source === 'custom' ? `<div class="grid md:grid-cols-3 gap-2">
        <input value="${escT(ln.proxy || '')}" oninput="lanesData[${i}].proxy=this.value" dir="ltr"
          placeholder="${ln.proxy_hint ? escT(ln.proxy_hint) + ' (محفوظ — اتركه فارغاً للإبقاء)' : 'host:port:user:pass'}" class="${inp} font-mono">
        <input value="${escT(ln.rotate_url || '')}" oninput="lanesData[${i}].rotate_url=this.value" dir="ltr" placeholder="رابط تدوير IP (اختياري) http://…/rotate" class="${inp} font-mono">
        <input value="${escT(ln.token_env || '')}" oninput="lanesData[${i}].token_env=this.value" dir="ltr" placeholder="متغير بيئة الرمز (اختياري) LANE3_ROTATE_TOKEN" class="${inp} font-mono">
      </div>` : ''}
    </div>`).join('');
}

function lanesAdd(source) {
  const names = {scrape: 'خط العمل', status: 'خط المحل (التابلت)', custom: 'خط ' + (lanesData.length + 1)};
  lanesData.push({source, name: names[source], enabled: true});
  lanesRender();
}

async function lanesSave() {
  const r = await fetch('/api/scrape-lanes', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                              body: JSON.stringify({lanes: lanesData})});
  const d = await r.json();
  if (!r.ok) { alert(d.error || 'فشل الحفظ'); return; }
  lanesLoad();
}
