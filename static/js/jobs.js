// ── Jobs loop ──
const JOB_AR = { pending:'في الانتظار', running:'قيد التنفيذ', done:'انتهى', failed:'فشل', stopped:'أُوقف', stopping:'جارٍ الإيقاف' };
async function loadJobs() {
  const r = await fetch('/api/jobs'); const d = await r.json();
  const el = document.getElementById('jobs');
  if (!d.jobs.length) { el.innerHTML = '<div class="text-slate-400 text-sm font-semibold py-4 text-center">لا أوامر بعد.</div>'; return; }
  const color = {
    pending:'text-slate-700 bg-slate-100 border-slate-300',
    running:'text-sky-700 bg-sky-50 border-sky-300',
    done:'text-emerald-700 bg-emerald-50 border-emerald-300',
    failed:'text-rose-700 bg-rose-50 border-rose-300',
    stopped:'text-amber-700 bg-amber-50 border-amber-300',
    stopping:'text-amber-700 bg-amber-50 border-amber-300'
  };
  el.innerHTML = d.jobs.map(j => `
    <div class="bg-[#f8fbff] border border-[#cfe0f5] rounded-xl px-4 py-3 text-sm space-y-1.5 shadow-sm">
      <div class="flex items-center justify-between gap-2">
        <div class="truncate">
          <span class="font-bold text-[#102a56]">${j.query ? escT(j.query) : '🔗 رابط مخصص'}</span>
          <span class="text-[11px] text-slate-500 font-medium"> · ${j.source === 'schedule' ? 'مجدول تلقائي' : 'يدوي'}</span>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          ${(j.status === 'done' || j.status === 'stopped') ? `<span class="text-xs text-emerald-700 font-bold num">${j.result_count || 0} إعلان</span>` : ''}
          <span class="text-[11px] px-2.5 py-0.5 rounded-full border font-bold ${color[j.status] || 'bg-slate-100'}">${JOB_AR[j.status] || escT(j.status)}</span>
          ${(j.status === 'running' || j.status === 'pending') ? `<button onclick="stopJob(${j.id})" class="text-xs text-rose-600 hover:underline font-bold">⏹ إيقاف</button>` : ''}
        </div>
      </div>
      ${j.status === 'running' ? `
        <div class="text-xs text-sky-700 font-semibold">📥 ${Number(j.progress) || 0} إعلان مسحوب ${j.progress_note ? ('· ' + escT(j.progress_note)) : ''}</div>
        <div class="h-1.5 bg-slate-200 rounded-full overflow-hidden"><div class="h-full bg-blue-600 animate-pulse" style="width:100%"></div></div>` : ''}
      ${j.status === 'pending' && j.retry_at ? `
        <div class="text-xs text-amber-700 font-semibold">⏸ انقطع بعد ${j.result_count || 0} إعلان · سيُستأنف من نفس النقطة ${new Date(j.retry_at).toLocaleTimeString('ar-DZ', {hour: '2-digit', minute: '2-digit'})} (محاولة ${j.attempts || 1})</div>` : ''}
      ${j.status === 'failed' && j.error ? `
        <div class="text-xs text-rose-700 font-semibold truncate" title="${escT(j.error)}">⚠️ ${escT(j.error)}</div>` : ''}
    </div>`).join('');
}
async function stopJob(id){ await fetch('/api/jobs/'+id+'/stop',{method:'POST'}); loadJobs(); }

