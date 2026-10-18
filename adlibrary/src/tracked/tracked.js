"use strict";

const ADRA_TRACKED_PAGES_KEY = "adra_tracked_pages";
const adraList = document.querySelector("#adra-tracked-list");
const adraStatus = document.querySelector("#adra-tracked-status");

function adraGetTrackedPages() {
  return new Promise((resolve, reject) => chrome.storage.local.get(ADRA_TRACKED_PAGES_KEY, (result) => {
    if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
    resolve(Array.isArray(result[ADRA_TRACKED_PAGES_KEY]) ? result[ADRA_TRACKED_PAGES_KEY] : []);
  }));
}

function adraSetTrackedPages(pages) {
  return new Promise((resolve, reject) => chrome.storage.local.set({ [ADRA_TRACKED_PAGES_KEY]: pages }, () => {
    if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
    resolve();
  }));
}

function adraElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = text;
  return element;
}

function adraButton(label, url, className = "") {
  const button = adraElement("button", className, label);
  button.type = "button";
  button.disabled = !url;
  button.addEventListener("click", () => url && window.open(url, "_blank", "noopener,noreferrer"));
  return button;
}

function adraDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("ar");
}

function adraMetric(label, value) {
  return adraElement("span", "adra-metric", `${label}: ${value ?? "—"}`);
}

function adraPageAdsUrl(page) {
  if (page.pageAdsUrl) return page.pageAdsUrl;
  if (!/^\d+$/u.test(String(page.pageId || ""))) return "";
  const url = new URL("https://www.facebook.com/ads/library/");
  url.searchParams.set("active_status", "active");
  url.searchParams.set("ad_type", "all");
  url.searchParams.set("country", "ALL");
  url.searchParams.set("is_targeted_country", "false");
  url.searchParams.set("media_type", "all");
  url.searchParams.set("search_type", "page");
  url.searchParams.set("sort_data[mode]", "total_impressions");
  url.searchParams.set("sort_data[direction]", "desc");
  url.searchParams.set("view_all_page_id", String(page.pageId));
  return url.href;
}

async function adraRender() {
  try {
    const pages = (await adraGetTrackedPages()).sort((first, second) =>
      String(second.lastSnapshotAt || "").localeCompare(String(first.lastSnapshotAt || ""))
    );
    adraList.replaceChildren();
    if (!pages.length) {
      adraList.append(adraElement("p", "adra-empty", "لا توجد صفحات متتبعة بعد."));
      return;
    }

    pages.forEach((page) => {
      const article = adraElement("article", "adra-tracked-card");
      const header = adraElement("div", "");
      header.append(
        adraElement("h2", "", page.pageName || "صفحة غير معروفة"),
        adraElement("p", "adra-page-id", `Page ID: ${page.pageId || "—"}`)
      );

      const ads = Array.isArray(page.ads) ? page.ads : [];
      const activeAds = ads.filter((ad) => ad.status === "ACTIVE");
      const metrics = adraElement("div", "adra-metrics");
      metrics.append(
        adraMetric("Ads saved", ads.length),
        adraMetric("Active at capture", activeAds.length),
        adraMetric("Snapshots", Array.isArray(page.snapshots) ? page.snapshots.length : 0),
        adraMetric("Last", adraDate(page.lastSnapshotAt))
      );

      const snapshots = adraElement("div", "adra-snapshots");
      (Array.isArray(page.snapshots) ? page.snapshots.slice().reverse().slice(0, 6) : []).forEach((snapshot) => {
        const row = adraElement("div", "adra-snapshot-row");
        row.append(
          adraElement("time", "", adraDate(snapshot.capturedAt)),
          adraElement("span", "", `${snapshot.activeAdsCount || 0} active`)
        );
        snapshots.append(row);
      });

      const actions = adraElement("div", "adra-actions");
      const pageUrl = adraPageAdsUrl(page);
      const openPage = adraButton("فتح الصفحة", pageUrl);
      const openFirstAd = adraButton("فتح أول إعلان", ads[0]?.adLibraryUrl || "");
      const remove = adraElement("button", "adra-delete", "حذف");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        await adraSetTrackedPages((await adraGetTrackedPages()).filter((item) =>
          String(item.pageId) !== String(page.pageId)
        ));
        // Local delete already succeeded — cloud untrack is best-effort and
        // must never block or undo it (callback consumes lastError).
        chrome.runtime.sendMessage(
          { type: "ADRA_CLOUD_UNTRACK_PAGE", facebookPageId: String(page.pageId || "") },
          () => void chrome.runtime.lastError
        );
        await adraRender();
      });
      actions.append(openPage, openFirstAd, remove);
      article.append(header, metrics, snapshots, actions);
      adraList.append(article);
    });
  } catch (_error) {
    adraStatus.textContent = "تعذر قراءة الصفحات المتتبعة";
  }
}

document.querySelector("#adra-delete-all-tracked").addEventListener("click", async () => {
  if (!confirm("هل تريد حذف جميع الصفحات المتتبعة؟")) return;
  try {
    const pages = await adraGetTrackedPages();
    await adraSetTrackedPages([]);
    // Best-effort cloud untrack for each page, after the local clear succeeded.
    pages
      .map((page) => String(page.pageId || ""))
      .filter((pageId) => /^\d+$/u.test(pageId))
      .forEach((pageId) => {
        chrome.runtime.sendMessage(
          { type: "ADRA_CLOUD_UNTRACK_PAGE", facebookPageId: pageId },
          () => void chrome.runtime.lastError
        );
      });
    await adraRender();
    adraStatus.textContent = "تم حذف جميع الصفحات";
  } catch (_error) {
    adraStatus.textContent = "تعذر حذف الصفحات";
  }
});

adraRender();
