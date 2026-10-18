"use strict";

(() => {
  const adraEmbeddedAds = new Map();
  const adraParsedScripts = new WeakMap();
  const ADRA_METADATA_LINES = [
    /^active$/iu,
    /^inactive$/iu,
    /^library id:/iu,
    /^started running on/iu,
    /^platforms$/iu,
    /^open dropdown$/iu,
    /^see ad details$/iu,
    /^sponsored$/iu,
    /^نشط$/iu,
    /^معر[ّ]?ف المكتبة/iu
  ];

  function adraNormalizeText(value) {
    return String(value || "").replace(/[\u200B-\u200D\uFEFF]/gu, "").trim();
  }

  function adraUnwrapFacebookUrl(rawUrl) {
    try {
      const url = new URL(rawUrl, globalThis.location?.href);
      if (url.hostname === "l.facebook.com" && url.pathname === "/l.php") {
        return new URL(url.searchParams.get("u") || "").href;
      }
      return url.href;
    } catch (_error) {
      return "";
    }
  }

  function adraIsLandingUrl(rawUrl) {
    const value = adraUnwrapFacebookUrl(rawUrl);
    if (!value) return false;
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) &&
        !/(^|\.)facebook\.com$/iu.test(url.hostname) &&
        !/(^|\.)instagram\.com$/iu.test(url.hostname);
    } catch (_error) {
      return false;
    }
  }

  function adraLandingUrl(card) {
    return Array.from(card.querySelectorAll("a[href]"))
      .map((link) => adraUnwrapFacebookUrl(link.href))
      .find(adraIsLandingUrl) || "";
  }

  function adraPageName(card) {
    const link = Array.from(card.querySelectorAll("a[href]")).find((candidate) => {
      try {
        const url = new URL(candidate.href);
        const text = adraNormalizeText(candidate.innerText || candidate.textContent);
        return /(^|\.)facebook\.com$/iu.test(url.hostname) &&
          !url.pathname.startsWith("/ads/library") && Boolean(text);
      } catch (_error) {
        return false;
      }
    });
    return adraNormalizeText(link?.innerText || link?.textContent);
  }

  function adraAdText(card, pageName) {
    const lines = adraNormalizeText(card.innerText || card.textContent)
      .split(/\n+/u)
      .map(adraNormalizeText)
      .filter(Boolean);
    const sponsoredIndex = lines.findIndex((line) => /^sponsored$|^م[ُّ]?مو[ّ]?ل$/iu.test(line));
    const content = (sponsoredIndex >= 0 ? lines.slice(sponsoredIndex + 1) : lines)
      .filter((line) => line !== pageName)
      .filter((line) => !ADRA_METADATA_LINES.some((pattern) => pattern.test(line)))
      .filter((line) => !/^\d+:\d+\s*\/\s*\d+:\d+$/u.test(line));
    return content.join("\n").trim();
  }

  function adraLocalId(data) {
    const source = `${data.pageName}|${data.adText}|${data.landingUrl}`;
    let hash = 2166136261;
    for (let index = 0; index < source.length; index += 1) {
      hash ^= source.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return `local-${(hash >>> 0).toString(16)}`;
  }

  function adraParseStartedAt(text) {
    const match = text.match(/Started running on\s+([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})/iu);
    if (!match) return "";
    const timestamp = Date.parse(match[1]);
    return Number.isNaN(timestamp) ? "" : new Date(timestamp).toISOString();
  }

  function adraActiveDays(startedAt, status, currentDate = new Date()) {
    if (!startedAt || status !== "ACTIVE") return 0;
    const started = new Date(startedAt);
    if (Number.isNaN(started.getTime())) return 0;
    const startDay = Date.UTC(started.getFullYear(), started.getMonth(), started.getDate());
    const today = Date.UTC(
      currentDate.getFullYear(),
      currentDate.getMonth(),
      currentDate.getDate()
    );
    return Math.max(0, Math.floor((today - startDay) / 86400000));
  }

  function adraCta(card) {
    const knownLabels = [
      "Shop now", "Order now", "Learn more", "Sign up", "Book now",
      "Contact us", "Send message", "Get offer", "Apply now", "Download",
      "اشتر الآن", "اطلب الآن", "معرفة المزيد", "إرسال رسالة"
    ];
    const buttons = Array.from(card.querySelectorAll("button"));
    const label = knownLabels.find((candidate) =>
      buttons.some((button) => adraNormalizeText(button.innerText || button.textContent) === candidate)
    );
    if (!label) return "";
    const arabicMap = {
      "اشتر الآن": "SHOP_NOW",
      "اطلب الآن": "ORDER_NOW",
      "معرفة المزيد": "LEARN_MORE",
      "إرسال رسالة": "SEND_MESSAGE"
    };
    return arabicMap[label] || label.toUpperCase().replace(/[^A-Z0-9]+/gu, "_").replace(/^_|_$/gu, "");
  }

  function adraCompactNumber(value) {
    const normalized = value.replace(/[\s,]/gu, "");
    const match = normalized.match(/^([\d.]+)([KMB])?$/iu);
    if (!match) return 0;
    const multiplier = { K: 1e3, M: 1e6, B: 1e9 }[(match[2] || "").toUpperCase()] || 1;
    return Math.round(Number.parseFloat(match[1]) * multiplier) || 0;
  }

  function adraPageLikes(card) {
    const localText = adraNormalizeText(card.innerText || card.textContent);
    const patterns = [
      /Page likes?\s*[:：]?\s*([\d.,]+\s*[KMB]?)/iu,
      /([\d.,]+\s*[KMB]?)\s+(?:people\s+like|likes?)/iu,
      /(?:إعجابات الصفحة|تسجيلات الإعجاب)\s*[:：]?\s*([\d.,]+\s*[KMB]?)/iu,
      /([\d.,]+\s*[KMB]?)\s+إعجاب/iu
    ];
    for (const pattern of patterns) {
      const match = localText.match(pattern);
      if (match) return adraCompactNumber(match[1]);
    }
    return 0;
  }

  function adraCreativeAdsCount(card) {
    const localText = adraNormalizeText(card.innerText || card.textContent);
    const patterns = [
      /([\d,.\s]+)\s+ads?\s+use\s+this\s+creative\s+and\s+text/iu,
      /([\d,.\s]+)\s+إعلانات?\s+تستخدم\s+(?:هذا|نفس)\s+(?:الإبداع|التصميم|النص)/iu
    ];
    for (const pattern of patterns) {
      const match = localText.match(pattern);
      if (!match) continue;
      const count = Number(String(match[1]).replace(/[^\d]/gu, ""));
      if (Number.isFinite(count) && count > 0) return count;
    }
    return 0;
  }

  function adraUniqueUrls(values) {
    return Array.from(new Set(values.filter((value) => {
      try {
        return ["http:", "https:"].includes(new URL(value).protocol);
      } catch (_error) {
        return false;
      }
    })));
  }

  function adraEmbeddedMedia(snapshot) {
    const videos = [];
    const images = [];
    const previews = [];
    (snapshot.videos || []).forEach((video) => {
      videos.push(video.video_hd_url || video.video_sd_url || "");
      previews.push(
        video.video_preview_image_url ||
        video.video_preview_image_uri ||
        video.preview_image_url ||
        video.thumbnail_url ||
        video.thumbnail_uri ||
        video.image_url ||
        ""
      );
    });
    (snapshot.images || []).forEach((image) => {
      images.push(image.original_image_url || image.resized_image_url || "");
    });
    (snapshot.cards || []).forEach((card) => {
      videos.push(card.video_hd_url || card.video_sd_url || "");
      images.push(card.original_image_url || card.resized_image_url || "");
      previews.push(
        card.video_preview_image_url ||
        card.video_preview_image_uri ||
        card.preview_image_url ||
        card.thumbnail_url ||
        card.thumbnail_uri ||
        card.image_url ||
        ""
      );
    });
    return {
      videoUrls: adraUniqueUrls(videos),
      imageUrls: adraUniqueUrls(images),
      previewImageUrl: adraUniqueUrls([...images, ...previews])[0] || ""
    };
  }

  function adraEmbeddedLandingUrl(snapshot) {
    const cardLink = (snapshot.cards || [])
      .map((card) => card.link_url || card.url || "")
      .find(Boolean);
    return adraUnwrapFacebookUrl(cardLink || snapshot.link_url || snapshot.website_url || "");
  }

  function adraEmbeddedTextField(...values) {
    for (const value of values) {
      if (typeof value === "string") {
        const text = adraNormalizeText(value);
        if (text) return text;
      }
      if (value && typeof value === "object") {
        const text = adraNormalizeText(value.text || value.value || value.__html || "");
        if (text) return text;
      }
    }
    return "";
  }

  function adraFirstCardText(snapshot, ...keys) {
    for (const card of snapshot.cards || []) {
      for (const key of keys) {
        const text = adraEmbeddedTextField(card?.[key]);
        if (text) return text;
      }
    }
    return "";
  }

  function adraEmbeddedCreativeContent(snapshot) {
    return {
      primaryText: adraEmbeddedTextField(
        snapshot.body,
        snapshot.body_text,
        snapshot.message,
        snapshot.text
      ) || adraFirstCardText(snapshot, "body", "body_text", "text"),
      headline: adraEmbeddedTextField(
        snapshot.title,
        snapshot.headline,
        snapshot.link_title
      ) || adraFirstCardText(snapshot, "title", "headline", "link_title"),
      description: adraEmbeddedTextField(
        snapshot.link_description,
        snapshot.description,
        snapshot.caption?.description
      ) || adraFirstCardText(snapshot, "link_description", "description"),
      caption: adraEmbeddedTextField(
        snapshot.caption,
        snapshot.link_caption,
        snapshot.display_url
      ) || adraFirstCardText(snapshot, "caption", "link_caption", "display_url")
    };
  }

  function adraEmbeddedPageId(value) {
    const visited = new WeakSet();
    const directKeys = new Set([
      "page_id",
      "pageId",
      "pageID",
      "page_profile_id",
      "pageProfileId",
      "pageProfileID",
      "view_all_page_id"
    ]);

    function normalize(candidate) {
      const text = String(candidate ?? "").trim();
      return /^\d+$/u.test(text) ? text : "";
    }

    function search(node) {
      if (!node || typeof node !== "object") return "";
      if (visited.has(node)) return "";
      visited.add(node);

      for (const key of directKeys) {
        const direct = normalize(node[key]);
        if (direct) return direct;
      }

      if (node.page && typeof node.page === "object") {
        const pageId = normalize(node.page.id);
        if (pageId) return pageId;
      }

      for (const child of Object.values(node)) {
        const nested = search(child);
        if (nested) return nested;
      }
      return "";
    }

    return search(value);
  }

  function adraIndexEmbeddedValue(value, changedIds) {
    if (!value || typeof value !== "object") return;

    if (
      !Array.isArray(value) &&
      value.ad_archive_id != null &&
      value.snapshot &&
      typeof value.snapshot === "object"
    ) {
      const snapshot = value.snapshot;
      const id = String(value.ad_archive_id);
      const media = adraEmbeddedMedia(snapshot);
      const creativeContent = adraEmbeddedCreativeContent(snapshot);
      adraEmbeddedAds.set(id, {
        id,
        pageId: adraEmbeddedPageId(value) || adraEmbeddedPageId(snapshot),
        pageName: adraNormalizeText(snapshot.page_name),
        pageLikes: Number.isFinite(snapshot.page_like_count) ? snapshot.page_like_count : 0,
        cta: adraNormalizeText(snapshot.cta_type),
        mediaType: adraNormalizeText(snapshot.display_format) ||
          (media.videoUrls.length ? "VIDEO" : media.imageUrls.length ? "IMAGE" : ""),
        videoUrls: media.videoUrls,
        imageUrls: media.imageUrls,
        previewImageUrl: media.previewImageUrl,
        primaryText: creativeContent.primaryText,
        headline: creativeContent.headline,
        description: creativeContent.description,
        caption: creativeContent.caption,
        landingUrl: adraEmbeddedLandingUrl(snapshot),
        status: value.is_active === true ? "ACTIVE" : value.is_active === false ? "INACTIVE" : "",
        startedAt: Number.isFinite(value.start_date)
          ? new Date(value.start_date * 1000).toISOString()
          : ""
      });
      changedIds.add(id);
    }

    Object.values(value).forEach((child) => adraIndexEmbeddedValue(child, changedIds));
  }

  function adraIndexEmbeddedRecords(records = []) {
    const changedIds = new Set();
    records.forEach((record) => adraIndexEmbeddedValue(record, changedIds));
    return Array.from(changedIds);
  }

  function adraRefreshEmbeddedAds() {
    const changedIds = new Set();
    if (!globalThis.document?.querySelectorAll) return [];
    const scripts = globalThis.document.querySelectorAll('script[type="application/json"]');
    Array.from(scripts).forEach((script) => {
      const json = script.textContent?.trim();
      if (!json) return;
      if (adraParsedScripts.get(script) === json.length) return;
      adraParsedScripts.set(script, json.length);
      try {
        adraIndexEmbeddedValue(JSON.parse(json), changedIds);
      } catch (_error) {
        // Facebook also uses unrelated JSON scripts; malformed blocks are ignored.
      }
    });
    return Array.from(changedIds);
  }

  function adraCardImageFromDom(card) {
    if (!card || typeof card.querySelectorAll !== "function") return "";
    const video = card.querySelector("video[poster]");
    if (video?.poster && video.poster.startsWith("http")) return video.poster;
    const images = Array.from(card.querySelectorAll("img[src]"));
    for (const img of images) {
      const src = img.src || "";
      if (!src.startsWith("http")) continue;
      if (src.includes("rsrc.php") || src.includes("static.xx") || src.includes("emoji.php")) continue;
      if (src.includes("scontent") || img.naturalWidth > 120 || img.clientWidth > 120) {
        return src;
      }
    }
    const scontent = images.find((img) => img.src && img.src.includes("scontent"));
    return scontent?.src || "";
  }

  function extractAdData(card) {
    const fullText = adraNormalizeText(card.innerText || card.textContent);
    const idMatch = fullText.match(/(?:Library ID|معر[ّ]?ف المكتبة)\s*:\s*(\d+)/iu);
    const visibleId = idMatch?.[1] || "";
    adraRefreshEmbeddedAds();
    const embedded = visibleId ? adraEmbeddedAds.get(visibleId) : null;
    const landingUrl = embedded?.landingUrl || adraLandingUrl(card);
    const pageName = embedded?.pageName || adraPageName(card);
    const adText = adraAdText(card, pageName);
    let domain = "";
    try { domain = landingUrl ? new URL(landingUrl).hostname.replace(/^www\./iu, "") : ""; } catch (_error) {}
    const status = embedded?.status ||
      (/(?:^|\n)\s*(?:Active|نشط)\s*(?:\n|$)/iu.test(fullText) ? "ACTIVE" : "");
    const startedAt = embedded?.startedAt || adraParseStartedAt(fullText);
    const domImage = adraCardImageFromDom(card);
    const data = {
      id: visibleId,
      pageId: embedded?.pageId || "",
      pageName,
      adText,
      primaryText: embedded?.primaryText || adText,
      headline: embedded?.headline || "",
      description: embedded?.description || "",
      caption: embedded?.caption || domain,
      landingUrl,
      domain,
      status,
      startedAt,
      activeDays: adraActiveDays(startedAt, status),
      cta: embedded?.cta || adraCta(card),
      pageLikes: embedded?.pageLikes || adraPageLikes(card),
      creativeAdsCount: adraCreativeAdsCount(card),
      mediaType: embedded?.mediaType || "",
      videoUrls: embedded?.videoUrls || [],
      imageUrls: embedded?.imageUrls || [],
      previewImageUrl: embedded?.previewImageUrl || embedded?.imageUrls?.[0] || domImage,
      detectedAt: new Date().toISOString()
    };
    if (!data.id) data.id = adraLocalId(data);
    return data;
  }

  globalThis.adraExtractor = Object.freeze({
    activeDaysBetween: adraActiveDays,
    extractAdData,
    indexEmbeddedRecords: adraIndexEmbeddedRecords,
    refreshEmbeddedAds: adraRefreshEmbeddedAds,
    unwrapFacebookUrl: adraUnwrapFacebookUrl
  });

  if (typeof document !== "undefined") {
    document.addEventListener("ADRA_PAGE_RECORDS", (event) => {
      const changedIds = adraIndexEmbeddedRecords(event.detail?.records || []);
      if (changedIds.length) {
        document.dispatchEvent(new CustomEvent("ADRA_RECORDS_UPDATED", { detail: { ids: changedIds } }));
      }
    });
  }
})();
