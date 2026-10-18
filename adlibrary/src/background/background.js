"use strict";

const ADRA_PLATFORM_CACHE_KEY = "adra_platform_origin_cache";
const ADRA_PLATFORM_CACHE_VERSION = 4;
const ADRA_PLATFORM_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;

const ADRA_PLATFORM_RULES = Object.freeze([
  {
    id: "shopify",
    label: "Shopify",
    patterns: [
      /\bShopify\.theme\b/iu,
      /cdn\.shopify\.com/iu,
      /shopify-section/iu,
      /x-shopify-/iu,
      /shopify_pay/iu,
      /shopify-features/iu,
      /shopifycloud/iu,
      /window\.Shopify/iu,
      /myshopify\.com/iu
    ]
  },
  {
    id: "foorweb",
    label: "Foorweb",
    patterns: [
      /foorweb/iu,
      /cdn\.foorweb/iu,
      /foorweb\.(?:store|shop|app|com)/iu,
      /foorweb-app/iu,
      /foorweb-store/iu,
      /foorweb-backend/iu,
      /"googleSheetName"\s*:\s*"foorweb"/iu,
      /"title"\s*:\s*"Foorweb/iu
    ]
  },
  {
    id: "youcan",
    label: "YouCan",
    patterns: [
      /youcan/iu,
      /cdn\.youcan/iu,
      /youcan\.shop/iu,
      /youcan\.store/iu,
      /youcan\.app/iu,
      /youcan-pay/iu,
      /youcanjs/iu
    ]
  },
  {
    id: "flexdz",
    label: "FlexDZ",
    patterns: [
      /flexdz/iu,
      /cdn\.flexdz/iu,
      /flexdz\.(?:com|app|shop|store)/iu,
      /flex-dz/iu,
      /flexdz-store/iu
    ]
  }
]);

function adraStorageGet(keys) {
  return new Promise((resolve, reject) => {
    chrome.storage.local.get(keys, (result) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      resolve(result || {});
    });
  });
}

function adraStorageSet(values) {
  return new Promise((resolve, reject) => {
    chrome.storage.local.set(values, () => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      resolve();
    });
  });
}

// One-time hygiene: drop keys left behind by the removed usage-limit/ExtPay code.
chrome.runtime.onInstalled.addListener(() => {
  const legacyLocalKeys = ["adra_usage_count", "extensionpay_api_key", "extensionpay_user", "extensionpay_installed_at"];
  const legacySyncKeys = ["extensionpay_api_key", "extensionpay_user", "extensionpay_installed_at"];
  chrome.storage.local.remove(legacyLocalKeys, () => { void chrome.runtime.lastError; });
  chrome.storage.sync.remove(legacySyncKeys, () => { void chrome.runtime.lastError; });
});

// --- ADRA Cloud sync (Phase 2) ---------------------------------------------
// All backend traffic is centralized here in the service worker. Content
// scripts never fetch the Worker directly — they send ADRA_CLOUD_* messages.

const ADRA_CLOUD_API_URL_KEY = "adra_cloud_api_url";
const ADRA_CLOUD_TOKEN_KEY = "adra_cloud_token";
const ADRA_CLOUD_TIMEOUT_MS = 8000;

/** Validate + normalize the configured base URL (http/https only, no trailing slash). */
function adraNormalizeCloudBaseUrl(raw) {
  const trimmed = String(raw || "").trim();
  if (!trimmed) return { configured: false };
  const withoutTrailingSlash = trimmed.replace(/\/+$/u, "");
  let parsed;
  try {
    parsed = new URL(withoutTrailingSlash);
  } catch (_error) {
    return { configured: true, error: "INVALID_URL" };
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { configured: true, error: "INVALID_URL" };
  }
  return { configured: true, base: withoutTrailingSlash };
}

/**
 * Central fetch helper for ADRA Cloud.
 * Never throws. Uniform response shapes:
 *   { ok: true, data }
 *   { ok: false, skipped: true, reason: "NOT_CONFIGURED" }  (no URL set)
 *   { ok: false, error: "INVALID_URL" | "NETWORK_ERROR" | "TIMEOUT" |
 *                        "INVALID_JSON" | "HTTP_<status>..." | ... }
 */
async function adraCloudRequest(path, options = {}) {
  const { method = "GET", body, timeoutMs = ADRA_CLOUD_TIMEOUT_MS } = options;

  let stored;
  try {
    stored = await adraStorageGet([ADRA_CLOUD_API_URL_KEY, ADRA_CLOUD_TOKEN_KEY]);
  } catch (_error) {
    return { ok: false, error: "STORAGE_ERROR" };
  }

  const normalized = adraNormalizeCloudBaseUrl(stored[ADRA_CLOUD_API_URL_KEY]);
  if (!normalized.configured) {
    return { ok: false, skipped: true, reason: "NOT_CONFIGURED" };
  }
  if (normalized.error) {
    return { ok: false, error: normalized.error };
  }

  const endpoint = `${normalized.base}${path}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = { "Content-Type": "application/json" };
    // Optional auth: attach the stored token (never hardcode secrets).
    const token = String(stored[ADRA_CLOUD_TOKEN_KEY] || "").trim();
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
      headers["X-Ingest-Token"] = token;
    }
    const init = { method, headers, signal: controller.signal };
    if (body !== undefined) init.body = JSON.stringify(body);

    const response = await fetch(endpoint, init);

    let parsed = null;
    let parseFailed = false;
    try {
      parsed = await response.json();
    } catch (_error) {
      parseFailed = true;
    }

    if (!response.ok) {
      const detail =
        parsed && parsed.error && parsed.error.message ? `: ${parsed.error.message}` : "";
      return { ok: false, error: `HTTP_${response.status}${detail}` };
    }
    if (parseFailed) return { ok: false, error: "INVALID_JSON" };
    // Envelope {ok,data} -> data; top-level bodies (health) -> whole body.
    return { ok: true, data: parsed && parsed.data !== undefined ? parsed.data : parsed };
  } catch (_error) {
    if (controller.signal.aborted) return { ok: false, error: "TIMEOUT" };
    return { ok: false, error: "NETWORK_ERROR" };
  } finally {
    clearTimeout(timer);
  }
}

/** Skip absent/empty values — never send undefined/null/"" over good cloud data. */
function adraCloudPut(payload, key, value) {
  if (value === undefined || value === null) return;
  if (typeof value === "string" && value.trim() === "") return;
  payload[key] = value;
}

/** Map a local saved-ad record to the /api/v1/ads schema. Null when id missing. */
function adraMapAdToCloud(ad) {
  if (!ad) return null;
  const id = String(ad.id ?? "").trim();
  if (!id) return null;

  const payload = { facebook_ad_id: id };
  adraCloudPut(payload, "page_id", ad.pageId != null && ad.pageId !== "" ? String(ad.pageId) : undefined);
  adraCloudPut(payload, "page_name", ad.pageName);
  adraCloudPut(payload, "ad_text", ad.adText || ad.primaryText);
  adraCloudPut(payload, "landing_url", ad.landingUrl);
  adraCloudPut(payload, "landing_domain", ad.domain);
  adraCloudPut(payload, "platform", ad.platformLabel || ad.platform);
  adraCloudPut(payload, "cta", ad.cta);
  adraCloudPut(payload, "start_date", ad.startedAt);
  adraCloudPut(payload, "media_type", ad.mediaType);
  adraCloudPut(payload, "saved_at", ad.savedAt);
  adraCloudPut(payload, "preview_image_url", ad.previewImageUrl || ad.preview_image_url || (Array.isArray(ad.imageUrls) ? ad.imageUrls[0] : undefined));
  // Counts: the extension uses 0 as its "unknown" sentinel (defensive
  // extraction), so only send real positive values — cloud keeps NULL instead.
  if (Number.isFinite(ad.activeDays) && ad.activeDays > 0) payload.active_days = ad.activeDays;
  if (Number.isFinite(ad.pageLikes) && ad.pageLikes > 0) payload.page_likes = ad.pageLikes;
  if (Number.isFinite(ad.creativeAdsCount) && ad.creativeAdsCount > 0) {
    payload.creative_count = ad.creativeAdsCount;
  }
  const media = {};
  if (Array.isArray(ad.videoUrls) && ad.videoUrls.length) media.videoUrls = ad.videoUrls;
  if (Array.isArray(ad.imageUrls) && ad.imageUrls.length) media.imageUrls = ad.imageUrls;
  if (media.videoUrls || media.imageUrls) payload.media_json = JSON.stringify(media);
  return payload;
}

/** Map a tracked-page record to the /api/v1/pages/track schema. Null when id invalid. */
function adraMapPageToCloud(page) {
  if (!page) return null;
  const pageId = String(page.pageId ?? "").trim();
  if (!/^\d+$/u.test(pageId)) return null;
  const payload = { facebook_page_id: pageId };
  adraCloudPut(payload, "page_name", page.pageName);
  adraCloudPut(payload, "page_url", page.pageAdsUrl || page.pageUrl);
  if (Number.isFinite(page.pageLikes) && page.pageLikes > 0) payload.page_likes = page.pageLikes;
  return payload;
}

/**
 * Map a Capture Snapshot payload to POST /api/v1/observations/page-snapshot.
 * Null when the snapshot/page id/session id is unusable (never a partial body).
 * Ads go through the Phase 2 ad mapper; client-side dedup already happened in
 * adraTrackedPageSnapshot (first id wins).
 */
function adraMapSnapshotToCloud(snapshot, scanSessionId) {
  if (!snapshot) return null;
  const pageId = String(snapshot.pageId ?? "").trim();
  if (!/^\d+$/u.test(pageId)) return null;
  const sessionId = String(scanSessionId ?? "").trim();
  if (!sessionId) return null;

  const ads = [];
  const seen = new Set();
  for (const ad of Array.isArray(snapshot.ads) ? snapshot.ads : []) {
    const mapped = adraMapAdToCloud(ad);
    if (!mapped) continue;
    if (seen.has(mapped.facebook_ad_id)) continue;
    seen.add(mapped.facebook_ad_id);
    ads.push(mapped);
  }

  const payload = {
    facebook_page_id: pageId,
    scan_session_id: sessionId,
    source: "extension_page_scan",
    captured_at: new Date().toISOString(),
    ads
  };
  adraCloudPut(payload, "page_name", snapshot.pageName);
  adraCloudPut(payload, "page_url", snapshot.pageAdsUrl || snapshot.pageUrl);
  return payload;
}

// --- Historical Sync (Phase 4) -----------------------------------------------
// One message uploads ALL local saved ads + tracked pages to ADRA Cloud in
// bounded batches (chunk = 25). UPLOAD ONLY — local storage is never modified,
// never cleared, on success or on failure. One retry per batch, then a logical
// stop of the whole sync.

const ADRA_CLOUD_SYNC_CHUNK = 25;
const ADRA_CLOUD_SYNC_AT_KEY = "adra_cloud_last_historical_sync_at";
const ADRA_CLOUD_SYNC_RESULT_KEY = "adra_cloud_last_historical_sync_result";
const ADRA_LOCAL_ADS_KEY = "adra_saved_ads";
const ADRA_LOCAL_PAGES_KEY = "adra_tracked_pages";

/** Best-effort progress broadcast to the popup (absent listener is fine). */
function adraBroadcastSyncProgress(progress) {
  try {
    chrome.runtime.sendMessage({ type: "ADRA_CLOUD_SYNC_PROGRESS", progress }, () => {
      void chrome.runtime.lastError; // popup may be closed — drop silently
    });
  } catch (_error) {
    // No listeners — progress is cosmetic only, never fails the sync.
  }
}

/**
 * Upload one section (ads or pages) in chunks of ADRA_CLOUD_SYNC_CHUNK.
 * Each failed chunk is retried EXACTLY once; a second failure stops the
 * whole sync (logical stop — remaining chunks/sections are not attempted).
 * Returns { sent, stopped, skipped, reason? }.
 */
async function adraUploadHistorySection(payloads, apiPath, label, stage) {
  const total = payloads.length;
  let sent = 0;
  for (let start = 0; start < total; start += ADRA_CLOUD_SYNC_CHUNK) {
    const chunk = payloads.slice(start, start + ADRA_CLOUD_SYNC_CHUNK);
    const body = { items: chunk };
    let attempt = await adraCloudRequest(apiPath, { method: "POST", body });
    if (!attempt.ok && !attempt.skipped) {
      // Exactly one retry per batch — no loops, no silent multi-tries.
      attempt = await adraCloudRequest(apiPath, { method: "POST", body });
    }
    if (attempt.ok) {
      sent += chunk.length;
      adraBroadcastSyncProgress({
        stage,
        sent,
        total,
        text: `${label}: ${sent} / ${total}`,
        done: false
      });
      continue;
    }
    if (attempt.skipped) {
      return { sent, stopped: true, skipped: true, reason: attempt.reason || "NOT_CONFIGURED" };
    }
    return { sent, stopped: true, skipped: false };
  }
  return { sent, stopped: false, skipped: false };
}

/**
 * ADRA_CLOUD_SYNC_HISTORY — full local -> cloud upload.
 * Never throws; never touches adra_saved_ads / adra_tracked_pages.
 * Writes optional state keys:
 *   adra_cloud_last_historical_sync_at     ISO timestamp
 *   adra_cloud_last_historical_sync_result "ok" | "partial" | "failed"
 * State never blocks re-running the sync.
 */
async function adraSyncHistory() {
  let stored;
  try {
    stored = await adraStorageGet([ADRA_LOCAL_ADS_KEY, ADRA_LOCAL_PAGES_KEY]);
  } catch (_error) {
    return { ok: false, error: "STORAGE_ERROR" };
  }

  const localAds = Array.isArray(stored[ADRA_LOCAL_ADS_KEY]) ? stored[ADRA_LOCAL_ADS_KEY] : [];
  const localPages = Array.isArray(stored[ADRA_LOCAL_PAGES_KEY])
    ? stored[ADRA_LOCAL_PAGES_KEY]
    : [];
  const adPayloads = localAds.map(adraMapAdToCloud).filter(Boolean);
  const pagePayloads = localPages.map(adraMapPageToCloud).filter(Boolean);

  if (adPayloads.length === 0 && pagePayloads.length === 0) {
    return {
      ok: true,
      data: {
        text: "اكتملت المزامنة",
        result: "ok",
        ads_sent: 0,
        ads_total: 0,
        pages_sent: 0,
        pages_total: 0,
        empty: true
      }
    };
  }

  const adsResult = await adraUploadHistorySection(
    adPayloads,
    "/api/v1/sync/ads",
    "Saved Ads",
    "ads"
  );
  if (adsResult.skipped) {
    return { ok: false, skipped: true, reason: adsResult.reason };
  }

  let pagesResult = { sent: 0, stopped: false };
  if (!adsResult.stopped) {
    pagesResult = await adraUploadHistorySection(
      pagePayloads,
      "/api/v1/sync/pages",
      "Tracked Pages",
      "pages"
    );
    if (pagesResult.skipped) {
      return { ok: false, skipped: true, reason: pagesResult.reason };
    }
  }

  const totalMapped = adPayloads.length + pagePayloads.length;
  const totalSent = adsResult.sent + pagesResult.sent;
  const failedCount = totalMapped - totalSent;
  const stopped = adsResult.stopped || pagesResult.stopped;

  let text;
  let result;
  if (!stopped && failedCount === 0) {
    text = "اكتملت المزامنة";
    result = "ok";
  } else if (totalSent > 0) {
    text = `تمت مزامنة ${totalSent} إعلان`;
    result = "partial";
  } else {
    text = `فشل ${failedCount}`;
    result = "failed";
  }

  const at = new Date().toISOString();
  try {
    await adraStorageSet({
      [ADRA_CLOUD_SYNC_AT_KEY]: at,
      [ADRA_CLOUD_SYNC_RESULT_KEY]: result
    });
  } catch (_error) {
    // State is optional — a storage failure never fails the sync itself.
  }

  adraBroadcastSyncProgress({
    stage: "done",
    done: true,
    ok: result === "ok",
    text,
    ads_sent: adsResult.sent,
    pages_sent: pagesResult.sent,
    total: totalMapped,
    at
  });

  return {
    ok: result === "ok",
    data: {
      text,
      result,
      at,
      ads_sent: adsResult.sent,
      ads_total: adPayloads.length,
      pages_sent: pagesResult.sent,
      pages_total: pagePayloads.length
    }
  };
}

/** Handle ADRA_CLOUD_* messages. Returns true when a response is pending. */
function adraHandleCloudMessage(message, sendResponse) {
  const respond = (promise) => {
    promise
      .then((result) => sendResponse(result))
      .catch(() => sendResponse({ ok: false, error: "INTERNAL" }));
    return true;
  };

  switch (message.type) {
    case "ADRA_CLOUD_SAVE_AD": {
      const payload = adraMapAdToCloud(message.ad);
      if (!payload) return respond(Promise.resolve({ ok: false, error: "INVALID_AD" }));
      return respond(adraCloudRequest("/api/v1/ads", { method: "POST", body: payload }));
    }
    case "ADRA_CLOUD_TRACK_PAGE": {
      const payload = adraMapPageToCloud(message.page);
      if (!payload) return respond(Promise.resolve({ ok: false, error: "INVALID_PAGE" }));
      return respond(
        adraCloudRequest("/api/v1/pages/track", { method: "POST", body: payload })
      );
    }
    case "ADRA_CLOUD_UNTRACK_PAGE": {
      const pageId = String(message.facebookPageId ?? "").trim();
      if (!/^\d+$/u.test(pageId)) {
        return respond(Promise.resolve({ ok: false, error: "INVALID_PAGE_ID" }));
      }
      return respond(
        adraCloudRequest(`/api/v1/pages/${encodeURIComponent(pageId)}/untrack`, {
          method: "POST"
        })
      );
    }
    case "ADRA_CLOUD_CAPTURE_SNAPSHOT": {
      const payload = adraMapSnapshotToCloud(message.snapshot, message.scanSessionId);
      if (!payload) {
        return respond(Promise.resolve({ ok: false, error: "INVALID_SNAPSHOT" }));
      }
      // Larger batches need more time than the default 8s ad/page calls.
      return respond(
        adraCloudRequest("/api/v1/observations/page-snapshot", {
          method: "POST",
          body: payload,
          timeoutMs: 20000
        })
      );
    }
    case "ADRA_CLOUD_SYNC_HISTORY":
      return respond(adraSyncHistory());
    case "ADRA_CLOUD_TEST_CONNECTION":
      return respond(adraCloudRequest("/api/v1/health", { method: "GET" }));
    default:
      return false;
  }
}

function adraLandingOrigin(rawUrl) {
  try {
    const url = new URL(rawUrl);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    return url.origin;
  } catch (_error) {
    return null;
  }
}

function adraNormalizeLandingUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    if (!["http:", "https:"].includes(url.protocol)) return "";
    url.hash = "";
    return url.href;
  } catch (_error) {
    return "";
  }
}

function adraDetectPlatformFromText(text) {
  const body = String(text || "");
  const rule = ADRA_PLATFORM_RULES.find((platform) =>
    platform.patterns.some((pattern) => pattern.test(body))
  );
  return rule
    ? { id: rule.id, label: rule.label, evidence: "source_code" }
    : { id: "", label: "", evidence: "" };
}

async function adraPlatformCache() {
  const result = await adraStorageGet(ADRA_PLATFORM_CACHE_KEY);
  const cache = result[ADRA_PLATFORM_CACHE_KEY];
  return cache && typeof cache === "object" ? cache : {};
}

async function adraSetPlatformCache(cache) {
  await adraStorageSet({ [ADRA_PLATFORM_CACHE_KEY]: cache });
}

async function adraFetchText(url) {
  try {
    const response = await fetch(url, { redirect: "follow" });
    if (!response.ok) return { text: "", finalUrl: response.url || url };
    return { text: await response.text(), finalUrl: response.url || url };
  } catch (_error) {
    return { text: "", finalUrl: url };
  }
}

async function adraDetectPlatform(landingUrl) {
  const normalizedUrl = adraNormalizeLandingUrl(landingUrl);
  const origin = adraLandingOrigin(landingUrl);
  if (!origin) {
    return { ok: true, platform: "", label: "", source: "invalid_url" };
  }

  const now = Date.now();
  const cache = await adraPlatformCache();
  const cached = cache[origin];
  if (cached && cached.version === ADRA_PLATFORM_CACHE_VERSION && cached.expiresAt > now) {
    return {
      ok: true,
      platform: cached.platform || "",
      label: cached.label || "",
      evidence: cached.evidence || "",
      origin,
      source: "cache"
    };
  }

  const landingFetch = normalizedUrl
    ? await adraFetchText(normalizedUrl)
    : { text: "", finalUrl: origin };
  let platform = adraDetectPlatformFromText(landingFetch.text);
  let source = "landing_page";

  if (!platform.id) {
    const originFetch = await adraFetchText(origin);
    platform = adraDetectPlatformFromText(originFetch.text);
    source = platform.id ? "origin_page" : "network";
  }

  cache[origin] = {
    platform: platform.id,
    label: platform.label,
    evidence: platform.evidence,
    version: ADRA_PLATFORM_CACHE_VERSION,
    checkedAt: now,
    expiresAt: now + ADRA_PLATFORM_CACHE_TTL_MS
  };
  await adraSetPlatformCache(cache);
  return {
    ok: true,
    platform: platform.id,
    label: platform.label,
    evidence: platform.evidence,
    origin,
    source
  };
}

function adraDownload(url, filename) {
  return new Promise((resolve, reject) => {
    chrome.downloads.download({ url, filename, conflictAction: "uniquify", saveAs: false }, (id) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      resolve(id);
    });
  });
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "ADRA_DETECT_PLATFORM") {
    adraDetectPlatform(message.landingUrl)
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (typeof message?.type === "string" && message.type.startsWith("ADRA_CLOUD_")) {
    return adraHandleCloudMessage(message, sendResponse);
  }

  if (message?.type !== "ADRA_DOWNLOAD_MEDIA" || !Array.isArray(message.items)) return false;

  Promise.allSettled(
    message.items
      .filter((item) => /^https?:\/\//iu.test(item.url) && typeof item.filename === "string")
      .map((item) => adraDownload(item.url, item.filename))
  ).then((results) => {
    const downloaded = results.filter((result) => result.status === "fulfilled").length;
    sendResponse({ ok: downloaded > 0, downloaded, failed: results.length - downloaded });
  });
  return true;
});

// Test hook (adra* global convention) — lets node:test drive the cloud helper
// without a real Service Worker. Not reachable from content scripts.
globalThis.adraBackground = Object.freeze({
  cloudRequest: adraCloudRequest,
  mapAdToCloud: adraMapAdToCloud,
  mapPageToCloud: adraMapPageToCloud,
  mapSnapshotToCloud: adraMapSnapshotToCloud,
  apiUrlStorageKey: ADRA_CLOUD_API_URL_KEY,
  syncHistory: adraSyncHistory,
  syncChunkSize: ADRA_CLOUD_SYNC_CHUNK
});
