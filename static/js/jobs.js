// ── Jobs section: what runs now, what waits, and a history per kind.
// Page expansion has its own tab, so it never hides the user's jobs.
const fmtN = n => Number(n || 0).toLocaleString('en-US');
const JOB_KIND = { keyword: '🔤 كلمة', page: '📄 صفحة', store: '🏪 متجر', link: '🔗 رابط', market: '🌍 سحب السوق',
                   check: '🔎 فحص', expand: '🔁 توسيع' };
const JOB_SOURCE = { manual: 'أنت', manual_target: 'أنت (منافس)', new_competitor: 'منافس جديد', manual_all: 'فحص كل المنافسين',
                     schedule_manual: 'فحص كل المنافسين', manual_global: 'أنت (السوق الخارجي)', manual_global_all: 'أنت (السوق الخارجي)',
                     manual_expand: 'أنت', schedule: 'مجدول تلقائياً', daily: 'يومي تلقائي', daily_rescan: 'يومي تلقائي', page_expand: 'تلقائي' };
const JOB_GROUPS = [['mine', '👤 مهامي'], ['checks', '🔎 الفحوصات'], ['auto', '⏰ المجدولة'], ['expand', '🔁 توسيع الصفحات']];
const JOB_VERDICT = { ok: '✅ نجحت', partial: '⚠️ ناقصة', empty: '∅ بلا نتائج', failed: '❌ فشلت', stopped: '⏹ أُوقفت' };
const CHECK_LINE = { tablet: 'خط التابلت', scrape: 'خط السحب (التابلت متوقف)' };
const jobsUi = { group: 'mine', verdict: '', q: '', limit: 30, busy: false, again: false, timer: null };

function jobClock(iso) {
  return iso ? new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'Africa/Algiers' }) : '';
}
function jobDay(iso) { return iso ? new Date(iso).toLocaleDateString('en-CA', { timeZone: 'Africa/Algiers' }) : ''; }
function jobDayLabel(day) {
  const t = todayDZ();
  return day === t ? 'اليوم' : day === shiftDay(t, -1) ? 'أمس' : day;
}
function jobDuration(from, to) {
  if (!from) return '';
  const s = Math.max(0, Math.round((new Date(to || Date.now()) - new Date(from)) / 1000));
  if (s < 60) return s + ' ث';
  if (s < 3600) return Math.round(s / 60) + ' د';
  return Math.floor(s / 3600) + ' س ' + Math.round((s % 3600) / 60) + ' د';
}
function jobBtn(fn, id, text, cls) { return `<button onclick="${fn}(${Number(id)})" class="jb-act ${cls}">${text}</button>`; }
function jobMeta(j) {
  return [JOB_KIND[j.kind] || '', JOB_SOURCE[j.source] || escT(j.source || ''), j.market === 'global' ? 'السوق الخارجي' : '',
          j.kind !== 'market' && j.start_date_min ? `<span dir="ltr">${escT(j.start_date_min)} → ${escT(j.start_date_max || '')}</span>` : '',
          '#' + Number(j.id)].filter(Boolean).join(' · ');
}
// Numbers of a finished job, in the words of its kind.
function jobNumbers(j) {
  const s = j.summary;
  if (j.kind === 'check') {
    if (!s) return `${fmtN(j.result_count)} إعلان فُحص`;
    return `<b class="text-emerald-700">${fmtN(s.confirmed)} نشط</b> · <b class="text-rose-700">${fmtN(s.stopped)} توقف</b> · ${fmtN(s.new)} جديد · ${fmtN(s.pages)} صفحة`;
  }
  if (j.kind === 'expand') return `${fmtN(j.slices_ok)}/${fmtN(j.slices_total)} صفحة · ${fmtN(j.result_count)} إعلان`;
  const out = [`<b class="text-[#102a56]">${fmtN(j.result_count)} إعلان محفوظ</b>`];
  if (Number(j.scanned_count)) out.push(`فُحص ${fmtN(j.scanned_count)}${j.reported_count ? ' من ' + fmtN(j.reported_count) + ' حسب Meta' : ''}`);
  if (Number(j.slices_total)) out.push(`شرائح ${fmtN(j.slices_ok)}/${fmtN(j.slices_total)}`);
  return out.join(' · ');
}
// Why a job is not a plain success, in one sentence.
function jobWhy(j) {
  if (j.verdict === 'failed') return j.error ? escT(j.error) : 'فشلت بلا سبب مسجَّل.';
  if (j.verdict === 'empty') return j.error ? escT(j.error) : 'انتهت دون أي إعلان محفوظ (لا نتائج مطابقة، أو كلها خارج الفلتر).';
  if (j.verdict !== 'partial') return '';
  if (j.kind === 'check') return `${fmtN(j.summary && j.summary.skipped_batches)} دفعة لم تُفحص كاملة — إعلاناتها بقيت كما هي.`;
  if (Number(j.slices_total) && Number(j.completed) !== 1) return `${fmtN(j.slices_total - j.slices_ok)} شريحة لم تكتمل.`;
  if (j.completed != null && Number(j.completed) !== 1) return 'توقفت قبل نهاية النتائج.';
  return `Meta توقفت عند ${fmtN(j.scanned_count)} من ${fmtN(j.reported_count)} (${Math.round(j.scanned_count / j.reported_count * 100)}%) — غالباً حدّ Meta وليس خطأً عندك.`;
}

function jobNowCard(j, live, label) {
  const l = live[String(j.id)] || {};
  const steps = l.steps && l.steps[1] ? l.steps : null;
  const scanned = Number(l.scanned || 0), rep = Number(l.reported || 0);
  const pct = steps ? Math.round(steps[0] / steps[1] * 100)
    : (['keyword', 'page', 'store', 'link'].includes(j.kind) && rep && scanned ? Math.min(100, Math.round(scanned / rep * 100)) : null);
  const unit = { check: 'دفعة', expand: 'صفحة', market: 'شريحة' }[j.kind] || '';
  const nums = [`${fmtN(l.scraped != null ? l.scraped : j.result_count)} إعلان`];
  if (steps) nums.push(`${fmtN(steps[0])}/${fmtN(steps[1])} ${unit}`);
  else if (rep) nums.push(`فُحص ${fmtN(scanned)} من ${fmtN(rep)}`);
  nums.push(`منذ ${jobDuration(j.started_at)}`);
  const state = j.stop_requested ? '⏹ جارٍ الإيقاف…' : j.pause_requested ? '⏸ جارٍ الإيقاف المؤقت، يُحفظ الموضع…' : '';
  return `<div class="jb-now ${j.kind === 'expand' ? 'bg' : ''}">
    <div class="flex justify-between gap-2"><span class="jb-line">${escT(j.kind === 'check' && l.line ? '🔎 ' + (CHECK_LINE[l.line] || l.line) : label)}</span>
      <span class="shrink-0">${state ? '' : jobBtn('pauseJob', j.id, '⏸ مؤقت', 'text-amber-700') + jobBtn('stopJob', j.id, '⏹ إيقاف', 'text-rose-600')}</span></div>
    <div class="jb-title">${escT(j.title)}</div><div class="jb-sub">${jobMeta(j)}</div>
    <div class="jb-bar"><i class="${pct == null ? 'animate-pulse' : ''}" style="width:${pct == null ? 100 : Math.max(2, pct)}%"></i></div>
    <div class="jb-sub num">${pct == null ? '' : `<b class="text-blue-700">${pct}%</b> · `}${nums.join(' · ')}</div>
    ${state ? `<div class="jb-why partial">${state}</div>` : l.last ? `<div class="jb-sub truncate" title="${escT(l.last)}">${escT(l.last)}</div>` : ''}
  </div>`;
}
function jobIdle(label, text) {
  return `<div class="jb-now idle"><span class="jb-line">${label}</span><div class="jb-sub">${text}</div></div>`;
}
function renderNow(d) {
  const isCheck = j => j.kind === 'check';
  const scrapes = d.running.filter(j => !isCheck(j)), checks = d.running.filter(isCheck);
  const overScrape = checks.some(j => (d.live[String(j.id)] || {}).line === 'scrape');
  return (scrapes.length ? scrapes.map(j => jobNowCard(j, d.live, '📥 خط السحب')).join('')
      : jobIdle('📥 خط السحب', overScrape ? 'يستعمله الفحص الآن، ويتركه فور وصول أي سحب.' : 'فارغ — جاهز لأي سحب.'))
    + (checks.length ? checks.map(j => jobNowCard(j, d.live, '🔎 الفحص')).join('')
      : jobIdle('🔎 الفحص', d.waiting.some(isCheck) ? 'لا يعمل الآن — فحص في الانتظار.' : 'لا فحص يعمل الآن.'));
}
function jobWaitReason(j) {
  if (j.status === 'paused') return '⏸ أوقفتها مؤقتاً — «استئناف» يكمل من نفس النقطة.';
  if (j.retry_at && new Date(j.retry_at) > new Date()) return `🔌 انقطعت — تُستأنف ${jobClock(j.retry_at)} من نفس النقطة (إعادة ${Number(j.attempts) || 1} من 3).${j.error ? ' ' + escT(j.error) : ''}`;
  if (j.kind === 'check') return j.paused_reason === 'auto' ? 'تنتظر خطاً متاحاً، وتكمل من نفس النقطة.' : 'تنتظر خط الفحص.';
  if (j.paused_reason === 'auto') return 'تركت الدور لمهمة أهم، وتكمل وحدها من نفس النقطة.';
  return 'تنتظر دورها على خط السحب.';
}
function renderQueue(rows) {
  let n = 0;
  return rows.map(j => `<div class="jb-row"><span class="jb-n num shrink-0">${j.status === 'paused' ? '⏸' : ++n}</span>
    <div class="jb-main"><div class="jb-title">${escT(j.title)}</div>
      <div class="jb-sub">${jobMeta(j)}${Number(j.result_count) ? ` · ${fmtN(j.result_count)} إعلان حتى الآن` : ''}</div>
      <div class="jb-sub text-amber-700">${jobWaitReason(j)}</div></div>
    <div class="shrink-0">${j.status === 'paused' ? jobBtn('resumeJob', j.id, '▶ استئناف', 'text-emerald-700') : jobBtn('pauseJob', j.id, '⏸ مؤقت', 'text-amber-700')}${jobBtn('stopJob', j.id, '✕ إلغاء', 'text-rose-600')}</div></div>`).join('');
}
function renderHistoryRow(j) {
  const why = jobWhy(j), end = j.finished_at;
  const again = j.verdict === 'failed' && j.resumable ? '▶ أكمل من حيث توقفت' : '↻ أعدها';
  return `<div class="jb-row"><span class="jb-v ${j.verdict}">${JOB_VERDICT[j.verdict] || ''}</span>
    <div class="jb-main"><div class="jb-title">${escT(j.title)}</div><div class="jb-sub">${jobMeta(j)}</div>
      <div class="jb-sub num">${jobNumbers(j)}</div>${why ? `<div class="jb-why ${j.verdict}">${why}</div>` : ''}</div>
    <div class="shrink-0 text-end"><div class="jb-sub num" title="بدأت ${jobClock(j.started_at)}">${jobClock(end)}${j.started_at && end ? ' · ' + jobDuration(j.started_at, end) : ''}</div>
      ${j.kind === 'expand' && j.verdict !== 'failed' ? '' : jobBtn('retryJob', j.id, again, 'text-blue-700')}</div></div>`;
}
function renderHistory(h) {
  if (!h.jobs.length) return `<div class="text-slate-400 text-sm font-semibold py-8 text-center">${jobsUi.q || jobsUi.verdict ? 'لا مهام تطابق هذا البحث.' : 'لا مهام منتهية هنا بعد.'}</div>`;
  let day = null;
  return h.jobs.map(j => {
    const d = jobDay(j.finished_at || j.created_at);
    const head = d !== day ? `<div class="jb-day">${jobDayLabel(d)}</div>` : '';
    day = d;
    return head + renderHistoryRow(j);
  }).join('');
}
function renderExpandDays(days) {
  if (!days.length) return '';
  return `<div class="jb-sub pb-1">حصيلة كل يوم (آخر 14 يوماً):</div><div class="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 pb-2">` + days.map(d =>
    `<div class="srow !block !cursor-default"><div class="srow-t">${jobDayLabel(d.day)}</div>
     <div class="jb-sub num">${fmtN(d.pages)} صفحة · ${fmtN(d.ads)} إعلان · ${fmtN(d.runs)} مهمة${d.failed ? ` · <b class="text-rose-700">${fmtN(d.failed)} فشلت</b>` : ''}</div></div>`).join('') + '</div>';
}

// Only touches the page when something changed (keeps scroll and selection).
function jobsSet(id, html) {
  const el = document.getElementById(id);
  if (el && el._html !== html) { el._html = html; el.innerHTML = html; }
}
async function loadJobs() {
  const view = document.getElementById('view-jobs');
  if (!view || view.classList.contains('hidden')) return;  // loaded when opened
  if (jobsUi.busy) { jobsUi.again = true; return; }
  jobsUi.busy = true;
  try {
    const r = await fetch('/api/jobs?' + new URLSearchParams({ group: jobsUi.group, verdict: jobsUi.verdict, q: jobsUi.q, limit: jobsUi.limit }));
    if (!r.ok) return;
    const d = await r.json(), h = d.history;
    if (h.group !== jobsUi.group) return;  // the user moved to another tab meanwhile
    jobsSet('jobs-now', renderNow(d));
    document.getElementById('jobs-queue-box').classList.toggle('hidden', !d.waiting.length);
    jobsSet('jobs-queue-n', String(d.waiting.length));
    jobsSet('jobs-queue', renderQueue(d.waiting));
    jobsSet('jobs-groups', JOB_GROUPS.map(([k, label]) => `<button onclick="jobsGroup('${k}')" class="jb-chip ${k === jobsUi.group ? 'on' : ''}">${label} <span class="num opacity-70">${fmtN(d.groups[k].total)}</span>${d.groups[k].failed ? `<b class="bad num" title="فشلت في آخر 48 ساعة">${d.groups[k].failed}</b>` : ''}</button>`).join(''));
    const all = Object.values(h.counts).reduce((a, b) => a + b, 0);
    jobsSet('jobs-verdicts', [['', 'الكل', all], ...Object.entries(JOB_VERDICT).map(([k, t]) => [k, t, h.counts[k] || 0])]
      .filter(([k, , n]) => !k || n || k === jobsUi.verdict)
      .map(([k, t, n]) => `<button onclick="jobsVerdict('${k}')" class="jb-chip ${k === jobsUi.verdict ? 'on' : ''}">${t} <span class="num opacity-70">${fmtN(n)}</span></button>`).join(''));
    jobsSet('jobs-history', (jobsUi.q || jobsUi.verdict ? '' : renderExpandDays(d.expand_days)) + renderHistory(h));
    document.getElementById('jobs-more').classList.toggle('hidden', h.jobs.length >= h.total || jobsUi.limit >= 200);
  } catch (_e) { /* the next refresh tries again */ } finally {
    jobsUi.busy = false;
    if (jobsUi.again) { jobsUi.again = false; loadJobs(); }
  }
}
function jobsGroup(g) { Object.assign(jobsUi, { group: g, verdict: '', limit: 30 }); loadJobs(); }
function jobsVerdict(v) { Object.assign(jobsUi, { verdict: v, limit: 30 }); loadJobs(); }
function jobsMore() { jobsUi.limit = Math.min(200, jobsUi.limit + 30); loadJobs(); }
function jobsSearch() {
  clearTimeout(jobsUi.timer);
  jobsUi.timer = setTimeout(() => { jobsUi.q = document.getElementById('jobs-q').value.trim(); jobsUi.limit = 30; loadJobs(); }, 300);
}
async function jobPost(id, what) {
  const r = await fetch('/api/jobs/' + id + '/' + what, { method: 'POST' });
  return r.json().catch(() => ({}));
}
async function pauseJob(id) { await jobPost(id, 'pause'); loadJobs(); }
async function resumeJob(id) {
  const d = await jobPost(id, 'resume');
  if (!d.ok) alert('⚠️ ' + (d.error || 'تعذّر الاستئناف'));
  loadJobs();
}
async function stopJob(id) {
  if (!confirm('إيقاف هذه المهمة نهائياً؟ ما جُمع منها يبقى محفوظاً.')) return;
  await jobPost(id, 'stop'); loadJobs();
}
async function retryJob(id) {
  const d = await jobPost(id, 'retry');
  if (!d.ok) alert('⚠️ ' + (d.error || 'تعذّرت الإعادة'));
  loadJobs();
}
