// ── Settings: one section at a time, one save bar for every section ──
const SETTINGS_TABS = ['filters', 'auto', 'proxy', 'ai', 'alerts', 'accounts', 'backup'];
const settingsDirty = new Set();  // save groups with changes: general | ai | nai | lanes
let settingsWired = false;

function settingsTab(name) {
  if (!SETTINGS_TABS.includes(name)) name = 'filters';
  document.querySelectorAll('#view-settings [data-spanel]').forEach(p => p.classList.toggle('hidden', p.dataset.spanel !== name));
  document.querySelectorAll('#settings-nav [data-stab]').forEach(b => b.classList.toggle('active', b.dataset.stab === name));
  try { localStorage.setItem('settingsTab', name); } catch (e) { /* private mode */ }
}

const settingsDirtyPanels = new Set();  // sections the user edited (for the dot in the menu)

function settingsMarkDirty(group, panel) {
  settingsDirty.add(group);
  settingsDirtyPanels.add(panel || (group === 'lanes' ? 'proxy' : group === 'nai' ? 'ai' : ''));
  settingsRefreshDirty();
}

function settingsRefreshDirty() {
  if (!settingsDirty.size) settingsDirtyPanels.clear();
  const n = [...settingsDirtyPanels].filter(Boolean).length;
  document.querySelectorAll('#settings-nav [data-stab]').forEach(b => b.classList.toggle('dirty', settingsDirtyPanels.has(b.dataset.stab)));
  document.getElementById('settings-savebar').classList.toggle('hidden', !settingsDirty.size);
  document.getElementById('settings-savebar-text').textContent = n > 1
    ? `لديك تغييرات غير محفوظة في ${n} أقسام` : 'لديك تغييرات غير محفوظة';
}

function settingsWire() {
  if (settingsWired) return;
  settingsWired = true;
  const root = document.getElementById('view-settings');
  const onEdit = e => {
    const g = e.target.closest('[data-save]');
    if (g) settingsMarkDirty(g.dataset.save, g.closest('[data-spanel]')?.dataset.spanel);
  };
  root.addEventListener('input', onEdit);
  root.addEventListener('change', onEdit);
  window.addEventListener('beforeunload', e => { if (settingsDirty.size) { e.preventDefault(); e.returnValue = ''; } });
  let last = null;
  try { last = localStorage.getItem('settingsTab'); } catch (e) { /* private mode */ }
  settingsTab(last || 'filters');
}

function settingsBadge(id, state, title) {
  const el = document.getElementById('sb-' + id);
  if (el) { el.className = 'sbadge ' + state; el.title = title; }
}

function settingsBadges(s, ai) {
  const on = (k, def) => (s[k] ?? def) === 'true';
  const filters = [on('arabic_only', 'true'), on('cta_filter_enabled', 'true')].filter(Boolean).length;
  settingsBadge('filters', filters ? 'ok' : 'warn', filters ? 'الفلاتر مفعّلة' : 'كل الفلاتر مطفأة');
  const auto = [on('schedule_enabled', 'false'), on('month_rescan_enabled', 'true'), on('status_check_enabled', 'true'),
                on('page_expand_enabled', 'true')].filter(Boolean).length;
  settingsBadge('auto', auto === 4 ? 'ok' : auto ? 'warn' : 'off', `${auto} من 4 مهام مفعّلة`);
  const proxy = !!(s.meta_ads_proxy || s.env_meta_ads_proxy);
  settingsBadge('proxy', proxy ? 'ok' : 'warn', proxy ? 'يوجد بروكسي' : 'السحب بدون بروكسي');
  if (ai) settingsBadge('ai', ai.key_unreadable ? 'bad' : ai.key_set ? 'ok' : 'off',
    ai.key_unreadable ? 'المفتاح لا يُقرأ — أدخله من جديد' : ai.key_set ? 'المفتاح محفوظ' : 'لا يوجد مفتاح');
  const tg = on('notify_enabled', 'false') && s.telegram_token && s.telegram_chat_id;
  settingsBadge('alerts', tg ? 'ok' : 'off', tg ? 'التنبيهات تعمل' : 'التنبيهات معطّلة');
  const open = on('registration_open', 'false');
  settingsBadge('accounts', open ? 'warn' : 'ok', open ? 'التسجيل الذاتي مفتوح' : 'التسجيل مغلق');
}

async function loadSettings() {
  settingsWire();
  settingsDirty.clear();
  settingsRefreshDirty();
  const aiP = aiLoadConfig();
  if (typeof naiLoad === 'function' && isAdmin()) naiLoad();
  if (typeof lanesLoad === 'function' && isAdmin()) lanesLoad();
  const r = await fetch('/api/settings'); const s = await r.json();
  aiP.then(ai => settingsBadges(s, ai));
  settingsBadges(s);
  document.getElementById('cfg-sched-enabled').checked = s.schedule_enabled === 'true';
  document.getElementById('cfg-entity-stats').checked = (s.entity_stats_enabled || 'true') === 'true';
  document.getElementById('cfg-registration-open').checked = s.registration_open === 'true';
  document.getElementById('cfg-extension-registration-open').checked = (s.extension_registration_open ?? 'true') === 'true';
  document.getElementById('cfg-cta-filter').checked = (s.cta_filter_enabled || 'true') === 'true';
  document.getElementById('cfg-arabic-only').checked = (s.arabic_only || 'true') === 'true';
  document.getElementById('cfg-cta-allowlist').value = (s.cta_allowlist || '').split(',').join(', ');
  document.getElementById('cfg-month-rescan').checked = (s.month_rescan_enabled || 'true') === 'true';
  document.getElementById('cfg-month-rescan-hour').value = s.month_rescan_hour || 23;
  document.getElementById('cfg-status-check').checked = (s.status_check_enabled || 'true') === 'true';
  document.getElementById('cfg-status-check-hour').value = s.status_check_hour || 3;
  document.getElementById('cfg-status-check-interval').value = s.status_check_interval_days || 1;
  document.getElementById('cfg-status-check-proxy').value = s.status_check_proxy || '';
  document.getElementById('cfg-status-check-rotate-url').value = s.status_check_rotate_url || '';
  document.getElementById('status-check-summary').innerText = statusCheckSummary(s.status_check_summary);
  document.getElementById('month-rescan-last').innerText = s.month_rescan_last ? `(آخر تشغيل: ${s.month_rescan_last})` : '';
  document.getElementById('cfg-sched-interval').value = s.interval_hours || 12;
  document.getElementById('cfg-page-expand').checked = (s.page_expand_enabled || 'true') === 'true';
  document.getElementById('cfg-page-expand-daily').value = s.page_expand_daily || 300;
  document.getElementById('cfg-page-expand-gap').value = s.page_expand_min_gap || 3;
  document.getElementById('cfg-page-expand-max').value = s.page_expand_max_page ?? 300;
  document.getElementById('cfg-page-expand-cod').checked = (s.page_expand_cod_only || 'true') === 'true';
  loadPageExpandStatus();
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

async function saveGeneralSettings() {
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
      page_expand_enabled: document.getElementById('cfg-page-expand').checked,
      page_expand_daily: parseInt(document.getElementById('cfg-page-expand-daily').value || '300'),
      page_expand_min_gap: parseInt(document.getElementById('cfg-page-expand-gap').value || '3'),
      page_expand_max_page: parseInt(document.getElementById('cfg-page-expand-max').value || '0'),
      page_expand_cod_only: document.getElementById('cfg-page-expand-cod').checked,
      registration_open: document.getElementById('cfg-registration-open').checked,
      extension_registration_open: document.getElementById('cfg-extension-registration-open').checked,
      cta_filter_enabled: document.getElementById('cfg-cta-filter').checked,
      arabic_only: document.getElementById('cfg-arabic-only').checked,
      cta_allowlist: document.getElementById('cfg-cta-allowlist').value,
      month_rescan_enabled: document.getElementById('cfg-month-rescan').checked,
      month_rescan_hour: parseInt(document.getElementById('cfg-month-rescan-hour').value || '23'),
      status_check_enabled: document.getElementById('cfg-status-check').checked,
      status_check_hour: parseInt(document.getElementById('cfg-status-check-hour').value || '3'),
      status_check_interval_days: parseInt(document.getElementById('cfg-status-check-interval').value || '1'),
      status_check_proxy: document.getElementById('cfg-status-check-proxy').value.trim(),
      status_check_rotate_url: document.getElementById('cfg-status-check-rotate-url').value.trim(),
      notify_enabled: notifEn,
      telegram_token: token,
      telegram_chat_id: chat,
      meta_ads_proxy: document.getElementById('cfg-proxy').value.trim(),
      blocked_domains: (document.getElementById('cfg-blocked')?.value || '').trim()
    })
  });
  return r.json();
}

// Saves every section with changes, then reloads the page values once.
async function settingsSave(btn) {
  btn.disabled = true; btn.textContent = '⏳ جارٍ الحفظ…';
  const notes = [];
  let failed = false;
  try {
    if (settingsDirty.has('ai')) { if (await aiSave()) settingsDirty.delete('ai'); else { failed = true; notes.push('❌ فشل حفظ إعدادات الذكاء الاصطناعي (التفاصيل في قسمه).'); } }
    if (settingsDirty.has('nai')) { if (await naiSave()) settingsDirty.delete('nai'); else failed = true; }
    if (settingsDirty.has('lanes')) { if (await lanesSave()) settingsDirty.delete('lanes'); else failed = true; }
    if (settingsDirty.has('general')) {
      const d = await saveGeneralSettings();
      if (d.ok) {
        settingsDirty.delete('general');
        if (d.purged != null) notes.push(`🧹 حُذف ${d.purged} إعلانًا لشركات كبرى من القاعدة.`);
        loadAds(); loadStats();  // CTA filter etc. apply immediately
      } else { failed = true; notes.push('❌ ' + (d.error || 'فشل حفظ الإعدادات العامة')); }
    }
  } catch (err) {
    failed = true; notes.push('❌ ' + err.message);
  } finally {
    btn.disabled = false; btn.textContent = 'حفظ';
  }
  if (!failed) { await loadSettings(); btn.textContent = '✅ تم الحفظ'; setTimeout(() => { btn.textContent = 'حفظ'; }, 1500); }
  else settingsRefreshDirty();
  if (notes.length) alert(notes.join('\n'));
}

function settingsDiscard() {
  if (!confirm('تجاهل كل التغييرات غير المحفوظة؟')) return;
  loadSettings();
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

async function loadPageExpandStatus() {
  const el = document.getElementById('page-expand-summary');
  if (!el) return;
  try {
    const r = await fetch('/api/page-expand');
    if (!r.ok) return;
    const d = await r.json();
    const fmt = n => Number(n || 0).toLocaleString('en-US');
    el.textContent = `ينتظر التوسيع: ${fmt(d.pending_pages)} صفحة (~${fmt(d.missing_ads)} إعلان ناقص) · اليوم: ${fmt(d.done_today)}/${fmt(d.daily)} صفحة`
      + (d.last ? ` · آخر تشغيل: ${String(d.last).slice(0, 16).replace('T', ' ')}` : '');
  } catch (e) { /* offline */ }
}

async function runPageExpand(btn) {
  btn.disabled = true;
  try {
    const r = await fetch('/api/page-expand/run', {method: 'POST'});
    const d = await r.json();
    alert(d.ok ? (d.queued ? '🕒 أُضيف توسيع الصفحات إلى قائمة الانتظار.' : '🔁 بدأ توسيع الصفحات.') : '⚠️ ' + (d.error || 'تعذّر البدء'));
    if (typeof loadJobs === 'function') loadJobs();
  } finally { btn.disabled = false; }
}

async function runScheduleNow() {
  const r = await fetch('/api/schedule/run-now', { method: 'POST' });
  const d = await r.json();
  if (d.ok) alert(`⚡ تم إرسال ${d.enqueued} مهمة فحص دوري تزايدي للمحرك.`);
}

