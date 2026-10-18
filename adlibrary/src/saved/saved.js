"use strict";

const ADRA_STORAGE_KEY = "adra_saved_ads";
const adraList = document.querySelector("#adra-saved-list");
const adraStatus = document.querySelector("#adra-saved-status");

function adraGetAds() {
  return new Promise((resolve, reject) => chrome.storage.local.get(ADRA_STORAGE_KEY, (result) => {
    if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
    resolve(Array.isArray(result[ADRA_STORAGE_KEY]) ? result[ADRA_STORAGE_KEY] : []);
  }));
}

function adraSetAds(ads) {
  return new Promise((resolve, reject) => chrome.storage.local.set({ [ADRA_STORAGE_KEY]: ads }, () => {
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

function adraAdLibraryUrl(ad) {
  if (ad.adLibraryUrl) return ad.adLibraryUrl;
  if (!/^\d+$/u.test(String(ad.id || ""))) return "";
  const url = new URL("https://www.facebook.com/ads/library/");
  url.searchParams.set("id", String(ad.id));
  return url.href;
}

function adraPageAdsUrl(ad) {
  if (!/^\d+$/u.test(String(ad.pageId || ""))) return "";
  const url = new URL("https://www.facebook.com/ads/library/");
  url.searchParams.set("active_status", "active");
  url.searchParams.set("ad_type", "all");
  url.searchParams.set("country", "ALL");
  url.searchParams.set("is_targeted_country", "false");
  url.searchParams.set("media_type", "all");
  url.searchParams.set("search_type", "page");
  url.searchParams.set("sort_data[mode]", "total_impressions");
  url.searchParams.set("sort_data[direction]", "desc");
  url.searchParams.set("view_all_page_id", String(ad.pageId));
  return url.href;
}

function adraButton(label, url, className = "") {
  const button = adraElement("button", className, label);
  button.type = "button";
  button.disabled = !url;
  button.addEventListener("click", () => url && window.open(url, "_blank", "noopener,noreferrer"));
  return button;
}

function adraMetric(label, value) {
  const item = adraElement("span", "adra-metric");
  item.append(adraElement("strong", "", label), document.createTextNode(value || "—"));
  return item;
}

async function adraRender() {
  try {
    const ads = (await adraGetAds()).sort((first, second) =>
      String(second.savedAt || second.detectedAt || "").localeCompare(String(first.savedAt || first.detectedAt || ""))
    );
    adraList.replaceChildren();
    if (!ads.length) {
      adraList.append(adraElement("p", "adra-empty", "لا توجد إعلانات محفوظة."));
      return;
    }
    ads.forEach((ad) => {
      const adLibraryUrl = adraAdLibraryUrl(ad);
      const pageAdsUrl = adraPageAdsUrl(ad);
      const article = adraElement("article", "adra-saved-card");

      const media = adraElement("div", "adra-card-media");
      const previewUrl = ad.previewImageUrl || ad.imageUrls?.[0] || "";
      if (previewUrl) {
        const image = document.createElement("img");
        image.src = previewUrl;
        image.alt = ad.pageName || "Ad preview";
        image.loading = "lazy";
        media.append(image);
      } else {
        media.append(adraElement("span", "", ad.mediaType || "AD"));
      }

      const body = adraElement("div", "adra-card-body");
      const header = adraElement("div", "adra-card-header");
      const titleGroup = adraElement("div", "");
      titleGroup.append(
        adraElement("h2", "", ad.pageName || "صفحة غير معروفة"),
        adraElement("p", "adra-card-domain", ad.domain || "لا يوجد دومين")
      );
      const savedAt = adraElement("time", "", new Date(ad.savedAt || ad.detectedAt).toLocaleString("ar"));
      header.append(titleGroup, savedAt);

      const metrics = adraElement("div", "adra-card-metrics");
      metrics.append(
        adraMetric("Active: ", ad.status === "ACTIVE" ? `${ad.activeDays ?? "—"} days` : "—"),
        adraMetric("CTA: ", ad.cta),
        adraMetric("Page likes: ", ad.pageLikes ? ad.pageLikes.toLocaleString("en-US") : ""),
        adraMetric("Media: ", ad.mediaType)
      );

      const link = adraElement("a", "adra-ad-link", adLibraryUrl ? "رابط الإعلان" : "لا يوجد رابط إعلان");
      if (adLibraryUrl) {
        link.href = adLibraryUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      }

      const actions = adraElement("div", "adra-card-actions");
      const openAd = adraButton("فتح الإعلان", adLibraryUrl);
      const openLanding = adraButton("فتح المتجر", ad.landingUrl);
      const openPageAds = adraButton("فتح إعلانات الصفحة", pageAdsUrl);
      const remove = adraElement("button", "adra-delete", "حذف الإعلان");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        await adraSetAds((await adraGetAds()).filter((item) => item.id !== ad.id));
        await adraRender();
      });
      actions.append(openAd, openLanding, openPageAds, remove);
      body.append(header, metrics, link, actions);
      article.append(media, body);
      adraList.append(article);
    });
  } catch (_error) {
    adraStatus.textContent = "تعذر قراءة الإعلانات المحفوظة";
  }
}

document.querySelector("#adra-delete-all").addEventListener("click", async () => {
  if (!confirm("هل تريد حذف جميع الإعلانات المحفوظة؟")) return;
  try {
    await adraSetAds([]);
    await adraRender();
    adraStatus.textContent = "تم حذف جميع الإعلانات";
  } catch (_error) {
    adraStatus.textContent = "تعذر حذف الإعلانات";
  }
});

adraRender();
