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
  if (isAdmin()) {
    loadTargets();
    loadJobs();
    loadAdminUserFilter();
    startEventStream();
  }
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

function viewUserData(userId) {
  const sel = document.getElementById('admin-user-filter');
  sel.value = String(userId);
  onAdminUserFilterChange();
  showTab('ads');
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

  if (isAdmin()) {
    badgeEl.innerText = '👑 مدير النظام';
    badgeEl.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-bold bg-amber-100 text-amber-800 border border-amber-300';
    resetBtn.classList.remove('hidden');
  } else {
    badgeEl.innerText = '👤 مستخدم';
    badgeEl.className = 'text-[11px] px-2.5 py-0.5 rounded-full font-bold bg-blue-100 text-blue-800 border border-blue-300';
    resetBtn.classList.add('hidden');
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
        <td class="px-5 font-bold text-[#102a56]">${escT(u.email)} ${isSelf ? '<span class="text-[10px] text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">(أنت)</span>' : ''}</td>
        <td class="px-5">${roleBadge}</td>
        <td class="px-5">${statusBadge}</td>
        <td class="px-5 num font-bold text-slate-800">${u.saved_ads_count || 0}</td>
        <td class="px-5 num font-bold text-slate-800">${u.tracked_pages_count || 0}</td>
        <td class="px-5 text-xs text-slate-500 font-mono">${(u.created_at || '').slice(0, 10) || '—'}</td>
        <td class="px-5 text-xs text-slate-500 font-mono">${(u.last_login_at || '').slice(0, 16).replace('T', ' ') || 'لم يدخل بعد'}</td>
        <td class="px-5 text-center">
          ${isSelf ? '<span class="text-xs text-slate-400 italic">حسابك الحالي</span>' : `
            <div class="flex items-center justify-center gap-2">
              <button onclick="viewUserData(${u.id})" class="text-xs px-2.5 py-1 rounded-lg bg-slate-50 text-slate-700 border border-slate-200 hover:bg-slate-100 font-bold transition">
                عرض بياناته
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
  ['ads','targets','stores','jobs','settings','users'].forEach(v => {
    const el = document.getElementById('view-'+v);
    if (el) el.classList.toggle('hidden', t!==v);
  });
  ['ads','targets','stores','jobs','settings','users'].forEach(v => {
    const btn = document.getElementById('tab-'+v);
    if (btn) {
      if (t === v) {
        btn.className = 'px-5 py-2.5 rounded-xl bg-blue-600 text-sm font-bold text-white shadow-sm transition';
      } else {
        btn.className = 'px-5 py-2.5 rounded-xl bg-white border border-[#cfe0f5] text-slate-700 text-sm font-bold hover:bg-[#eef6ff] transition';
      }
      if (btn.hasAttribute('data-admin-only') && !isAdmin()) btn.classList.add('hidden');
    }
  });
  if (t === 'ads') { loadKeywordChips(); loadNicheChips(); }
  if (t === 'targets') loadTargets();
  if (t === 'stores') loadStores();
  if (t === 'jobs') loadJobs();
  if (t === 'settings') loadSettings();
  if (t === 'users') { loadAdminUsers(); loadSecurityEvents(); }
}

