let currentUser = null;
let currentAuthMode = 'login';
let ads = [];
let currentPreset = '';
let currentTargetFilter = null; // { id, type, value, label }
let currentKeywords = [];       // selected scrape-keyword tags (OR filter)
let currentNiches = [];         // selected niche slugs (OR filter)
let adCache = {};               // ad_id -> full ad object (for the details modal)
let lastAdBody = '';            // text of the ad currently shown in the modal
let kwMenuItems = [];           // keyword dropdown items
let nicheMenuItems = [];        // niche dropdown items

// Restore filters from the URL (?keywords=...&niche=...) so a view is shareable.
(function restoreFiltersFromUrl() {
  try {
    const p = new URLSearchParams(location.search);
    const kw = p.get('keywords');
    const ni = p.get('niche');
    if (kw) currentKeywords = kw.split(',').map(x => x.trim()).filter(Boolean);
    if (ni) currentNiches = ni.split(',').map(x => x.trim()).filter(Boolean);
  } catch (_e) {}
})();

function syncFilterUrl() {
  try {
    const p = new URLSearchParams(location.search);
    if (currentKeywords.length) p.set('keywords', currentKeywords.join(',')); else p.delete('keywords');
    if (currentNiches.length) p.set('niche', currentNiches.join(',')); else p.delete('niche');
    const qs = p.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  } catch (_e) {}
}

function toggleMs(id, force) {
  const panel = document.getElementById(id + '-panel');
  if (!panel) return;
  const other = id === 'kw' ? 'niche' : 'kw';
  const otherPanel = document.getElementById(other + '-panel');
  if (otherPanel) otherPanel.classList.add('hidden');
  const show = force === undefined ? panel.classList.contains('hidden') : Boolean(force);
  panel.classList.toggle('hidden', !show);
  if (show && id === 'kw') setTimeout(() => { const s = document.getElementById('kw-search'); if (s) s.focus(); }, 40);
}
document.addEventListener('click', (e) => {
  if (!e.target.closest('.ms-wrap')) {
    const a = document.getElementById('kw-panel'); if (a) a.classList.add('hidden');
    const b = document.getElementById('niche-panel'); if (b) b.classList.add('hidden');
  }
});
let debounceTimer = null;

// ── Auth Manager ──
async function checkAuth() {
  try {
    const res = await fetch('/api/auth/me');
    const data = await res.json();
    if (data.authenticated && data.user) {
      currentUser = data.user;
      renderAuthUser();
      hideAuthModal();
      loadDashboardData();
    } else {
      currentUser = null;
      showAuthModal();
    }
  } catch (err) {
    currentUser = null;
    showAuthModal();
  }
}

function isAdmin() {
  return Boolean(currentUser && currentUser.role === 'admin');
}

function loadDashboardData() {
  loadStats();
  loadAds();
  loadKeywordChips();
  loadNicheChips();
  loadServerLogs();
  loadWinningKeys();
  if (isAdmin()) {
    loadTargets();
    loadJobs();
    loadAdminUserFilter();
    startEventStream();
  }
  // Deep link from Telegram alerts: /?tab=winning
  const tab = new URLSearchParams(location.search).get('tab');
  showTab(tab || 'dashboard');
}

// Admin: whose data to show ('' = everyone). Regular users are always scoped server-side.
function adminUserParam(prefix) {
  const sel = document.getElementById('admin-user-filter');
  const v = isAdmin() && sel ? sel.value : '';
  return v ? `${prefix}user_id=${encodeURIComponent(v)}` : '';
}

async function loadAdminUserFilter() {
  const sel = document.getElementById('admin-user-filter');
  try {
    const res = await fetch('/api/admin/users');
    const data = await res.json();
    const current = sel.value;
    sel.innerHTML = '<option value="">👥 كل المستخدمين</option>' + (data.users || []).map(u =>
      `<option value="${u.id}">${escT(u.email)} (${u.saved_ads_count || 0} إعلان، ${u.tracked_pages_count || 0} صفحة)</option>`
    ).join('');
    sel.value = current;
  } catch (err) { /* the filter is optional */ }
}

function onAdminUserFilterChange() {
  loadStats();
  loadAds();
  if (!document.getElementById('view-targets').classList.contains('hidden')) loadTargets();
  loadStores();
}

function viewUserData(userId, tab) {
  const sel = document.getElementById('admin-user-filter');
  const value = String(userId);
  loadAdminUserFilter().then(() => {
    if (!Array.from(sel.options).some(o => o.value === value)) {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = `مستخدم #${value}`;
      sel.appendChild(opt);
    }
    sel.value = value;
    onAdminUserFilterChange();
    showTab(tab || 'ads');
  });
}

function showAuthModal() {
  document.getElementById('auth-modal').classList.remove('hidden');
  fetch('/api/auth/config').then(r => r.json()).then(cfg => {
    const reg = document.getElementById('tab-register-btn');
    reg.classList.toggle('hidden', !cfg.registration_open);
    if (!cfg.registration_open && currentAuthMode === 'register') setAuthMode('login');
  }).catch(() => {});
}

function hideAuthModal() {
  document.getElementById('auth-modal').classList.add('hidden');
}

function setAuthMode(mode) {
  currentAuthMode = mode;
  const loginBtn = document.getElementById('tab-login-btn');
  const regBtn = document.getElementById('tab-register-btn');
  const submitBtn = document.getElementById('auth-submit-btn');
  const subtitle = document.getElementById('auth-subtitle');
  const errBox = document.getElementById('auth-error-msg');
  errBox.classList.add('hidden');

  if (mode === 'login') {
    loginBtn.className = 'flex-1 py-2 rounded-lg bg-white text-blue-700 shadow-sm transition';
    regBtn.className = 'flex-1 py-2 rounded-lg text-slate-600 hover:text-slate-900 transition';
    submitBtn.innerText = 'دخول إلى المنصة';
    subtitle.innerText = 'تسجيل الدخول للوصول إلى لوحة التحكم وبنك الإعلانات';
  } else {
    regBtn.className = 'flex-1 py-2 rounded-lg bg-white text-blue-700 shadow-sm transition';
    loginBtn.className = 'flex-1 py-2 rounded-lg text-slate-600 hover:text-slate-900 transition';
    submitBtn.innerText = 'إنشاء حساب جديد';
    subtitle.innerText = 'أنشئ حسابك للبدء في استخدام أدوات التجسس والتحليل';
  }
}

async function handleAuthSubmit(e) {
  e.preventDefault();
  const email = document.getElementById('auth-email').value.trim();
  const password = document.getElementById('auth-password').value;
  const errBox = document.getElementById('auth-error-msg');
  const submitBtn = document.getElementById('auth-submit-btn');

  errBox.classList.add('hidden');
  submitBtn.disabled = true;
  submitBtn.innerText = 'جارٍ التحقق...';

  try {
    const endpoint = currentAuthMode === 'login' ? '/api/auth/login' : '/api/auth/register';
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });
    const data = await res.json();
    if (data.ok && data.user) {
      currentUser = data.user;
      renderAuthUser();
      hideAuthModal();
      loadDashboardData();
    } else {
      errBox.innerText = data.error || 'حدث خطأ في المصادقة';
      errBox.classList.remove('hidden');
    }
  } catch (err) {
    errBox.innerText = 'تعذر الاتصال بالخادم، يرجى المحاولة لاحقاً';
    errBox.classList.remove('hidden');
  } finally {
    submitBtn.disabled = false;
    submitBtn.innerText = currentAuthMode === 'login' ? 'دخول إلى المنصة' : 'إنشاء حساب جديد';
  }
}

async function logoutAll() {
  if (!confirm('سيتم إنهاء جلساتك على جميع الأجهزة والمتصفحات (بما فيها هذا) وفصل إضافات المتصفح المرتبطة بحسابك. متابعة؟')) return;
  await fetch('/api/auth/logout-all', { method: 'POST' });
  location.reload();
}

async function logout() {
  if (!confirm('هل تريد تسجيل الخروج؟')) return;
  await fetch('/api/auth/logout', { method: 'POST' });
  currentUser = null;
  location.reload();
}

function renderAuthUser() {
  if (!currentUser) return;
  const emailEl = document.getElementById('auth-user-email');
  const badgeEl = document.getElementById('auth-user-badge');
  const resetBtn = document.getElementById('btn-reset-db');

  emailEl.innerText = currentUser.email;
  const avatarEl = document.getElementById('auth-user-avatar');
  if (avatarEl) avatarEl.innerText = (currentUser.email || '?').trim().charAt(0).toUpperCase();
  const menuEmailEl = document.getElementById('auth-user-menu-email');
  if (menuEmailEl) menuEmailEl.innerText = currentUser.email;

  if (isAdmin()) {
    badgeEl.innerText = 'مدير النظام';
    badgeEl.className = 'block text-[10px] font-semibold text-amber-600';
    resetBtn.classList.remove('hidden');
  } else {
    badgeEl.innerText = 'حساب مستخدم';
    badgeEl.className = 'block text-[10px] font-semibold text-slate-400';
    resetBtn.classList.add('hidden');
  }
  const planTitle = document.getElementById('sidebar-plan-title');
  const planQuota = document.getElementById('sidebar-plan-quota');
  if (planTitle && planQuota) {
    planTitle.innerText = isAdmin() ? 'وضع المدير' : 'الخطة المجانية';
    planQuota.innerText = isAdmin() ? 'بلا حدود' : '1/5 متاجر';
  }
  applyAdminVisibility();
}

function applyAdminVisibility() {
  document.querySelectorAll('[data-admin-only]').forEach(el => el.classList.toggle('hidden', !isAdmin()));
}

// ── Admin Users Management ──
const SEC_EVENT_AR = {
  login_success: '✅ دخول', login_failed: '❌ كلمة مرور خاطئة', login_blocked: '⛔ حظر مؤقت (محاولات كثيرة)',
  login_refused_disabled: '🚫 حساب معطل حاول الدخول', logout: 'خروج', logout_all: 'خروج من كل الأجهزة',
  register: '🆕 تسجيل', user_created_by_admin: '🆕 حساب أنشأه المدير', user_disabled: '🔒 تعطيل حساب',
  api_register: '🆕 تسجيل من الإضافة',
  user_enabled: '🔓 تفعيل حساب', role_changed: '👑 تغيير صلاحية', user_deleted: '🗑️ حذف حساب',
  user_signed_out_by_admin: '⏏️ إخراج مستخدم', api_token_created: '🔌 ربط إضافة', api_tokens_revoked: '🔌 فصل الإضافات',
  settings_changed: '⚙️ تعديل الإعدادات'
};
async function loadSecurityEvents() {
  const el = document.getElementById('security-events');
  try {
    const d = await (await fetch('/api/admin/security-events?limit=200')).json();
    el.innerHTML = (d.events || []).map(e => `
      <tr class="${/failed|blocked|refused/.test(e.event) ? 'bg-rose-50/50' : ''}">
        <td class="py-1.5 px-4 font-mono text-slate-500 whitespace-nowrap">${escT((e.created_at || '').slice(0, 19).replace('T', ' '))}</td>
        <td class="px-4 font-bold text-slate-700 whitespace-nowrap">${escT(SEC_EVENT_AR[e.event] || e.event)}</td>
        <td class="px-4" dir="ltr">${escT(e.email || '—')}</td>
        <td class="px-4 font-mono text-slate-500">${escT(e.ip || '')}</td>
        <td class="px-4 text-slate-500">${escT(e.detail || '')}</td>
      </tr>`).join('') || '<tr><td colspan="5" class="py-6 text-center text-slate-400">لا أحداث بعد.</td></tr>';
  } catch (_e) { el.innerHTML = '<tr><td colspan="5" class="py-6 text-center text-rose-600">تعذر التحميل</td></tr>'; }
}
async function adminCreateUser() {
  const email = document.getElementById('new-user-email').value.trim();
  const password = document.getElementById('new-user-password').value;
  const role = document.getElementById('new-user-role').value;
  const r = await fetch('/api/admin/users', { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ email, password, role }) });
  const d = await r.json();
  if (!d.ok) return alert(d.error || 'فشل إنشاء الحساب');
  document.getElementById('new-user-email').value = '';
  document.getElementById('new-user-password').value = '';
  alert('✅ تم إنشاء الحساب. أرسل بيانات الدخول للمستخدم بطريقة آمنة.');
  loadAdminUsers();
}
async function adminSignOutUser(id) {
  if (!confirm('إنهاء كل جلسات هذا المستخدم وفصل إضافات المتصفح لديه؟')) return;
  const d = await (await fetch(`/api/admin/users/${id}/sign-out`, { method: 'POST' })).json();
  if (d.ok) alert(`تم: ${d.sessions} جلسة و${d.tokens} إضافة.`);
  loadSecurityEvents();
}

async function loadAdminUsers() {
  if (!currentUser || currentUser.role !== 'admin') return;
  const el = document.getElementById('admin-users-list');
  try {
    const res = await fetch('/api/admin/users');
    const data = await res.json();
    const users = data.users || [];
    if (!users.length) {
      el.innerHTML = '<tr><td colspan="9" class="py-6 text-center text-slate-400">لا يوجد مستخدمون مسجلون.</td></tr>';
      return;
    }
    el.innerHTML = users.map(u => {
      const isSelf = u.id === currentUser.id;
      const roleBadge = u.role === 'admin'
        ? '<span class="px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200 text-xs font-bold">مدير النظام</span>'
        : '<span class="px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 text-xs font-semibold">مستخدم عادي</span>';
      const statusBadge = u.is_active
        ? '<span class="text-xs text-emerald-700 font-bold">🟢 مفعل</span>'
        : '<span class="text-xs text-rose-600 font-bold">🔴 معطل</span>';

      return `
      <tr class="hover:bg-slate-50 transition">
        <td class="py-3 px-5 text-slate-400 font-mono text-xs">${u.id}</td>
        <td class="px-5 font-bold text-[#102a56]">${escT(u.email)} ${isSelf ? '<span class="text-[10px] text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">(أنت)</span>' : ''} ${Number(u.extension_signup_count) > 0 ? '<span class="text-[10px] text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full font-bold">من الإضافة</span>' : ''}</td>
        <td class="px-5">${roleBadge}</td>
        <td class="px-5">${statusBadge}</td>
        <td class="px-5 num font-bold text-slate-800">${u.saved_ads_count || 0}</td>
        <td class="px-5 num font-bold text-slate-800">${u.tracked_pages_count || 0}</td>
        <td class="px-5 text-xs text-slate-500 font-mono">${(u.created_at || '').slice(0, 10) || '—'}</td>
        <td class="px-5 text-xs text-slate-500 font-mono">${(u.last_login_at || '').slice(0, 16).replace('T', ' ') || 'لم يدخل بعد'}</td>
        <td class="px-5 text-center">
          ${isSelf ? '<span class="text-xs text-slate-400 italic">حسابك الحالي</span>' : `
            <div class="flex items-center justify-center gap-2">
              <button onclick="viewUserData(${u.id}, 'ads')" title="عرض الإعلانات التي حفظها هذا المستخدم" class="text-xs px-2.5 py-1 rounded-lg bg-blue-50 text-blue-700 border border-blue-200 hover:bg-blue-100 font-bold transition">
                إعلاناته
              </button>
              <button onclick="viewUserData(${u.id}, 'targets')" title="عرض الصفحات/المنافسين الذين يتابعهم" class="text-xs px-2.5 py-1 rounded-lg bg-indigo-50 text-indigo-700 border border-indigo-200 hover:bg-indigo-100 font-bold transition">
                صفحاته
              </button>
              <button onclick="toggleUserStatus(${u.id})" class="text-xs px-2.5 py-1 rounded-lg border font-bold transition ${u.is_active ? 'bg-amber-50 text-amber-700 border-amber-200 hover:bg-amber-100' : 'bg-emerald-50 text-emerald-700 border-emerald-200 hover:bg-emerald-100'}">
                ${u.is_active ? 'تجميد' : 'تفعيل'}
              </button>
              <button onclick="toggleUserRole(${Number(u.id)}, '${u.role === 'admin' ? 'admin' : 'user'}')" class="text-xs px-2.5 py-1 rounded-lg bg-blue-50 text-blue-700 border border-blue-200 hover:bg-blue-100 font-bold transition">
                ${u.role === 'admin' ? 'تنزيل لمستخدم' : 'ترقية لأدمن'}
              </button>
              <button onclick="adminSignOutUser(${Number(u.id)})" title="إنهاء كل جلساته وفصل إضافاته" class="text-xs px-2.5 py-1 rounded-lg bg-white text-rose-700 border border-rose-200 hover:bg-rose-50 font-bold transition">
                تسجيل خروجه
              </button>
              <button onclick="deleteUser(${u.id})" class="text-xs px-2 py-1 rounded-lg bg-rose-50 text-rose-700 border border-rose-200 hover:bg-rose-100 font-bold transition">
                حذف
              </button>
            </div>
          `}
        </td>
      </tr>`;
    }).join('');
  } catch (err) {
    el.innerHTML = '<tr><td colspan="9" class="py-6 text-center text-rose-600 font-bold">فشل تحميل المستخدمين</td></tr>';
  }
}

async function toggleUserStatus(userId) {
  const res = await fetch(`/api/admin/users/${userId}/toggle-status`, { method: 'POST' });
  const data = await res.json();
  if (!data.ok) return alert(data.error || 'فشلت العملية');
  loadAdminUsers();
}

async function toggleUserRole(userId, currentRole) {
  const newRole = currentRole === 'admin' ? 'user' : 'admin';
  const res = await fetch(`/api/admin/users/${userId}/role`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role: newRole })
  });
  const data = await res.json();
  if (!data.ok) return alert(data.error || 'فشلت العملية');
  loadAdminUsers();
}

async function deleteUser(userId) {
  if (!confirm('هل أنت متأكد من حذف هذا المستخدم نهائياً؟')) return;
  const res = await fetch(`/api/admin/users/${userId}`, { method: 'DELETE' });
  const data = await res.json();
  if (!data.ok) return alert(data.error || 'فشلت العملية');
  loadAdminUsers();
}

async function loadStats() {
  const r = await fetch('/api/stats' + adminUserParam('?')); const s = await r.json();
  document.getElementById('stat-total').innerText = s.total_ads;
  document.getElementById('stat-stores').innerText = s.total_stores;
  document.getElementById('db-backend').innerText = s.backend;
}

function escT(s){ return String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
// ── Tab switcher ──
function showTab(t) {
  if (t === 'tracked') t = 'targets';  // old tab merged into competitors
  if (['jobs', 'settings', 'users'].includes(t) && !isAdmin()) t = 'dashboard';
  const views = ['dashboard','ads','targets','winning','sales','stores','analysis','global','jobs','settings','users'];
  views.forEach(v => {
    const el = document.getElementById('view-'+v);
    if (el) el.classList.toggle('hidden', t!==v);
  });
  document.querySelectorAll('[data-nav]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.nav === t);
  });
  applyAdminVisibility();
  toggleSidebar(false);
  closeUserMenu();
  window.scrollTo({ top: 0, behavior: 'instant' });
  if (t === 'dashboard') loadDashboard();
  if (t === 'ads') { loadKeywordChips(); loadNicheChips(); }
  if (t === 'targets') loadTargets();
  if (t === 'winning') loadWinning();
  if (t === 'sales') loadSales();
  if (t === 'stores') loadStores();
  if (t === 'analysis') loadAnalysis();
  if (t === 'global') loadGlobal();
  if (t === 'jobs') loadJobs();
  if (t === 'settings') loadSettings();
  if (t === 'users') { loadAdminUsers(); loadSecurityEvents(); loadAdminUserFilter(); }
}


// ---- egress IP badge + manual rotate button ----
(function egressIpBadge() {
  const badge = document.getElementById('egress-ip-badge');
  const value = document.getElementById('egress-ip');
  const btn = document.getElementById('egress-rotate');
  const checkBadge = document.getElementById('egress-check-badge');
  const checkValue = document.getElementById('egress-check-ip');
  if (!badge || !value) return;

  // The two rotators report differently: the home agent `ip`, the tablet `new_ip`.
  function rotatorInfo(r) {
    if (!r) return '';
    const ip = r.ip || r.new_ip;
    return ' | rotator: ' + (r.status || '') + (ip ? ' ' + ip : '');
  }

  function paintCheck(c) {
    if (!checkBadge || !checkValue) return;
    if (!c) { checkBadge.classList.add('hidden'); return; }
    checkValue.textContent = c.ip || '--';
    checkBadge.title = (c.proxy ? 'proxy: ' + c.proxy : '') + rotatorInfo(c.rotator)
      + (c.error ? ' | ' + c.error : '');
    checkBadge.classList.remove('hidden');
  }

  function paint(j) {
    paintCheck(j && j.status_check);
    if (!j || !j.ok || !j.ip) { badge.classList.add('hidden'); return; }
    value.textContent = j.ip;
    badge.title = 'source: ' + (j.source || '?') + (j.proxy ? ' | proxy: ' + j.proxy : '')
      + rotatorInfo(j.rotator);
    badge.classList.remove('hidden');
  }

  async function load() {
    try {
      const r = await fetch('/api/egress-ip', { credentials: 'same-origin' });
      if (!r.ok) { badge.classList.add('hidden'); return; }
      paint(await r.json());
    } catch (_e) { badge.classList.add('hidden'); }
  }

  async function rotate() {
    if (!btn || btn.disabled) return;
    const before = value.textContent;
    btn.disabled = true;
    btn.classList.add('animate-spin');
    try {
      const r = await fetch('/api/egress-ip/rotate', { method: 'POST', credentials: 'same-origin' });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) {
        const rr = (j && (j.error || (j.result && j.result.error))) || 'rotate failed';
        btn.title = String(rr);
      }
      for (let i = 0; i < 20; i++) {
        await new Promise((res) => setTimeout(res, 2000));
        try {
          const q = await fetch('/api/egress-ip', { credentials: 'same-origin' });
          if (!q.ok) continue;
          const jj = await q.json();
          paint(jj);
          if (jj && jj.ip && jj.ip !== before) break;
        } catch (_e) { }
      }
    } catch (_e) {
    } finally {
      btn.disabled = false;
      btn.classList.remove('animate-spin');
    }
  }

  if (btn) btn.addEventListener('click', rotate);
  load();
  setInterval(load, 60000);
})();

// Live refresh of the admin users table (new extension signups appear on their
// own while the tab is open).
setInterval(() => {
  const view = document.getElementById('view-users');
  if (isAdmin() && view && !view.classList.contains('hidden')) loadAdminUsers();
}, 20000);

// ── App shell (sidebar, topbar search, dashboard) ──
function toggleSidebar(open) {
  const sb = document.getElementById('app-sidebar');
  const bd = document.getElementById('sidebar-backdrop');
  if (!sb) return;
  sb.classList.toggle('open', !!open);
  if (bd) bd.classList.toggle('hidden', !open);
}

function toggleUserMenu(e) {
  if (e) e.stopPropagation();
  const m = document.getElementById('user-menu');
  if (m) m.classList.toggle('hidden');
}

function closeUserMenu() {
  const m = document.getElementById('user-menu');
  if (m) m.classList.add('hidden');
}

document.addEventListener('click', (e) => {
  if (e.target.closest('#user-menu') || e.target.closest('[data-user-menu-btn]')) return;
  closeUserMenu();
});

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    document.getElementById('topbar-search')?.focus();
  }
  if (e.key === 'Escape') { closeUserMenu(); toggleSidebar(false); }
});

function topbarSearch() {
  const v = (document.getElementById('topbar-search')?.value || '').trim();
  if (!v) { showTab('ads'); return; }
  quickAdSearch(v);
}

function quickAdSearch(term) {
  showTab('ads');
  const s = document.getElementById('search');
  if (s) s.value = term;
  if (typeof loadAds === 'function') loadAds();
}

function dismissOnboarding() {
  try { localStorage.setItem('ac_onboard_hidden', '1'); } catch (_e) {}
  document.getElementById('onboarding-card')?.classList.add('hidden');
}

function fmtNum(v) { return Number(v || 0).toLocaleString('en-US'); }

async function loadDashboard() {
  try {
    if (localStorage.getItem('ac_onboard_hidden') === '1') {
      document.getElementById('onboarding-card')?.classList.add('hidden');
    }
  } catch (_e) {}
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  const [st, sa, wi] = await Promise.all([
    fetch('/api/stats', { credentials: 'same-origin' }).then(r => r.json()).catch(() => null),
    fetch('/api/sales/overview?days=7', { credentials: 'same-origin' }).then(r => r.json()).catch(() => null),
    fetch('/api/winning', { credentials: 'same-origin' }).then(r => r.json()).catch(() => null),
  ]);

  if (st && st.total_ads != null) {
    set('dash-ads', fmtNum(st.total_ads));
    set('sidebar-ads-count', fmtNum(st.total_ads));
  }

  const stores = (sa && sa.stores) || [];
  const measuredToday = stores.filter(s => s.today_measured);
  const today = measuredToday.reduce((a, s) => a + (s.orders_today || 0), 0);
  set('dash-sales-today', measuredToday.length ? fmtNum(today) : '—');
  const measuredY = stores.filter(s => s.yesterday_measured);
  const yest = measuredY.reduce((a, s) => a + (s.orders_yesterday || 0), 0);
  set('dash-sales-yesterday', measuredY.length ? fmtNum(yest) : '—');
  set('dash-sales-week', stores.length ? fmtNum(stores.reduce((a, s) => a + (s.orders_window || 0), 0)) : '—');
  set('dash-stores', fmtNum(stores.length));

  const note = document.getElementById('dash-sales-today-note');
  if (note) {
    if (measuredToday.length && measuredY.length && yest > 0) {
      const pct = Math.round((today - yest) / yest * 100);
      note.textContent = (pct >= 0 ? '+' : '') + pct + '%';
      note.className = 'num ' + (pct >= 0 ? 'text-emerald-600' : 'text-rose-600');
    } else {
      note.textContent = '—';
      note.className = 'num';
    }
  }

  const products = (wi && wi.products) || [];
  set('dash-winning', fmtNum(products.length));
  set('sidebar-winning-count', fmtNum(products.length));
  const list = document.getElementById('dash-winning-list');
  if (list) {
    list.innerHTML = products.slice(0, 4).map(p => `
      <div class="flex items-center justify-between gap-2 border-b border-slate-100 py-2 last:border-0">
        <div class="min-w-0">
          <div class="font-bold text-slate-800 truncate">${escT(p.page_name || p.product_key || 'منتج')}</div>
          <div class="text-[11px] text-slate-400 font-mono truncate" dir="ltr" style="text-align:right">${escT(p.store_domain || '')}</div>
        </div>
        <span class="pill shrink-0">${escT(p.stage || 'مراقبة')} · ${Number(p.active_ads || 0)}</span>
      </div>`).join('') ||
      '<div class="text-center text-slate-400 font-semibold py-4">لا توجد منتجات محفوظة بعد — احفظ منتجاً بنجمة من بطاقة الإعلان.</div>';
  }
}
