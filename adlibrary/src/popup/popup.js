"use strict";

const ADRA_STORAGE_KEY = "adra_saved_ads";
const ADRA_TRACKED_PAGES_KEY = "adra_tracked_pages";
const ADRA_LANDING_FILTER_KEY = "adra_landing_url_filter";
const ADRA_MIN_PAGE_LIKES_KEY = "adra_min_page_likes";
const ADRA_CLOUD_URL_KEY = "adra_cloud_api_url";
const ADRA_CLOUD_TOKEN_KEY = "adra_cloud_token";
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

// --- ADRA Cloud settings ---

const adraCloudStatus = document.querySelector("#adra-cloud-status");

function adraRenderCloudStatus(url, connection, detail = "") {
  adraCloudStatus.classList.remove("adra-cloud-ok", "adra-cloud-error");
  if (url && connection === "connected") {
    adraCloudStatus.textContent = "الحالة: متصل بـ ADRA Cloud 🟢";
    adraCloudStatus.classList.add("adra-cloud-ok");
  } else if (url && connection === "error") {
    const errorText = detail ? ` (${detail})` : "";
    adraCloudStatus.textContent = `الحالة: تعذر الاتصال بـ ADRA Cloud${errorText}`;
    adraCloudStatus.classList.add("adra-cloud-error");
  } else {
    adraCloudStatus.textContent = "الحالة: غير مهيأ";
  }
}

function adraValidCloudUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch (_error) {
    return false;
  }
}

function adraLoadCloudSettings() {
  chrome.storage.local.get([ADRA_CLOUD_URL_KEY, ADRA_CLOUD_TOKEN_KEY, ADRA_CLOUD_STATUS_KEY], (result) => {
    if (chrome.runtime.lastError) return;
    const url = String(result[ADRA_CLOUD_URL_KEY] || "");
    document.querySelector("#adra-cloud-url").value = url;
    document.querySelector("#adra-cloud-token").value = String(result[ADRA_CLOUD_TOKEN_KEY] || "");
    adraRenderCloudStatus(url, String(result[ADRA_CLOUD_STATUS_KEY] || ""));
  });
}

document.querySelector("#adra-save-cloud-url").addEventListener("click", () => {
  const url = document.querySelector("#adra-cloud-url").value.trim();
  const token = document.querySelector("#adra-cloud-token").value.trim();
  if (url && !adraValidCloudUrl(url)) {
    adraCloudStatus.textContent = "الحالة: رابط غير صالح (http/https فقط)";
    adraCloudStatus.classList.add("adra-cloud-error");
    return;
  }
  chrome.storage.local.set({ [ADRA_CLOUD_URL_KEY]: url, [ADRA_CLOUD_TOKEN_KEY]: token, [ADRA_CLOUD_STATUS_KEY]: "" }, () => {
    if (chrome.runtime.lastError) {
      adraStatus.textContent = "تعذر حفظ إعداد Cloud";
      return;
    }
    adraStatus.textContent = "تم حفظ إعداد Cloud";
    adraRenderCloudStatus(url, "");
  });
});

document.querySelector("#adra-test-cloud").addEventListener("click", () => {
  const url = document.querySelector("#adra-cloud-url").value.trim();
  if (!url) {
    adraRenderCloudStatus("", "");
    return;
  }
  if (!adraValidCloudUrl(url)) {
    adraCloudStatus.textContent = "الحالة: رابط غير صالح (http/https فقط)";
    adraCloudStatus.classList.add("adra-cloud-error");
    return;
  }
  const testButton = document.querySelector("#adra-test-cloud");
  testButton.disabled = true;
  adraCloudStatus.textContent = "الحالة: جارٍ الاختبار…";
  adraCloudStatus.classList.remove("adra-cloud-ok", "adra-cloud-error");

  chrome.storage.local.set({ [ADRA_CLOUD_URL_KEY]: url }, () => {
    if (chrome.runtime.lastError) {
      testButton.disabled = false;
      adraRenderCloudStatus(url, "error");
      return;
    }
    chrome.runtime.sendMessage({ type: "ADRA_CLOUD_TEST_CONNECTION" }, (response) => {
      testButton.disabled = false;
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
      const detail = response && !response.ok ? (response.error || "") : "";
      adraRenderCloudStatus(url, connected ? "connected" : "error", detail);
    });
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
  const url = document.querySelector("#adra-cloud-url").value.trim();
  if (!url || !adraValidCloudUrl(url)) {
    adraSyncProgress.textContent = "أضف رابط ADRA Cloud صالحًا أولًا";
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
      adraSyncProgress.textContent = "أضف رابط ADRA Cloud أولًا";
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