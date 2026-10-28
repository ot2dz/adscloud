// ── Settings & Telegram Integration ──
async function loadSettings() {
  const r = await fetch('/api/settings'); const s = await r.json();
  document.getElementById('cfg-sched-enabled').checked = s.schedule_enabled === 'true';
  document.getElementById('cfg-entity-stats').checked = (s.entity_stats_enabled || 'true') === 'true';
  document.getElementById('cfg-registration-open').checked = s.registration_open === 'true';
  document.getElementById('cfg-cta-filter').checked = (s.cta_filter_enabled || 'true') === 'true';
  document.getElementById('cfg-arabic-only').checked = (s.arabic_only || 'true') === 'true';
  document.getElementById('cfg-cta-allowlist').value = (s.cta_allowlist || '').split(',').join(', ');
  document.getElementById('cfg-month-rescan').checked = (s.month_rescan_enabled || 'true') === 'true';
  document.getElementById('cfg-month-rescan-hour').value = s.month_rescan_hour || 23;
  document.getElementById('cfg-status-check').checked = (s.status_check_enabled || 'true') === 'true';
  document.getElementById('cfg-status-check-hour').value = s.status_check_hour || 3;
  document.getElementById('status-check-summary').innerText = statusCheckSummary(s.status_check_summary);
  document.getElementById('month-rescan-last').innerText = s.month_rescan_last ? `(آخر تشغيل: ${s.month_rescan_last})` : '';
  document.getElementById('cfg-sched-interval').value = s.interval_hours || 12;
  document.getElementById('cfg-notify-enabled').checked = s.notify_enabled === 'true';
  document.getElementById('cfg-tg-token').value = s.telegram_token || '';
  document.getElementById('cfg-tg-chat').value = s.telegram_chat_id || '';
  document.getElementById('cfg-proxy').value = s.meta_ads_proxy || '';
  const blockedEl = document.getElementById('cfg-blocked');
  if (blockedEl) blockedEl.value = s.blocked_domains || '';
  const envEl = document.getElementById('cfg-proxy-env');
  if (envEl) {
    if (s.env_meta_ads_proxy) {
      envEl.classList.remove('hidden');
      envEl.textContent = '⚠️ يوجد بروكسي أيضاً في متغيّر البيئة META_ADS_PROXY (يُستخدم فقط إذا كان الحقل فارغاً).';
    } else {
      envEl.classList.add('hidden');
    }
  }
  document.getElementById('last-sched-time').innerText = s.last_scheduled_at ? s.last_scheduled_at.slice(0, 16).replace('T', ' ') : 'لم تبدأ بعد';
}

async function saveAllSettings() {
  const token = document.getElementById('cfg-tg-token').value.trim();
  const chat = document.getElementById('cfg-tg-chat').value.trim();
  const schedEn = document.getElementById('cfg-sched-enabled').checked;
  const schedInt = parseFloat(document.getElementById('cfg-sched-interval').value) || 12;
  const notifEn = document.getElementById('cfg-notify-enabled').checked;

  const r = await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      schedule_enabled: schedEn,
      interval_hours: schedInt,
      entity_stats_enabled: document.getElementById('cfg-entity-stats').checked,
      registration_open: document.getElementById('cfg-registration-open').checked,
      cta_filter_enabled: document.getElementById('cfg-cta-filter').checked,
      arabic_only: document.getElementById('cfg-arabic-only').checked,
      cta_allowlist: document.getElementById('cfg-cta-allowlist').value,
      month_rescan_enabled: document.getElementById('cfg-month-rescan').checked,
      month_rescan_hour: parseInt(document.getElementById('cfg-month-rescan-hour').value || '23'),
      status_check_enabled: document.getElementById('cfg-status-check').checked,
      status_check_hour: parseInt(document.getElementById('cfg-status-check-hour').value || '3'),
      notify_enabled: notifEn,
      telegram_token: token,
      telegram_chat_id: chat,
      meta_ads_proxy: document.getElementById('cfg-proxy').value.trim(),
      blocked_domains: (document.getElementById('cfg-blocked')?.value || '').trim()
    })
  });
  const d = await r.json();
  if (d.ok) {
    let msg = '✅ تم حفظ جميع الإعدادات بنجاح!';
    if (d.purged != null) msg += `\n🧹 حُذف ${d.purged} إعلانًا لشركات كبرى من القاعدة.`;
    alert(msg);
    loadSettings(); loadAds(); loadStats();  // CTA filter etc. apply immediately
  }
}

async function testProxy() {
  const el = document.getElementById('proxy-test-result');
  const btn = document.getElementById('btn-proxy-test');
  btn.disabled = true; btn.innerText = '⏳ جارٍ الفحص…';
  el.classList.add('hidden');
  try {
    const r = await fetch('/api/proxy/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ meta_ads_proxy: document.getElementById('cfg-proxy').value.trim() })
    });
    const d = await r.json();
    el.classList.remove('hidden');
    const lines = (d.results || []).map(x => x.ok
      ? '✅ ' + x.proxy + ' → IP: ' + x.ip
      : '❌ ' + x.proxy + ' → ' + x.error);
    if (!lines.length) lines.push('❌ ' + (d.error || 'فشل الفحص'));
    el.className = 'p-3 rounded-xl text-xs leading-relaxed font-bold whitespace-pre-line ' +
      (d.ok ? 'bg-emerald-50 text-emerald-800 border border-emerald-300'
            : 'bg-rose-50 text-rose-800 border border-rose-300');
    el.textContent = lines.join('\n');
  } catch (err) {
    el.classList.remove('hidden');
    el.className = 'p-3 rounded-xl text-xs bg-rose-50 text-rose-800 border border-rose-300 font-bold whitespace-pre-line';
    el.textContent = '❌ فشل الاتصال بالخادم: ' + err.message;
  } finally {
    btn.disabled = false; btn.innerText = '🧪 فحص البروكسي';
  }
}

async function diagnoseProxy(noProxy) {
  const el = document.getElementById('diag-result');
  const btn = noProxy ? document.getElementById('btn-diagnose-direct') : document.getElementById('btn-diagnose');
  const label = noProxy ? '🚫 تشخيص بدون بروكسي' : '🩺 تشخيص السحب';
  btn.disabled = true; btn.innerText = '⏳ جارٍ التشخيص…';
  el.classList.remove('hidden');
  el.className = 'p-3 rounded-xl text-xs leading-relaxed font-bold whitespace-pre-line bg-slate-50 text-slate-700 border border-slate-200';
  el.textContent = noProxy ? '… يتم فحص الاتصال المباشر بمكتبة الإعلانات' : '… يتم فحص البروكسي والاتصال بمكتبة الإعلانات';
  try {
    const r = await fetch('/api/diagnose' + (noProxy ? '?proxy=0' : ''));
    const d = await r.json();
    const lines = [
      'المصدر: ' + (d.source || '-'),
      'البروكسي: ' + (d.proxy || 'بدون'),
      'IP الخروج: ' + (d.egress_ip || '-'),
      'IP المباشر للسيرفر: ' + (d.direct_ip || '-'),
      (d.proxy_changes_ip === false && d.egress_ip) ? '⚠️ البروكسي لا يغيّر عنوان الخروج.' : '',
      'نتائج Meta (استعلام تجريبي): ' + (d.meta_count == null ? '—' : d.meta_count),
      d.meta_error ? 'خطأ Meta: ' + d.meta_error : '',
      '',
      d.verdict || ''
    ].filter(x => x !== '');
    const ok = Number(d.meta_count) > 0;
    el.className = 'p-3 rounded-xl text-xs leading-relaxed font-bold whitespace-pre-line ' +
      (ok ? 'bg-emerald-50 text-emerald-800 border border-emerald-300'
          : 'bg-rose-50 text-rose-800 border border-rose-300');
    el.textContent = lines.join('\n');
  } catch (err) {
    el.className = 'p-3 rounded-xl text-xs bg-rose-50 text-rose-800 border border-rose-300 font-bold whitespace-pre-line';
    el.textContent = '❌ فشل التشخيص: ' + err.message;
  } finally {
    btn.disabled = false; btn.innerText = label;
  }
}

async function testTelegram() {
  const token = document.getElementById('cfg-tg-token').value.trim();
  const chat = document.getElementById('cfg-tg-chat').value.trim();
  const resEl = document.getElementById('tg-test-result');
  const btn = document.getElementById('btn-tg-test');

  if (!token || !chat) {
    alert('الرجاء إدخال كل من Bot Token و Chat ID أولاً.');
    return;
  }

  btn.disabled = true;
  btn.innerText = '⏳ جارٍ الإرسال…';
  resEl.classList.add('hidden');

  try {
    const r = await fetch('/api/telegram/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ telegram_token: token, telegram_chat_id: chat })
    });
    const d = await r.json();
    resEl.classList.remove('hidden');
    if (d.ok) {
      resEl.className = 'p-3 rounded-xl text-xs bg-emerald-50 text-emerald-800 border border-emerald-300 font-bold';
      resEl.textContent = '✅ ' + d.message;
    } else {
      resEl.className = 'p-3 rounded-xl text-xs bg-rose-50 text-rose-800 border border-rose-300 font-bold';
      resEl.textContent = '❌ ' + d.message;
    }
  } catch (err) {
    resEl.classList.remove('hidden');
    resEl.className = 'p-3 rounded-xl text-xs bg-rose-50 text-rose-800 border border-rose-300 font-bold';
    resEl.textContent = '❌ فشل الاتصال بالخادم: ' + err.message;
  } finally {
    btn.disabled = false;
    btn.innerText = '🧪 فحص وإرسال رسالة تجريبية';
  }
}

async function runScheduleNow() {
  const r = await fetch('/api/schedule/run-now', { method: 'POST' });
  const d = await r.json();
  if (d.ok) alert(`⚡ تم إرسال ${d.enqueued} مهمة فحص دوري تزايدي للمحرك.`);
}

