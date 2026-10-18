"use strict";

(() => {
  const ADRA_PROCESSED_ATTRIBUTE = "data-adra-processed";
  const ADRA_BATCH_DELAY_MS = 120;
  const ADRA_LANDING_FILTER_KEY = "adra_landing_url_filter";
  const ADRA_MIN_PAGE_LIKES_KEY = "adra_min_page_likes";
  const adraProcessedCardIds = new Map();
  const adraCardToolbars = new WeakMap();
  const adraCardRecords = new Map();
  const adraCtaFilters = new Set();
  let adraMediaFilter = "";
  let adraPlatformFilter = "";
  let adraCreativeAdsFilterCount = 0;
  let adraDomainSuffixFilter = "";
  let adraLandingFilterEnabled = false;
  let adraLandingFilterSettings = { text: "", caseSensitive: false };
  let adraMinPageLikes = 0;
  let adraFilterPanelRefs = null;

  // Keep page-dependent selectors together so future Facebook DOM changes have
  // one clear maintenance point. Text and structural evidence are still
  // required; matching one of these selectors alone never identifies an ad.
  const ADRA_CARD_SELECTORS = Object.freeze([
    "[data-ad-preview]",
    '[data-testid*="ad-preview"]',
    '[data-testid*="ad-library"]',
    '[role="article"]',
    "article"
  ]);
  const ADRA_EXACT_ID_PATTERN = /^(?:Library ID|معر[ّ]?ف المكتبة)\s*:\s*\d+$/iu;

  const ADRA_LIBRARY_ID_PATTERNS = Object.freeze([
    /\blibrary\s+id\b/iu,
    /معر[ّ]?ف\s+المكتبة/iu,
    /رقم\s+تعريف\s+المكتبة/iu
  ]);

  function adraIsExtensionElement(element) {
    if (!element || element.nodeType !== 1) {
      return false;
    }

    if (typeof element.closest === "function" && element.closest('[class*="adra-"]')) {
      return true;
    }

    return Array.from(element.classList || []).some((className) =>
      className.startsWith("adra-")
    );
  }

  function adraHasMatchingText(text, patterns) {
    return patterns.some((pattern) => pattern.test(text));
  }

  function adraHasCardDetails(text) {
    return /See (?:ad|summary) details|عرض (?:تفاصيل|ملخص) الإعلان/iu.test(text);
  }

  function adraIsLikelyAdCard(element) {
    if (
      !element ||
      element.nodeType !== 1 ||
      adraIsExtensionElement(element) ||
      element.hasAttribute(ADRA_PROCESSED_ATTRIBUTE)
    ) {
      return false;
    }

    if (element.matches?.('[data-testid="ad-library-dynamic-content-container"]')) {
      return false;
    }

    const text = (element.innerText || element.textContent || "").trim();
    if (!text) {
      return false;
    }

    const hasLibraryId = adraHasMatchingText(text, ADRA_LIBRARY_ID_PATTERNS);
    // A creative sub-container can contain "Sponsored" and an outbound link,
    // but it is not the full ad card. Library ID is the reliable boundary.
    return hasLibraryId;
  }

  function findAdCards(root = document) {
    if (!root || typeof root.querySelectorAll !== "function") {
      return [];
    }

    const selector = ADRA_CARD_SELECTORS.join(",");
    const candidates = [];

    if (root.nodeType === 1 && typeof root.matches === "function" && root.matches(selector)) {
      candidates.push(root);
    }

    candidates.push(...root.querySelectorAll(selector));

    // Current Ad Library cards (verified 2026-07-23) expose no stable role or
    // data-testid. Locate exact Library-ID labels, then climb to the smallest
    // ancestor that contains both the card header and sponsored creative.
    const textCandidates = Array.from(root.querySelectorAll("div,span"));
    if (root.nodeType === 1) textCandidates.unshift(root);
    textCandidates
      .filter((element) => ADRA_EXACT_ID_PATTERN.test((element.textContent || "").trim()))
      .forEach((anchor) => {
        let current = anchor;
        for (let depth = 0; current && depth < 12; depth += 1, current = current.parentElement) {
          const text = current.innerText || current.textContent || "";
          const idCount = (text.match(/(?:Library ID|معر[ّ]?ف المكتبة)\s*:\s*\d+/giu) || []).length;
          const hasDetails = adraHasCardDetails(text);
          const hasCreative = /Sponsored|م[ُّ]?مو[ّ]?ل/iu.test(text);
          if (idCount === 1 && hasDetails && hasCreative) {
            candidates.push(current);
            break;
          }
        }
      });

    return Array.from(new Set(candidates)).filter(adraIsLikelyAdCard);
  }

  function adraProcessCard(card) {
    const cardText = card.innerText || card.textContent || "";
    const idMatch = cardText.match(/(?:Library ID|معر[ّ]?ف المكتبة)\s*:\s*(\d+)/iu);
    const cardId = idMatch?.[1] || "";
    const existingCard = cardId ? adraProcessedCardIds.get(cardId) : null;

    if (existingCard && existingCard !== card && existingCard.isConnected !== false) {
      const currentContainsExisting =
        typeof card.contains === "function" && card.contains(existingCard);
      if (currentContainsExisting) {
        adraCardToolbars.get(existingCard)?.remove();
        adraCardToolbars.delete(existingCard);
        adraCardRecords.delete(existingCard);
        existingCard.setAttribute(ADRA_PROCESSED_ATTRIBUTE, "duplicate");
      } else {
        card.setAttribute(ADRA_PROCESSED_ATTRIBUTE, "duplicate");
        adraCardToolbars.get(card)?.remove();
        adraCardToolbars.delete(card);
        return;
      }
    }

    if (cardId) {
      adraProcessedCardIds.set(cardId, card);
    }

    // Mark first so mutations caused by the phase-3 toolbar cannot enqueue the
    // same card again. Phase 3 will replace this placeholder with UI mounting.
    card.setAttribute(ADRA_PROCESSED_ATTRIBUTE, "true");
    if (typeof document !== "undefined" && globalThis.adraExtractor && globalThis.adraStorage) {
      adraMountToolbar(card);
    }
  }

  const ADRA_ICON_SAVE =
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 2.5h7a.5.5 0 0 1 .5.5v8.5L7 9.5 3 11.5V3a.5.5 0 0 1 .5-.5z"/></svg>';
  const ADRA_ICON_COPY =
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="5" width="6.5" height="6.5" rx="1"/><path d="M9 5V4a1 1 0 0 0-1-1H3.5A1 1 0 0 0 2.5 4v4a1 1 0 0 0 1 1H4"/></svg>';
  const ADRA_ICON_OPEN =
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 7.5l5-5"/><path d="M8 2.5H11.5V6"/><path d="M11.5 8.5v2A1.5 1.5 0 0 1 10 12H4a1.5 1.5 0 0 1-1.5-1.5v-6A1.5 1.5 0 0 1 4 3h2"/></svg>';
  const ADRA_ICON_DOWNLOAD =
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 2.5v7"/><path d="M4 6.5l3 3 3-3"/><path d="M2.5 10.5v.5A1.5 1.5 0 0 0 4 12.5h6A1.5 1.5 0 0 0 11.5 11v-.5"/></svg>';

  function adraButton(label, className, iconSvg) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `adra-button ${className}`;
    if (iconSvg) {
      const icon = document.createElement("span");
      icon.className = "adra-button-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = iconSvg;
      button.append(icon);
    }
    const labelSpan = document.createElement("span");
    labelSpan.className = "adra-button-label";
    labelSpan.textContent = label;
    button.append(labelSpan);
    return button;
  }

  function adraSetStatus(status, message, isError = false) {
    status.textContent = message;
    status.classList.toggle("adra-status-error", isError);
    // Chained updates (local save → cloud sync result) must restart the timer
    // so the latest message is not cleared by an earlier timeout.
    globalThis.clearTimeout(status._adraClearTimer);
    status._adraClearTimer = globalThis.setTimeout(() => { status.textContent = ""; }, 2200);
  }

  function adraRuntimeMessage(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (response) => {
          if (chrome.runtime.lastError) {
            resolve({ ok: false, error: chrome.runtime.lastError.message });
            return;
          }
          resolve(response || { ok: false, error: "No response" });
        });
      } catch (_error) {
        resolve({ ok: false, error: "Messaging failed" });
      }
    });
  }

  // Cloud sync helpers (Phase 2). Never throw, never block local operations:
  // responses are {ok:true,...} | {ok:false,skipped:true,...} | {ok:false,error}.
  function adraCloudSyncSaveAd(ad) {
    return adraRuntimeMessage({ type: "ADRA_CLOUD_SAVE_AD", ad });
  }

  function adraCloudSyncTrackPage(page) {
    return adraRuntimeMessage({ type: "ADRA_CLOUD_TRACK_PAGE", page });
  }

  function adraCloudSyncUntrackPage(facebookPageId) {
    return adraRuntimeMessage({
      type: "ADRA_CLOUD_UNTRACK_PAGE",
      facebookPageId: String(facebookPageId || "")
    });
  }

  function adraCloudSyncCapturePageSnapshot(snapshot, scanSessionId) {
    return adraRuntimeMessage({
      type: "ADRA_CLOUD_CAPTURE_SNAPSHOT",
      snapshot,
      scanSessionId: String(scanSessionId || "")
    });
  }

  globalThis.adraCloudSync = Object.freeze({
    saveAd: adraCloudSyncSaveAd,
    trackPage: adraCloudSyncTrackPage,
    untrackPage: adraCloudSyncUntrackPage,
    capturePageSnapshot: adraCloudSyncCapturePageSnapshot
  });

  function adraBuildPageAdsUrl(pageId) {
    if (!/^\d+$/u.test(String(pageId || ""))) return "";
    const url = new URL("https://www.facebook.com/ads/library/");
    url.searchParams.set("active_status", "active");
    url.searchParams.set("ad_type", "all");
    url.searchParams.set("country", "ALL");
    url.searchParams.set("is_targeted_country", "false");
    url.searchParams.set("media_type", "all");
    url.searchParams.set("search_type", "page");
    url.searchParams.set("sort_data[mode]", "total_impressions");
    url.searchParams.set("sort_data[direction]", "desc");
    url.searchParams.set("view_all_page_id", String(pageId));
    return url.href;
  }

  function adraCurrentTrackedPageId() {
    try {
      return new URL(globalThis.location.href).searchParams.get("view_all_page_id") || "";
    } catch (_error) {
      return "";
    }
  }

  function adraSafeFilename(value) {
    return String(value || "ad")
      .normalize("NFKD")
      .replace(/[^\p{L}\p{N}._-]+/gu, "-")
      .replace(/^-+|-+$/gu, "")
      .slice(0, 80) || "ad";
  }

  function adraMediaDownloadItems(data) {
    const prefix = adraSafeFilename(`${data.pageName || "page"}-${data.id || "ad"}`);
    const videos = (data.videoUrls || []).map((url, index) => ({
      url,
      filename: `Ad-Library-Research/${prefix}-video-${index + 1}.mp4`
    }));
    const images = (data.imageUrls || []).map((url, index) => ({
      url,
      filename: `Ad-Library-Research/${prefix}-image-${index + 1}.jpg`
    }));
    return [...videos, ...images];
  }

  function adraFormattedCreativeContent(data) {
    const rows = [
      ["Primary Text", data.primaryText || data.adText || ""],
      ["Headline", data.headline || ""],
      ["Description", data.description || ""],
      ["Caption", data.caption || data.domain || ""],
      ["CTA", data.cta || ""],
      ["Landing URL", data.landingUrl || ""],
      ["Ad URL", data.id ? `https://www.facebook.com/ads/library/?id=${data.id}` : ""]
    ];
    return rows
      .filter(([_label, value]) => String(value || "").trim())
      .map(([label, value]) => `${label}:\n${value}`)
      .join("\n\n");
  }

  function adraTrackedPageSnapshot(pageId) {
    const connectedAds = Array.from(adraCardRecords.entries())
      .filter(([card, data]) =>
        card.isConnected !== false &&
        String(data.pageId || pageId) === String(pageId)
      )
      .map(([_card, data]) => ({
        id: data.id || "",
        pageId: data.pageId || pageId,
        pageName: data.pageName || "",
        adLibraryUrl: data.adLibraryUrl || (data.id ? `https://www.facebook.com/ads/library/?id=${data.id}` : ""),
        adText: data.adText || "",
        primaryText: data.primaryText || data.adText || "",
        headline: data.headline || "",
        description: data.description || "",
        caption: data.caption || data.domain || "",
        landingUrl: data.landingUrl || "",
        domain: data.domain || "",
        status: data.status || "",
        startedAt: data.startedAt || "",
        activeDays: data.activeDays || 0,
        cta: data.cta || "",
        pageLikes: data.pageLikes || 0,
        creativeAdsCount: data.creativeAdsCount || 0,
        mediaType: data.mediaType || "",
        previewImageUrl: data.previewImageUrl || data.imageUrls?.[0] || "",
        detectedAt: data.detectedAt || new Date().toISOString()
      }))
      .filter((ad, index, ads) => ad.id && ads.findIndex((item) => item.id === ad.id) === index);

    const activeAds = connectedAds.filter((ad) => ad.status === "ACTIVE");
    const pageName = activeAds.find((ad) => ad.pageName)?.pageName ||
      connectedAds.find((ad) => ad.pageName)?.pageName ||
      "صفحة غير معروفة";

    return {
      pageId,
      pageName,
      pageAdsUrl: adraBuildPageAdsUrl(pageId),
      activeAdsCount: activeAds.length,
      totalAdsCount: connectedAds.length,
      ads: connectedAds,
      source: "visible_ad_library_cards"
    };
  }

  function adraMatchesLandingUrl(url, settings = adraLandingFilterSettings) {
    const needle = String(settings?.text || "").trim();
    if (!needle) return true;
    const haystack = String(url || "");
    return settings.caseSensitive
      ? haystack.includes(needle)
      : haystack.toLocaleLowerCase().includes(needle.toLocaleLowerCase());
  }

  function adraDomainSuffixTokens(value) {
    return String(value || "")
      .split(/[\s,،]+/u)
      .map((token) => token.trim().toLocaleLowerCase())
      .filter(Boolean)
      .map((token) => token.startsWith(".") ? token : `.${token}`);
  }

  function adraMatchesDomainSuffix(domain, filterValue = adraDomainSuffixFilter) {
    const tokens = adraDomainSuffixTokens(filterValue);
    if (!tokens.length) return true;
    const normalizedDomain = String(domain || "").trim().toLocaleLowerCase().replace(/\.$/u, "");
    if (!normalizedDomain) return false;
    return tokens.some((token) => normalizedDomain.endsWith(token));
  }

  function adraMatchesMinPageLikes(pageLikes, min = adraMinPageLikes) {
    return !min || Number(pageLikes || 0) >= min;
  }

  function adraApplyLocalFilters() {
    adraCardRecords.forEach((data, card) => {
      if (card.isConnected === false) {
        adraCardRecords.delete(card);
        return;
      }
      const ctaMatches = !adraCtaFilters.size || adraCtaFilters.has(data.cta);
      const mediaMatches = !adraMediaFilter || data.mediaType === adraMediaFilter;
      const creativeAdsMatches =
        !adraCreativeAdsFilterCount ||
        Number(data.creativeAdsCount || 0) === adraCreativeAdsFilterCount;
      const platformMatches =
        !adraPlatformFilter ||
        data.platform === adraPlatformFilter ||
        data.platformPending === true;
      const landingMatches =
        !adraLandingFilterEnabled || adraMatchesLandingUrl(data.landingUrl);
      const domainSuffixMatches = adraMatchesDomainSuffix(data.domain);
      const likesMatch = adraMatchesMinPageLikes(data.pageLikes);
      const wrapper = card.parentElement;
      if (!wrapper) return;
      if (
        ctaMatches &&
        mediaMatches &&
        creativeAdsMatches &&
        platformMatches &&
        landingMatches &&
        domainSuffixMatches &&
        likesMatch
      ) {
        wrapper.removeAttribute("data-adra-local-filter-hidden");
      }
      else wrapper.setAttribute("data-adra-local-filter-hidden", "true");
    });
    adraSyncFilterChips();
  }

  function adraSyncFilterChips() {
    const refs = adraFilterPanelRefs;
    if (!refs) return;

    const chips = [];
    if (adraMediaFilter) chips.push({ type: "media", value: adraMediaFilter, label: `Media: ${adraMediaFilter}` });
    if (adraPlatformFilter) chips.push({ type: "platform", value: adraPlatformFilter, label: `Platform: ${adraPlatformFilter}` });
    adraCtaFilters.forEach((value) => chips.push({ type: "cta", value, label: `CTA: ${value}` }));
    if (adraCreativeAdsFilterCount > 0) chips.push({ type: "creative", value: adraCreativeAdsFilterCount, label: `Creative: ${adraCreativeAdsFilterCount}` });
    if (adraMinPageLikes > 0) chips.push({ type: "likes", value: adraMinPageLikes, label: `Likes ≥ ${adraMinPageLikes}` });
    if (adraDomainSuffixFilter) chips.push({ type: "domain", value: adraDomainSuffixFilter, label: `Domain: ${adraDomainSuffixFilter}` });
    if (adraLandingFilterEnabled) chips.push({ type: "landing", value: "", label: "My Filter" });

    refs.chipsRow.replaceChildren();
    chips.forEach((chip) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "adra-chip";
      button.dataset.adraChip = chip.type;
      button.append(document.createTextNode(chip.label));
      const removeGlyph = document.createElement("span");
      removeGlyph.className = "adra-chip-remove";
      removeGlyph.setAttribute("aria-hidden", "true");
      removeGlyph.textContent = "×";
      button.append(removeGlyph);
      button.addEventListener("click", () => adraChipRemove(chip.type, chip.value));
      refs.chipsRow.append(button);
    });

    if (chips.length) {
      const clearAll = document.createElement("button");
      clearAll.type = "button";
      clearAll.className = "adra-chip adra-chip-clear-all";
      clearAll.textContent = "Clear all";
      clearAll.addEventListener("click", () => adraClearAllFilters());
      refs.chipsRow.append(clearAll);
    }

    refs.chipsRow.hidden = chips.length === 0;

    const secondaryCount =
      (adraMediaFilter ? 1 : 0) +
      (adraPlatformFilter ? 1 : 0) +
      adraCtaFilters.size +
      (adraCreativeAdsFilterCount > 0 ? 1 : 0) +
      (adraMinPageLikes > 0 ? 1 : 0) +
      (adraDomainSuffixFilter ? 1 : 0);
    refs.moreFiltersSummary.textContent = secondaryCount
      ? `More filters (${secondaryCount})`
      : "More filters";
  }

  function adraChipRemove(type, value) {
    const refs = adraFilterPanelRefs;
    if (!refs) return;
    switch (type) {
      case "media":
        refs.mediaSelect.value = "";
        adraMediaFilter = "";
        break;
      case "platform":
        refs.platformSelect.value = "";
        adraPlatformFilter = "";
        adraRefreshPlatformDetections();
        break;
      case "cta":
        adraCtaFilters.delete(value);
        refs.ctaMenu.querySelectorAll("input").forEach((checkbox) => {
          if (checkbox.value === value) checkbox.checked = false;
        });
        refs.updateCtaSummary(refs.ctaSummary);
        break;
      case "creative":
        refs.creativeInput.value = "";
        adraCreativeAdsFilterCount = 0;
        break;
      case "likes":
        refs.likesInput.value = "";
        adraMinPageLikes = 0;
        break;
      case "domain":
        refs.domainInput.value = "";
        adraDomainSuffixFilter = "";
        break;
      case "landing":
        refs.myFilterCheckbox.checked = false;
        adraLandingFilterEnabled = false;
        break;
      default:
        return;
    }
    adraApplyLocalFilters();
  }

  function adraClearAllFilters() {
    const refs = adraFilterPanelRefs;
    if (!refs) return;
    refs.mediaSelect.value = "";
    adraMediaFilter = "";
    refs.platformSelect.value = "";
    adraPlatformFilter = "";
    adraCtaFilters.clear();
    refs.ctaMenu.querySelectorAll("input").forEach((checkbox) => {
      checkbox.checked = false;
    });
    refs.updateCtaSummary(refs.ctaSummary);
    refs.creativeInput.value = "";
    adraCreativeAdsFilterCount = 0;
    refs.likesInput.value = "";
    adraMinPageLikes = 0;
    refs.domainInput.value = "";
    adraDomainSuffixFilter = "";
    refs.myFilterCheckbox.checked = false;
    adraLandingFilterEnabled = false;
    adraApplyLocalFilters();
  }

  function adraUpdatePlatformBadge(toolbar, platformLabel) {
    const badge = toolbar.querySelector('[data-adra-insight="platform"]');
    if (badge) badge.textContent = `Platform: ${platformLabel || "—"}`;
  }

  function adraRefreshPlatformDetections() {
    adraCardRecords.forEach((data, card) => {
      if (!adraPlatformFilter || card.isConnected === false || data.platformPending || !data.landingUrl) {
        return;
      }
      if (data.platform !== adraPlatformFilter && data.platformChecked !== true) {
        const toolbar = adraCardToolbars.get(card);
        if (toolbar) adraDetectCardPlatform(card, toolbar, data);
      }
    });
  }

  async function adraDetectCardPlatform(card, toolbar, data) {
    if (!data.landingUrl) {
      data.platform = "";
      data.platformLabel = "";
      data.platformPending = false;
      data.platformChecked = true;
      adraUpdatePlatformBadge(toolbar, "");
      adraApplyLocalFilters();
      return;
    }
    data.platformPending = true;
    adraCardRecords.set(card, data);
    const response = await adraRuntimeMessage({
      type: "ADRA_DETECT_PLATFORM",
      landingUrl: data.landingUrl
    });
    data.platformPending = false;
    data.platformChecked = true;
    if (!response.ok) {
      adraCardRecords.set(card, data);
      adraUpdatePlatformBadge(toolbar, "");
      adraApplyLocalFilters();
      return;
    }
    data.platform = response.platform || "";
    data.platformLabel = response.label || "";
    adraCardRecords.set(card, data);
    adraUpdatePlatformBadge(toolbar, data.platformLabel);
    adraApplyLocalFilters();
  }

  function adraLoadLandingFilterSettings() {
    chrome.storage.local.get(ADRA_LANDING_FILTER_KEY, (result) => {
      if (chrome.runtime.lastError) return;
      const settings = result[ADRA_LANDING_FILTER_KEY];
      if (settings && typeof settings === "object") {
        adraLandingFilterSettings = {
          text: String(settings.text || ""),
          caseSensitive: Boolean(settings.caseSensitive)
        };
      }
      const checkbox = document.querySelector(".adra-my-filter-checkbox");
      if (checkbox) {
        checkbox.disabled = !adraLandingFilterSettings.text.trim();
        if (checkbox.disabled) {
          checkbox.checked = false;
          adraLandingFilterEnabled = false;
        }
      }
      adraApplyLocalFilters();
    });
  }

  function adraLoadMinPageLikesSettings() {
    chrome.storage.local.get(ADRA_MIN_PAGE_LIKES_KEY, (result) => {
      if (chrome.runtime.lastError) return;
      const raw = result[ADRA_MIN_PAGE_LIKES_KEY];
      const next = Number.isFinite(Number(raw)) && Number(raw) >= 0 ? Math.floor(Number(raw)) : 0;
      adraMinPageLikes = next;
      const input = document.querySelector(".adra-min-page-likes-input");
      if (input) input.value = next ? String(next) : "";
      adraApplyLocalFilters();
    });
  }

  function adraMountToolbar(card) {
    if (adraCardToolbars.has(card)) return;
    const data = globalThis.adraExtractor?.extractAdData(card) || {};
    card.classList.toggle("adra-creative-group-card", Number(data.creativeAdsCount) > 1);
    adraCardRecords.set(card, data);
    const toolbar = document.createElement("div");
    toolbar.className = "adra-toolbar";
    toolbar.dir = "rtl";
    const save = adraButton("حفظ الإعلان", "adra-save-button", ADRA_ICON_SAVE);
    const copy = adraButton("نسخ النص", "adra-copy-button", ADRA_ICON_COPY);
    const copyContent = adraButton("نسخ المحتوى", "adra-copy-content-button", ADRA_ICON_COPY);
    const openPageAds = adraButton("فتح الإعلانات", "adra-open-page-ads-button", ADRA_ICON_OPEN);
    const mediaItems = adraMediaDownloadItems(data);
    const downloadMedia = adraButton(
      `تنزيل الوسائط${mediaItems.length ? ` (${mediaItems.length})` : ""}`,
      "adra-download-media-button",
      ADRA_ICON_DOWNLOAD
    );
    downloadMedia.disabled = mediaItems.length === 0;
    const insights = document.createElement("div");
    insights.className = "adra-insights";
    const insightValues = [
      data.status === "ACTIVE"
        ? `Active ${data.activeDays} ${data.activeDays === 1 ? "day" : "days"}`
        : "Active: —",
      `CTA: ${data.cta || "—"}`,
      `Page likes: ${data.pageLikes ? data.pageLikes.toLocaleString("en-US") : "—"}`,
      `Media: ${data.mediaType || "—"}${mediaItems.length ? ` (${mediaItems.length})` : ""}`,
      `Creative ads: ${data.creativeAdsCount ? data.creativeAdsCount.toLocaleString("en-US") : "—"}`,
      `Platform: ${data.platformLabel || "…"}`
    ];
    insightValues.forEach((value, index) => {
      const item = document.createElement("span");
      item.className = "adra-insight";
      if (index === insightValues.length - 1) item.dataset.adraInsight = "platform";
      item.textContent = value;
      insights.append(item);
    });
    const status = document.createElement("span");
    status.className = "adra-status";
    const pageAdsUrl = adraBuildPageAdsUrl(data.pageId);
    openPageAds.disabled = !pageAdsUrl;
    save.addEventListener("click", async () => {
      try {
        const result = await globalThis.adraStorage.saveAd(data);
        save.querySelector(".adra-button-label").textContent = "محفوظ";
        save.disabled = true;
        const baseMessage = result.duplicate ? "محفوظ مسبقًا" : "تم الحفظ";
        adraSetStatus(status, baseMessage);
        // Cloud sync is a non-blocking layer on top of the local save: it can
        // only refine the status message, never undo or block the save.
        if (result.saved) {
          adraCloudSyncSaveAd(data).then((cloud) => {
            if (!cloud || cloud.skipped) return; // not configured -> keep base message
            if (cloud.ok) adraSetStatus(status, "تم الحفظ والمزامنة");
            else adraSetStatus(status, "تم الحفظ محليًا — تعذر مزامنة السحابة", true);
          });
        }
      } catch (_error) { adraSetStatus(status, "تعذر حفظ الإعلان", true); }
    });
    copy.addEventListener("click", async () => {
      if (!data.adText) return adraSetStatus(status, "لم يتم العثور على نص", true);
      try {
        await navigator.clipboard.writeText(data.adText);
        adraSetStatus(status, "تم النسخ");
      } catch (_error) { adraSetStatus(status, "تعذر نسخ النص", true); }
    });
    copyContent.addEventListener("click", async () => {
      const content = adraFormattedCreativeContent(data);
      if (!content) return adraSetStatus(status, "لم يتم العثور على محتوى", true);
      try {
        await navigator.clipboard.writeText(content);
        adraSetStatus(status, "تم نسخ المحتوى");
      } catch (_error) { adraSetStatus(status, "تعذر نسخ المحتوى", true); }
    });
    openPageAds.addEventListener("click", () => {
      if (pageAdsUrl) window.open(pageAdsUrl, "_blank", "noopener,noreferrer");
    });
    downloadMedia.addEventListener("click", () => {
      if (!mediaItems.length) return;
      downloadMedia.disabled = true;
      chrome.runtime.sendMessage({ type: "ADRA_DOWNLOAD_MEDIA", items: mediaItems }, (response) => {
        const error = chrome.runtime.lastError;
        downloadMedia.disabled = false;
        if (error || !response?.ok) {
          adraSetStatus(status, "تعذر تنزيل الوسائط", true);
          return;
        }
        adraSetStatus(status, `بدأ تنزيل ${response.downloaded} ملف`);
      });
    });
    toolbar.append(insights, save, copy, copyContent, openPageAds, downloadMedia, status);
    toolbar.dataset.adraAdId = data.id || "";

    // Facebook cards use internal flex ordering, so prepending inside the card
    // can render the toolbar below the header. Insert it immediately before the
    // card inside its one-card wrapper to guarantee the visual top position.
    if (card.parentElement) {
      while (card.previousElementSibling?.classList?.contains("adra-toolbar")) {
        card.previousElementSibling.remove();
      }
      card.parentElement.insertBefore(toolbar, card);
    } else {
      card.querySelectorAll?.(":scope > .adra-toolbar").forEach((element) => element.remove());
      card.prepend(toolbar);
    }
    adraCardToolbars.set(card, toolbar);
    adraDetectCardPlatform(card, toolbar, data);
    adraApplyLocalFilters();
  }

  function adraRefreshCardsForIds(ids) {
    ids.forEach((id) => {
      const card = adraProcessedCardIds.get(String(id));
      if (!card || card.isConnected === false) return;
      adraCardToolbars.get(card)?.remove();
      adraCardToolbars.delete(card);
      adraCardRecords.delete(card);
      adraMountToolbar(card);
    });
  }

  function adraDateForDaysAgo(days) {
    const date = new Date();
    date.setHours(12, 0, 0, 0);
    date.setDate(date.getDate() - days);
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function adraCurrentDateFilterDays(href = globalThis.location.href) {
    const currentUrl = new URL(href);
    const maximumDate = currentUrl.searchParams.get("start_date[max]");
    if (!maximumDate) return 0;
    return [3, 7, 14, 21].find((days) => adraDateForDaysAgo(days) === maximumDate) || 0;
  }

  function adraBuildDateFilterUrl(href, days) {
    const url = new URL(href);
    const isAlreadyActive = adraCurrentDateFilterDays(href) === days;
    if (isAlreadyActive) {
      url.searchParams.delete("start_date[max]");
      url.searchParams.delete("start_date[min]");
    } else {
      url.searchParams.set("start_date[max]", adraDateForDaysAgo(days));
      url.searchParams.set("start_date[min]", "");
    }
    return url.href;
  }

  function adraNavigateWithDateFilter(days) {
    globalThis.location.assign(adraBuildDateFilterUrl(globalThis.location.href, days));
  }

  function adraMountDateFilters() {
    if (typeof document === "undefined" || document.querySelector(".adra-date-filters")) return;
    const filterControl = Array.from(document.querySelectorAll("div,span"))
      .filter((element) => /^(?:Filters|الفلاتر)$/iu.test((element.textContent || "").trim()))
      .map((element) => element.closest('[role="button"]'))
      .find(Boolean);
    let controlsRow = null;
    let filterColumn = null;
    let current = filterControl;
    for (let depth = 0; current?.parentElement && depth < 6; depth += 1) {
      const parent = current.parentElement;
      const children = Array.from(parent.children);
      const filterChild = children.find((child) => child.contains(filterControl));
      const sortChild = children.find((child) =>
        child !== filterChild &&
        (child.querySelector('[role="combobox"]') || /Sort|ترتيب/iu.test(child.textContent || ""))
      );
      if (filterChild && sortChild) {
        controlsRow = parent;
        filterColumn = filterChild;
        break;
      }
      current = parent;
    }

    const panel = document.createElement("div");
    panel.className = "adra-date-filters";
    panel.dir = "ltr";
    panel.setAttribute("aria-label", "فلترة الإعلانات حسب مدة النشاط");

    const primaryBar = document.createElement("div");
    primaryBar.className = "adra-filter-bar";

    const moreFilters = document.createElement("details");
    moreFilters.className = "adra-more-filters";
    const moreFiltersSummary = document.createElement("summary");
    moreFiltersSummary.className = "adra-more-filters-summary";
    moreFiltersSummary.textContent = "More filters";
    const moreFiltersMenu = document.createElement("div");
    moreFiltersMenu.className = "adra-more-filters-menu";
    moreFilters.append(moreFiltersSummary, moreFiltersMenu);

    const chipsRow = document.createElement("div");
    chipsRow.className = "adra-active-chips";
    chipsRow.hidden = true;

    const filterStatus = document.createElement("span");
    filterStatus.className = "adra-filter-status";
    const trackedPageId = adraCurrentTrackedPageId();
    if (/^\d+$/u.test(trackedPageId)) {
      const trackPageButton = document.createElement("button");
      trackPageButton.type = "button";
      trackPageButton.className = "adra-date-filter-button adra-track-page-button";
      trackPageButton.textContent = "تتبع الصفحة";
      trackPageButton.title = "حفظ الصفحة الحالية وكل الإعلانات الظاهرة للتتبع لاحقًا";
      trackPageButton.addEventListener("click", async () => {
        try {
          filterStatus.classList.remove("adra-status-error");
          const snapshot = adraTrackedPageSnapshot(trackedPageId);
          if (!snapshot.totalAdsCount) {
            filterStatus.textContent = "لم يتم العثور على إعلانات ظاهرة للحفظ";
            return;
          }
          const result = await globalThis.adraStorage.trackPage(snapshot);
          const baseMessage = result.updated
            ? `تم تحديث تتبع الصفحة (${snapshot.totalAdsCount} إعلان)`
            : `تم تتبع الصفحة (${snapshot.totalAdsCount} إعلان)`;
          filterStatus.textContent = baseMessage;
          // Non-blocking cloud sync after the local track succeeded.
          adraCloudSyncTrackPage(snapshot).then((cloud) => {
            if (!cloud || cloud.skipped) return;
            if (cloud.ok) {
              filterStatus.textContent = `${baseMessage} — تمت المزامنة`;
              filterStatus.classList.remove("adra-status-error");
            } else {
              filterStatus.textContent = `${baseMessage} — تعذر مزامنة السحابة`;
              filterStatus.classList.add("adra-status-error");
            }
          });
        } catch (_error) {
          filterStatus.textContent = "تعذر تتبع الصفحة";
          filterStatus.classList.add("adra-status-error");
        }
      });
      primaryBar.append(trackPageButton);

      // Phase 6 — Capture Snapshot (manual only; no auto-scan, no cron).
      const captureButton = document.createElement("button");
      captureButton.type = "button";
      captureButton.className = "adra-date-filter-button adra-capture-snapshot-button";
      captureButton.textContent = "تحديث الرصد";
      captureButton.title = "تسجيل لقطة من الإعلانات الظاهرة حاليًا وحفظها في ADRA Cloud";
      captureButton.addEventListener("click", async () => {
        if (captureButton.disabled) return;
        captureButton.disabled = true;
        const release = () => {
          captureButton.disabled = false;
        };
        try {
          filterStatus.classList.remove("adra-status-error");
          filterStatus.textContent = "جاري تجهيز الرصد...";
          const snapshot = adraTrackedPageSnapshot(trackedPageId);
          if (!snapshot.ads.length) {
            filterStatus.textContent = "لا توجد إعلانات ظاهرة للرصد";
            release();
            return;
          }
          filterStatus.textContent = `تم رصد ${snapshot.ads.length} إعلانًا ظاهرًا`;
          const scanSessionId =
            globalThis.crypto && typeof globalThis.crypto.randomUUID === "function"
              ? globalThis.crypto.randomUUID()
              : `adra-scan-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
          const result = await adraCloudSyncCapturePageSnapshot(snapshot, scanSessionId);
          if (!result || result.skipped) {
            filterStatus.textContent = "ADRA Cloud غير مهيأ";
            release();
            return;
          }
          if (result.ok) {
            const savedCount =
              result.data && Number.isFinite(Number(result.data.observed_ads_count))
                ? Number(result.data.observed_ads_count)
                : snapshot.ads.length;
            filterStatus.textContent = `تم حفظ الرصد: ${savedCount} إعلانًا`;
          } else {
            filterStatus.textContent = "تعذر حفظ الرصد";
            filterStatus.classList.add("adra-status-error");
          }
        } catch (_error) {
          filterStatus.textContent = "تعذر حفظ الرصد";
          filterStatus.classList.add("adra-status-error");
        } finally {
          release();
        }
      });
      primaryBar.append(captureButton);
    }

    [
      [21, "3 Weeks Active"],
      [14, "2 Weeks Active"],
      [7, "1 Week Active"],
      [3, "3 Days Active"]
    ].forEach(([days, label]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "adra-date-filter-button";
      button.dataset.adraDays = String(days);
      button.textContent = label;
      button.classList.toggle("adra-date-filter-active", days === adraCurrentDateFilterDays());
      button.addEventListener("click", () => {
        adraNavigateWithDateFilter(days);
      });
      primaryBar.append(button);
    });

    const myFilterLabel = document.createElement("label");
    myFilterLabel.className = "adra-my-filter";
    const myFilterCheckbox = document.createElement("input");
    myFilterCheckbox.type = "checkbox";
    myFilterCheckbox.className = "adra-my-filter-checkbox";
    myFilterCheckbox.disabled = !adraLandingFilterSettings.text.trim();
    const myFilterText = document.createElement("span");
    myFilterText.textContent = "My Filter";
    myFilterCheckbox.addEventListener("change", () => {
      adraLandingFilterEnabled = myFilterCheckbox.checked;
      adraApplyLocalFilters();
    });
    myFilterLabel.append(myFilterCheckbox, myFilterText);
    primaryBar.append(myFilterLabel);
    primaryBar.append(moreFilters);

    function addSelect(label, values, onChange) {
      const select = document.createElement("select");
      select.className = "adra-local-filter-select";
      select.setAttribute("aria-label", label);
      values.forEach(([value, text]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = text;
        select.append(option);
      });
      select.addEventListener("change", () => {
        onChange(select.value);
      });
      moreFiltersMenu.append(select);
      return select;
    }

    function updateCtaSummary(summary) {
      summary.textContent = adraCtaFilters.size
        ? `CTA (${adraCtaFilters.size})`
        : "All CTA";
    }

    function addCtaMultiSelect() {
      const details = document.createElement("details");
      details.className = "adra-multi-filter";
      const summary = document.createElement("summary");
      summary.setAttribute("aria-label", "فلترة حسب CTA");
      updateCtaSummary(summary);
      const menu = document.createElement("div");
      menu.className = "adra-multi-filter-menu";

      [
        ["SHOP_NOW", "CTA: SHOP_NOW"],
        ["ORDER_NOW", "CTA: ORDER_NOW"],
        ["LEARN_MORE", "CTA: LEARN_MORE"],
        ["NO_BUTTON", "CTA: NO_BUTTON"]
      ].forEach(([value, text]) => {
        const labelElement = document.createElement("label");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = value;
        const textElement = document.createElement("span");
        textElement.textContent = text;
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) {
            adraCtaFilters.add(value);
          } else {
            adraCtaFilters.delete(value);
          }
          updateCtaSummary(summary);
          adraApplyLocalFilters();
        });
        labelElement.append(checkbox, textElement);
        menu.append(labelElement);
      });

      const clear = document.createElement("button");
      clear.type = "button";
      clear.textContent = "All CTA";
      clear.addEventListener("click", () => {
        adraCtaFilters.clear();
        menu.querySelectorAll("input").forEach((checkbox) => {
          checkbox.checked = false;
        });
        updateCtaSummary(summary);
        adraApplyLocalFilters();
      });
      menu.append(clear);
      details.append(summary, menu);
      moreFiltersMenu.append(details);
      return { menu, summary };
    }

    const ctaControls = addCtaMultiSelect();
    const ctaMenu = ctaControls.menu;
    const ctaSummary = ctaControls.summary;

    const creativeFilter = document.createElement("label");
    creativeFilter.className = "adra-number-filter";
    creativeFilter.title = "يعرض فقط البطاقات التي تساوي هذا العدد في Creative ads";
    const creativeFilterText = document.createElement("span");
    creativeFilterText.textContent = "Creative ads:";
    const creativeFilterInput = document.createElement("input");
    creativeFilterInput.type = "number";
    creativeFilterInput.min = "1";
    creativeFilterInput.step = "1";
    creativeFilterInput.inputMode = "numeric";
    creativeFilterInput.placeholder = "Any";
    const creativeFilterClear = document.createElement("button");
    creativeFilterClear.type = "button";
    creativeFilterClear.textContent = "×";
    creativeFilterClear.title = "مسح فلتر Creative ads";
    creativeFilterInput.addEventListener("input", () => {
      const value = Number.parseInt(creativeFilterInput.value, 10);
      adraCreativeAdsFilterCount = Number.isFinite(value) && value > 0 ? value : 0;
      adraApplyLocalFilters();
    });
    creativeFilterClear.addEventListener("click", () => {
      creativeFilterInput.value = "";
      adraCreativeAdsFilterCount = 0;
      adraApplyLocalFilters();
    });
    creativeFilter.append(creativeFilterText, creativeFilterInput, creativeFilterClear);
    moreFiltersMenu.append(creativeFilter);

    const likesFilter = document.createElement("label");
    likesFilter.className = "adra-number-filter";
    likesFilter.title = "يعرض فقط البطاقات التي عدد إعجابات صفحتها أكبر من أو يساوي هذا الرقم";
    const likesFilterText = document.createElement("span");
    likesFilterText.textContent = "Min Page Likes:";
    const likesFilterInput = document.createElement("input");
    likesFilterInput.type = "number";
    likesFilterInput.min = "0";
    likesFilterInput.step = "1";
    likesFilterInput.inputMode = "numeric";
    likesFilterInput.className = "adra-min-page-likes-input";
    likesFilterInput.placeholder = "Any";
    likesFilterInput.value = adraMinPageLikes ? String(adraMinPageLikes) : "";
    const likesFilterClear = document.createElement("button");
    likesFilterClear.type = "button";
    likesFilterClear.textContent = "×";
    likesFilterClear.title = "مسح فلتر الحد الأدنى لإعجابات الصفحة";
    likesFilterInput.addEventListener("input", () => {
      const value = Number.parseInt(likesFilterInput.value, 10);
      adraMinPageLikes = Number.isFinite(value) && value >= 0 ? value : 0;
      adraApplyLocalFilters();
    });
    likesFilterClear.addEventListener("click", () => {
      likesFilterInput.value = "";
      adraMinPageLikes = 0;
      adraApplyLocalFilters();
    });
    likesFilter.append(likesFilterText, likesFilterInput, likesFilterClear);
    moreFiltersMenu.append(likesFilter);

    const domainFilter = document.createElement("label");
    domainFilter.className = "adra-text-filter";
    domainFilter.title = "فلترة حسب نهاية دومين رابط الإعلان، مثال: .shop أو .shop, .store";
    const domainFilterText = document.createElement("span");
    domainFilterText.textContent = "Domain:";
    const domainFilterInput = document.createElement("input");
    domainFilterInput.type = "text";
    domainFilterInput.placeholder = ".shop, .store";
    domainFilterInput.dir = "ltr";
    const domainFilterClear = document.createElement("button");
    domainFilterClear.type = "button";
    domainFilterClear.textContent = "×";
    domainFilterClear.title = "مسح فلتر الدومين";
    domainFilterInput.addEventListener("input", () => {
      adraDomainSuffixFilter = domainFilterInput.value.trim();
      adraApplyLocalFilters();
    });
    domainFilterClear.addEventListener("click", () => {
      domainFilterInput.value = "";
      adraDomainSuffixFilter = "";
      adraApplyLocalFilters();
    });
    domainFilter.append(domainFilterText, domainFilterInput, domainFilterClear);
    moreFiltersMenu.append(domainFilter);

    const mediaSelect = addSelect("فلترة حسب نوع الوسائط", [
      ["", "All Media"],
      ["VIDEO", "Media: VIDEO"],
      ["IMAGE", "Media: IMAGE"],
      ["CAROUSEL", "Media: CAROUSEL"],
      ["DPA", "Media: DPA"]
    ], (value) => {
      adraMediaFilter = value;
      adraApplyLocalFilters();
    });
    const platformSelect = addSelect("فلترة حسب المنصة", [
      ["", "All Platforms"],
      ["shopify", "Platform: Shopify"],
      ["foorweb", "Platform: Foorweb"],
      ["youcan", "Platform: YouCan"],
      ["flexdz", "Platform: FlexDZ"]
    ], (value) => {
      adraPlatformFilter = value;
      adraRefreshPlatformDetections();
      adraApplyLocalFilters();
    });
    const signature = document.createElement("span");
    signature.className = "adra-signature";
    signature.textContent = "Powered by Othmane Djoualil · Telegram: @ot2dz";
    primaryBar.append(filterStatus);
    primaryBar.append(signature);
    panel.append(primaryBar, chipsRow);

    if (controlsRow && filterColumn) {
      controlsRow.insertBefore(panel, filterColumn);
    } else {
      const resultsHeading = Array.from(document.querySelectorAll("h1,h2,h3,h4")).find((heading) =>
        /(?:~?\s*\d+\s+results?|نتائج|نتيجة)/iu.test(heading.innerText || heading.textContent || "")
      );
      if (!resultsHeading) return;
      resultsHeading.insertAdjacentElement("afterend", panel);
    }

    adraFilterPanelRefs = {
      chipsRow,
      mediaSelect,
      platformSelect,
      ctaMenu,
      ctaSummary,
      creativeInput: creativeFilterInput,
      likesInput: likesFilterInput,
      domainInput: domainFilterInput,
      myFilterCheckbox,
      moreFiltersSummary,
      updateCtaSummary
    };
    adraSyncFilterChips();
  }

  function adraProcessRoot(root) {
    const changedIds = globalThis.adraExtractor?.refreshEmbeddedAds?.() || [];
    adraRefreshCardsForIds(changedIds);
    findAdCards(root).forEach(adraProcessCard);
    adraMountDateFilters();
  }

  function adraStartDiscovery(targetDocument = document, Observer = MutationObserver) {
    const pendingRoots = new Set();
    let batchTimer = null;

    const flush = () => {
      batchTimer = null;
      const roots = Array.from(pendingRoots);
      pendingRoots.clear();
      roots.forEach(adraProcessRoot);
    };

    const schedule = (root) => {
      if (!root || adraIsExtensionElement(root)) {
        return;
      }

      pendingRoots.add(root);
      if (batchTimer === null) {
        batchTimer = globalThis.setTimeout(flush, ADRA_BATCH_DELAY_MS);
      }
    };

    adraProcessRoot(targetDocument);

    const observer = new Observer((mutations) => {
      mutations.forEach((mutation) => {
        Array.from(mutation.addedNodes || []).forEach((node) => {
          if (node.nodeType === 1) {
            schedule(node);
          }
        });
      });
    });

    const observationTarget = targetDocument.body || targetDocument.documentElement;
    if (observationTarget) {
      observer.observe(observationTarget, { childList: true, subtree: true });
    }

    return {
      disconnect() {
        observer.disconnect();
        if (batchTimer !== null) {
          globalThis.clearTimeout(batchTimer);
          batchTimer = null;
        }
        pendingRoots.clear();
      },
      flush
    };
  }

  const adraDiscoveryApi = Object.freeze({
    ADRA_CARD_SELECTORS,
    ADRA_PROCESSED_ATTRIBUTE,
    findAdCards,
    isLikelyAdCard: adraIsLikelyAdCard,
    processRoot: adraProcessRoot,
    startDiscovery: adraStartDiscovery,
    dateForDaysAgo: adraDateForDaysAgo,
    buildDateFilterUrl: adraBuildDateFilterUrl,
    buildPageAdsUrl: adraBuildPageAdsUrl,
    mediaDownloadItems: adraMediaDownloadItems,
    matchesLandingUrl: adraMatchesLandingUrl,
    matchesDomainSuffix: adraMatchesDomainSuffix,
    matchesMinPageLikes: adraMatchesMinPageLikes,
    hasCardDetails: adraHasCardDetails
  });

  // The prefixed API enables isolated tests and later extractor/UI integration.
  globalThis.adraContentDiscovery = adraDiscoveryApi;

  if (typeof document !== "undefined" && typeof MutationObserver !== "undefined") {
    adraStartDiscovery();
    adraLoadLandingFilterSettings();
    adraLoadMinPageLikesSettings();
    chrome.storage.onChanged.addListener((changes, areaName) => {
      if (areaName === "local" && changes[ADRA_LANDING_FILTER_KEY]) {
        adraLoadLandingFilterSettings();
      }
      if (areaName === "local" && changes[ADRA_MIN_PAGE_LIKES_KEY]) {
        adraLoadMinPageLikesSettings();
      }
    });
    document.addEventListener("ADRA_RECORDS_UPDATED", (event) => {
      adraRefreshCardsForIds(event.detail?.ids || []);
      adraApplyLocalFilters();
    });
  }
})();
