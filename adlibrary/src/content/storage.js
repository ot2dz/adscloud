"use strict";

(() => {
  const ADRA_STORAGE_KEY = "adra_saved_ads";
  const ADRA_TRACKED_PAGES_KEY = "adra_tracked_pages";

  function adraGet() {
    return new Promise((resolve, reject) => {
      chrome.storage.local.get(ADRA_STORAGE_KEY, (result) => {
        const error = chrome.runtime.lastError;
        if (error) return reject(new Error(error.message));
        resolve(Array.isArray(result[ADRA_STORAGE_KEY]) ? result[ADRA_STORAGE_KEY] : []);
      });
    });
  }

  function adraSet(ads) {
    return new Promise((resolve, reject) => {
      chrome.storage.local.set({ [ADRA_STORAGE_KEY]: ads }, () => {
        const error = chrome.runtime.lastError;
        if (error) return reject(new Error(error.message));
        resolve();
      });
    });
  }

  function adraAdLibraryUrl(ad) {
    if (!/^\d+$/u.test(String(ad?.id || ""))) return "";
    const url = new URL("https://www.facebook.com/ads/library/");
    url.searchParams.set("id", String(ad.id));
    return url.href;
  }

  async function saveAd(ad) {
    const ads = await adraGet();
    if (ads.some((item) => item.id === ad.id)) return { saved: false, duplicate: true };
    await adraSet([...ads, {
      ...ad,
      adLibraryUrl: ad.adLibraryUrl || adraAdLibraryUrl(ad),
      savedAt: new Date().toISOString()
    }]);
    return { saved: true, duplicate: false };
  }

  function adraGetTrackedPages() {
    return new Promise((resolve, reject) => {
      chrome.storage.local.get(ADRA_TRACKED_PAGES_KEY, (result) => {
        const error = chrome.runtime.lastError;
        if (error) return reject(new Error(error.message));
        resolve(
          Array.isArray(result[ADRA_TRACKED_PAGES_KEY])
            ? result[ADRA_TRACKED_PAGES_KEY]
            : []
        );
      });
    });
  }

  function adraSetTrackedPages(pages) {
    return new Promise((resolve, reject) => {
      chrome.storage.local.set({ [ADRA_TRACKED_PAGES_KEY]: pages }, () => {
        const error = chrome.runtime.lastError;
        if (error) return reject(new Error(error.message));
        resolve();
      });
    });
  }

  async function trackPage(page) {
    const pages = await adraGetTrackedPages();
    const pageId = String(page?.pageId || "");
    if (!/^\d+$/u.test(pageId)) {
      throw new Error("A numeric pageId is required");
    }

    const now = new Date().toISOString();
    const existingIndex = pages.findIndex((item) => String(item.pageId) === pageId);
    const previous = existingIndex >= 0 ? pages[existingIndex] : {};
    const tracked = {
      ...previous,
      ...page,
      pageId,
      firstTrackedAt: previous.firstTrackedAt || now,
      lastSnapshotAt: now,
      snapshots: [
        ...(Array.isArray(previous.snapshots) ? previous.snapshots : []),
        {
          capturedAt: now,
          activeAdsCount: Number(page.activeAdsCount || 0),
          adIds: Array.isArray(page.ads) ? page.ads.map((ad) => ad.id).filter(Boolean) : []
        }
      ].slice(-30)
    };

    const nextPages = existingIndex >= 0
      ? pages.map((item, index) => index === existingIndex ? tracked : item)
      : [...pages, tracked];
    await adraSetTrackedPages(nextPages);
    return { tracked, updated: existingIndex >= 0 };
  }

  globalThis.adraStorage = Object.freeze({
    key: ADRA_STORAGE_KEY,
    trackedPagesKey: ADRA_TRACKED_PAGES_KEY,
    getAds: adraGet,
    setAds: adraSet,
    saveAd,
    getTrackedPages: adraGetTrackedPages,
    setTrackedPages: adraSetTrackedPages,
    trackPage
  });
})();
