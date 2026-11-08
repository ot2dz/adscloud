// ── Jobs loop ──
const JOB_AR = { pending:'في الانتظار', running:'قيد التنفيذ', done:'انتهى', failed:'فشل', stopped:'أُوقف', stopping:'جارٍ الإيقاف' };
// Below this share of Meta's estimate, a run that Meta "finished" most likely
// hit Meta's pagination limit rather than the real end of the results.
const JOB_COVERAGE_OK = 0.9;
const fmtN = n => Number(n || 0).toLocaleString('en-US');

// Meta estimate vs. what Meta really returned, and whether the run reached
// the end. Empty for jobs that do not track it (old jobs, market scans).
function jobCoverage(j) {
  const reported = j.reported_count == null ? null : Number(j.reported_count);
  const scanned = Number(j.scanned_count || 0);
  const finished = ['done', 'stopped', 'failed'].includes(j.status);
  if (reported == null && !scanned) return '';
  const nums = `<span class="num">${reported != null ? 'Meta: ' + fmtN(reported) + ' · ' : ''}مفحوص: ${fmtN(scanned)} · محفوظ: ${fmtN(j.result_count)}</span>`;
  let verdict = '';
  if (finished && j.completed != null) {
    if (Number(j.completed) !== 1) {
      verdict = `<span class="text-amber-700">⚠️ لم يكتمل — توقف قبل نهاية النتائج</span>`;
    } else if (reported && scanned < reported * JOB_COVERAGE_OK) {
      verdict = `<span class="text-orange-700" title="Meta أوقفت الصفحات قبل الوصول لعددها التقديري">🟠 Meta توقفت عند ${fmtN(scanned)} من ${fmtN(reported)} (${Math.round(scanned / reported * 100)}%) — غالباً حد Meta</span>`;
    } else {
      verdict = `<span class="text-emerald-700">✅ اكتمل — Meta لم تعد تعطي نتائج</span>`;
    }
  }
  return `<div class="text-[11px] text-slate-600 font-semibold flex flex-wrap gap-x-2">📊 ${nums}${verdict ? ' · ' + verdict : ''}</div>`;
}
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
      ${jobCoverage(j)}
      ${j.status === 'failed' && j.error ? `
        <div class="text-xs text-rose-700 font-semibold truncate" title="${escT(j.error)}">⚠️ ${escT(j.error)}</div>` : ''}
    </div>`).join('');
}
async function stopJob(id){ await fetch('/api/jobs/'+id+'/stop',{method:'POST'}); loadJobs(); }

