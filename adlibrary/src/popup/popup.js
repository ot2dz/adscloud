"use strict";

const ADRA_STORAGE_KEY = "adra_saved_ads";
const ADRA_TRACKED_PAGES_KEY = "adra_tracked_pages";
const ADRA_LANDING_FILTER_KEY = "adra_landing_url_filter";
const ADRA_MIN_PAGE_LIKES_KEY = "adra_min_page_likes";
const ADRA_CLOUD_TOKEN_KEY = "adra_cloud_token";
const ADRA_CLOUD_EMAIL_KEY = "adra_cloud_email";
const ADRA_CLOUD_STATUS_KEY = "adra_cloud_status";
const adraCount = document.querySelector("#adra-saved-count");
const adraTrackedCount = document.querySelector("#adra-tracked-count");
const adraStatus = document.querySelector("#adra-popup-status");

function adraReadAds() {
  return new Promise((resolve, reject) => {
    chrome.storage.local.get(ADRA_STORAGE_KEY, (result) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      resolve(Array.isArray(result[ADRA_STORAGE_KEY]) ? result[ADRA_STORAGE_KEY] : []);
    });
  });
}

function adraReadTrackedPages() {
  return new Promise((resolve, reject) => {
    chrome.storage.local.get(ADRA_TRACKED_PAGES_KEY, (result) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      resolve(Array.isArray(result[ADRA_TRACKED_PAGES_KEY]) ? result[ADRA_TRACKED_PAGES_KEY] : []);
    });
  });
}

async function adraRefreshCount() {
  try {
    adraCount.textContent = String((await adraReadAds()).length);
    adraTrackedCount.textContent = String((await adraReadTrackedPages()).length);
  } catch (_error) {
    adraStatus.textContent = "تعذر قراءة الإعلانات المحفوظة";
  }
}

document.querySelector("#adra-view-saved").addEventListener("click", () => {
  window.open(chrome.runtime.getURL("src/saved/saved.html"), "_blank", "noopener,noreferrer");
});

document.querySelector("#adra-view-tracked").addEventListener("click", () => {
  window.open(chrome.runtime.getURL("src/tracked/tracked.html"), "_blank", "noopener,noreferrer");
});

document.querySelector("#adra-clear-saved").addEventListener("click", () => {
  if (!confirm("هل تريد حذف جميع الإعلانات المحفوظة؟")) return;
  chrome.storage.local.set({ [ADRA_STORAGE_KEY]: [] }, () => {
    if (chrome.runtime.lastError) {
      adraStatus.textContent = "تعذر مسح الإعلانات";
    } else {
      adraStatus.textContent = "تم المسح";
      adraRefreshCount();
    }
  });
});

function adraLoadUrlFilter() {
  chrome.storage.local.get(ADRA_LANDING_FILTER_KEY, (result) => {
    if (chrome.runtime.lastError) return;
    const settings = result[ADRA_LANDING_FILTER_KEY] || {};
    document.querySelector("#adra-url-filter-text").value = settings.text || "";
    document.querySelector("#adra-url-filter-case").checked = Boolean(settings.caseSensitive);
  });
}

function adraLoadMinPageLikes() {
  chrome.storage.local.get(ADRA_MIN_PAGE_LIKES_KEY, (result) => {
    if (chrome.runtime.lastError) return;
    const raw = result[ADRA_MIN_PAGE_LIKES_KEY];
    const value = Number.isFinite(Number(raw)) && Number(raw) >= 0 ? Math.floor(Number(raw)) : 0;
    document.querySelector("#adra-min-page-likes").value = value ? String(value) : "";
  });
}

document.querySelector("#adra-save-url-filter").addEventListener("click", () => {
  const settings = {
    text: document.querySelector("#adra-url-filter-text").value.trim(),
    caseSensitive: document.querySelector("#adra-url-filter-case").checked
  };
  chrome.storage.local.set({ [ADRA_LANDING_FILTER_KEY]: settings }, () => {
    if (chrome.runtime.lastError) {
      adraStatus.textContent = "تعذر حفظ الفلتر";
      return;
    }
    adraStatus.textContent = settings.text ? "تم حفظ الفلتر" : "تم مسح الفلتر";
  });
});

document.querySelector("#adra-save-min-page-likes").addEventListener("click", () => {
  const raw = document.querySelector("#adra-min-page-likes").value;
  const value = Number.isFinite(Number(raw)) && Number(raw) >= 0 ? Math.floor(Number(raw)) : 0;
  chrome.storage.local.set({ [ADRA_MIN_PAGE_LIKES_KEY]: value }, () => {
    if (chrome.runtime.lastError) {
      adraStatus.textContent = "تعذر حفظ الفلتر";
      return;
    }
    adraStatus.textContent = value ? "تم حفظ الفلتر" : "تم مسح الفلتر";
  });
});

// --- ADRA Cloud (fixed hosted server, no URL field) -------------------------

const adraCloudStatus = document.querySelector("#adra-cloud-status");
const adraLoginStatus = document.querySelector("#adra-cloud-login-status");
const adraAuthGate = document.querySelector("#adra-auth-gate");
const adraMainUi = document.querySelector("#adra-main-ui");
const adraBoot = document.querySelector("#adra-boot");
const adraAccountBlock = document.querySelector("#adra-cloud-account");
const adraLoginForm = document.querySelector("#adra-cloud-login-form");
const adraRegisterForm = document.querySelector("#adra-cloud-register-form");
const adraTabLogin = document.querySelector("#adra-tab-login");
const adraTabRegister = document.querySelector("#adra-tab-register");
const adraRegisterRule = document.querySelector("#adra-cloud-register-rule");

const ADRA_SERVER_HOST = (() => {
  try {
    return new URL(ADRA_DEFAULT_CLOUD_URL).host;
  } catch (_error) {
    return String(ADRA_DEFAULT_CLOUD_URL || "");
  }
})();

const ADRA_CLOUD_ERRORS = {
  STORAGE_ERROR: "تعذر الوصول إلى ذاكرة الإضافة",
  NETWORK_ERROR: "تعذر الاتصال بالخادم",
  TIMEOUT: "انتهت مهلة الاتصال بالخادم",
  INVALID_JSON: "استجابة غير صالحة من الخادم",
  MISSING_CREDENTIALS: "أدخل البريد الإلكتروني وكلمة المرور",
  INVALID_URL: "عنوان الخادم غير صالح",
  INTERNAL: "خطأ داخلي، حاول مجدداً"
};

let adraRegistrationOpen = true;
let adraMinPasswordLen = Number(ADRA_MIN_PASSWORD_LEN) >= 1
  ? Math.floor(Number(ADRA_MIN_PASSWORD_LEN))
  : 10;
let adraSignedIn = false;

document.querySelector("#adra-cloud-server").textContent = ADRA_SERVER_HOST;

function adraErrorText(response) {
  if (!response) return ADRA_CLOUD_ERRORS.NETWORK_ERROR;
  const raw = String(response.error || response.reason || "").trim();
  if (!raw) return "تعذر إتمام العملية";
  if (ADRA_CLOUD_ERRORS[raw]) return ADRA_CLOUD_ERRORS[raw];
  const detail = raw.includes(":") ? raw.slice(raw.indexOf(":") + 1).trim() : "";
  return detail || raw;
}

function adraRenderCloudStatus(connection, detail = "") {
  adraCloudStatus.classList.remove("adra-cloud-ok", "adra-cloud-error");
  if (connection === "connected") {
    adraCloudStatus.textContent = `الحالة: متصل بـ ${ADRA_SERVER_HOST} 🟢`;
    adraCloudStatus.classList.add("adra-cloud-ok");
  } else if (connection === "error") {
    const suffix = detail ? ` (${detail})` : "";
    adraCloudStatus.textContent = `الحالة: تعذر الاتصال بـ ${ADRA_SERVER_HOST}${suffix}`;
    adraCloudStatus.classList.add("adra-cloud-error");
  } else {
    adraCloudStatus.textContent = "الحالة: جارٍ الاتصال…";
  }
}

function adraRenderAccount(email) {
  adraSignedIn = Boolean(email);
  if (adraBoot) adraBoot.hidden = true;
  adraAuthGate.hidden = adraSignedIn;
  adraMainUi.hidden = !adraSignedIn;
  adraAccountBlock.hidden = !adraSignedIn;
  document.querySelector("#adra-cloud-account-email").textContent = email || "";
}

function adraShowAuthTab(tab) {
  const register = tab === "register" && adraRegistrationOpen;
  adraLoginForm.hidden = register;
  adraRegisterForm.hidden = !register;
  adraTabLogin.classList.toggle("is-active", !register);
  adraTabRegister.classList.toggle("is-active", register);
}

function adraTestConnection(silent) {
  if (!silent) {
    adraCloudStatus.classList.remove("adra-cloud-ok", "adra-cloud-error");
    adraCloudStatus.textContent = "الحالة: جارٍ الاختبار…";
  }
  chrome.runtime.sendMessage({ type: "ADRA_CLOUD_TEST_CONNECTION" }, (response) => {
    const connected = Boolean(
      !chrome.runtime.lastError &&
      response &&
      response.ok &&
      response.data &&
      response.data.ok === true &&
      response.data.service === "adra-cloud"
    );
    chrome.storage.local.set(
      { [ADRA_CLOUD_STATUS_KEY]: connected ? "connected" : "error" },
      () => void chrome.runtime.lastError
    );
    const detail = response && !response.ok ? adraErrorText(response) : "";
    adraRenderCloudStatus(connected ? "connected" : "error", connected ? "" : detail);
  });
}

function adraLoadCloudSettings() {
  chrome.storage.local.get(
    [ADRA_CLOUD_TOKEN_KEY, ADRA_CLOUD_EMAIL_KEY, ADRA_CLOUD_STATUS_KEY],
    (result) => {
      if (chrome.runtime.lastError) {
        adraRenderAccount("");
        return;
      }
      const signedIn = Boolean(String(result[ADRA_CLOUD_TOKEN_KEY] || "").trim());
      adraRenderAccount(signedIn ? String(result[ADRA_CLOUD_EMAIL_KEY] || "") : "");
      const status = String(result[ADRA_CLOUD_STATUS_KEY] || "");
      if (status === "connected") adraRenderCloudStatus("connected");
      else if (status === "error") adraRenderCloudStatus("error");
      else adraRenderCloudStatus("");
    }
  );
}

function adraLoadAuthConfig() {
  chrome.runtime.sendMessage({ type: "ADRA_CLOUD_AUTH_CONFIG" }, (response) => {
    if (chrome.runtime.lastError || !response || !response.ok || !response.data) return;
    adraRegistrationOpen = Boolean(response.data.registration_open);
    const len = Number(response.data.min_password_length);
    if (Number.isFinite(len) && len >= 1) adraMinPasswordLen = Math.floor(len);
    adraTabRegister.hidden = !adraRegistrationOpen;
    if (!adraRegistrationOpen) adraShowAuthTab("login");
    adraRegisterRule.textContent = `كلمة المرور: ${adraMinPasswordLen} أحرف على الأقل`;
  });
}

document.querySelector("#adra-test-cloud").addEventListener("click", () => adraTestConnection(false));

adraTabLogin.addEventListener("click", () => adraShowAuthTab("login"));
adraTabRegister.addEventListener("click", () => adraShowAuthTab("register"));

document.querySelector("#adra-cloud-login").addEventListener("click", () => {
  const email = document.querySelector("#adra-cloud-email").value.trim();
  const password = document.querySelector("#adra-cloud-password").value;
  if (!email || !password) {
    adraLoginStatus.textContent = ADRA_CLOUD_ERRORS.MISSING_CREDENTIALS;
    return;
  }
  const button = document.querySelector("#adra-cloud-login");
  button.disabled = true;
  adraLoginStatus.textContent = "جارٍ تسجيل الدخول…";
  chrome.runtime.sendMessage({ type: "ADRA_CLOUD_LOGIN", email, password }, (response) => {
    button.disabled = false;
    if (chrome.runtime.lastError || !response) {
      adraLoginStatus.textContent = ADRA_CLOUD_ERRORS.NETWORK_ERROR;
      return;
    }
    if (!response.ok) {
      adraLoginStatus.textContent = adraErrorText(response);
      return;
    }
    document.querySelector("#adra-cloud-password").value = "";
    adraLoginStatus.textContent = "تم تسجيل الدخول";
    adraRenderAccount(response.data.email);
    adraTestConnection(true);
  });
});

document.querySelector("#adra-cloud-register").addEventListener("click", () => {
  const email = document.querySelector("#adra-cloud-register-email").value.trim();
  const password = document.querySelector("#adra-cloud-register-password").value;
  const confirm = document.querySelector("#adra-cloud-register-confirm").value;
  if (!email || !password) {
    adraLoginStatus.textContent = ADRA_CLOUD_ERRORS.MISSING_CREDENTIALS;
    return;
  }
  if (password.length < adraMinPasswordLen) {
    adraLoginStatus.textContent = `كلمة المرور يجب أن لا تقل عن ${adraMinPasswordLen} أحرف`;
    return;
  }
  if (password !== confirm) {
    adraLoginStatus.textContent = "كلمتا المرور غير متطابقتين";
    return;
  }
  const button = document.querySelector("#adra-cloud-register");
  button.disabled = true;
  adraLoginStatus.textContent = "جارٍ إنشاء الحساب…";
  chrome.runtime.sendMessage(
    { type: "ADRA_CLOUD_REGISTER", email, password, passwordConfirm: confirm },
    (response) => {
      button.disabled = false;
      if (chrome.runtime.lastError || !response) {
        adraLoginStatus.textContent = ADRA_CLOUD_ERRORS.NETWORK_ERROR;
        return;
      }
      if (!response.ok) {
        adraLoginStatus.textContent = adraErrorText(response);
        return;
      }
      document.querySelector("#adra-cloud-register-password").value = "";
      document.querySelector("#adra-cloud-register-confirm").value = "";
      adraLoginStatus.textContent = "تم إنشاء الحساب وتسجيل الدخول";
      adraRenderAccount(response.data.email);
      adraTestConnection(true);
    }
  );
});

document.querySelector("#adra-cloud-logout").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "ADRA_CLOUD_LOGOUT" }, () => {
    void chrome.runtime.lastError;
    adraLoginStatus.textContent = "تم تسجيل الخروج";
    adraRenderAccount("");
    adraShowAuthTab("login");
  });
});

// --- Historical Sync ---
const ADRA_SYNC_AT_KEY = "adra_cloud_last_historical_sync_at";
const ADRA_SYNC_RESULT_KEY = "adra_cloud_last_historical_sync_result";
const adraSyncButton = document.querySelector("#adra-sync-history");
const adraSyncProgress = document.querySelector("#adra-sync-progress");
const adraSyncLast = document.querySelector("#adra-sync-last");

function adraFormatSyncTime(iso) {
  const parsed = Date.parse(String(iso || ""));
  if (!Number.isFinite(parsed)) return "";
  try {
    return new Date(parsed).toLocaleString("ar-EG");
  } catch (_error) {
    return String(iso);
  }
}

function adraLoadLastSync() {
  chrome.storage.local.get([ADRA_SYNC_AT_KEY, ADRA_SYNC_RESULT_KEY], (result) => {
    if (chrome.runtime.lastError) return;
    const at = String(result[ADRA_SYNC_AT_KEY] || "");
    if (!at) {
      adraSyncLast.textContent = "";
      return;
    }
    adraSyncLast.textContent = `آخر مزامنة: ${adraFormatSyncTime(at) || at}`;
  });
}

chrome.runtime.onMessage.addListener((message) => {
  if (message && message.type === "ADRA_CLOUD_SYNC_PROGRESS" && message.progress) {
    adraSyncProgress.textContent = String(message.progress.text || "");
  }
});

adraSyncButton.addEventListener("click", () => {
  if (!adraSignedIn) {
    adraSyncProgress.textContent = "سجّل الدخول أولًا لتتم المزامنة";
    return;
  }
  adraSyncButton.disabled = true;
  adraSyncProgress.textContent = "جارٍ المزامنة…";
  chrome.runtime.sendMessage({ type: "ADRA_CLOUD_SYNC_HISTORY" }, (response) => {
    adraSyncButton.disabled = false;
    if (chrome.runtime.lastError || !response) {
      adraSyncProgress.textContent = "فشلت المزامنة";
      return;
    }
    if (response.skipped) {
      adraSyncProgress.textContent = "تعذر الاتصال بالخادم";
      return;
    }
    const text = response.data && response.data.text ? String(response.data.text) : "";
    adraSyncProgress.textContent =
      text || (response.ok ? "اكتملت المزامنة" : "فشلت المزامنة");
    adraLoadLastSync();
  });
});

adraRefreshCount();
adraLoadUrlFilter();
adraLoadMinPageLikes();
adraLoadCloudSettings();
adraLoadLastSync();
adraLoadAuthConfig();
adraTestConnection(true);
