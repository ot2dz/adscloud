"use strict";

// Read a product's selling price (DZD) from its landing page HTML.
// Same rules as the platform's price_fetch.py, in the same order:
// JSON-LD offers → price meta tags → DZD price in the visible text (currency
// after or before the number) → "price" in the page's embedded product JSON.
// Pure functions: loaded by the background worker and by node tests.

(function (root) {
  const LO = 200;
  const HI = 500000;

  const LD_RE = /<script[^>]+application\/ld\+json[^>]*>([\s\S]*?)<\/script>/giu;
  const META_RES = [
    /<meta[^>]+(?:property|name|itemprop)=["'](?:product:price:amount|og:price:amount|price)["'][^>]*content=["']([\d.,\s]+)["']/iu,
    /<meta[^>]+content=["']([\d.,\s]+)["'][^>]*(?:property|name|itemprop)=["'](?:product:price:amount|og:price:amount|price)["']/iu,
    /itemprop=["']price["'][^>]*content=["']([\d.,\s]+)["']/iu
  ];
  const TAG_RE = /<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>|<[^>]+>/giu;
  const NUM = "(\\d{1,3}(?:[ .,\\u00a0\\u202f]\\d{3})+|\\d{3,6})";
  const DZD_AFTER_RE = new RegExp(
    "(?<![\\d.,])" + NUM + "(?:[.,]\\d{2})?\\s*(?:دج|د\\.ج|دينار|da\\b|dzd\\b|dz\\b|dinars?)", "giu");
  const DZD_BEFORE_RE = new RegExp(
    "(?:\\bDZD?\\b|\\bDA\\b|دج|د\\.ج)\\s*" + NUM + "(?:[.,]\\d{2})?(?!\\d)", "giu");
  const JSON_PRICE_RE = /"(?:price|sale_price|final_price)"\s*:\s*"?(\d{3,6}(?:\.\d{1,2})?)"?/giu;
  const CHALLENGE = ["just a moment", "cf-chl", "challenge-platform", "attention required",
    "enable javascript and cookies", "cf_chl_opt"];

  function num(v) {
    if (v === null || v === undefined) return null;
    let s = String(v).replace(/[^\d.,]/gu, "");
    if (!s) return null;
    if (/[.,]\d{2}$/u.test(s)) s = s.slice(0, -3);
    s = s.replace(/[.,]/gu, "");
    const x = parseInt(s, 10);
    return Number.isFinite(x) && x >= LO && x <= HI ? x : null;
  }

  function mostCommon(values) {
    const counts = new Map();
    for (const v of values) counts.set(v, (counts.get(v) || 0) + 1);
    let best = null;
    let bestN = 0;
    for (const [v, n] of counts) {
      if (n > bestN) { best = v; bestN = n; }
    }
    return best;
  }

  function fromLd(html) {
    const walk = (o) => {
      if (Array.isArray(o)) {
        for (const v of o) { const r = walk(v); if (r) return r; }
      } else if (o && typeof o === "object") {
        for (const k of ["price", "lowPrice"]) {
          if (k in o) { const v = num(o[k]); if (v) return v; }
        }
        for (const v of Object.values(o)) { const r = walk(v); if (r) return r; }
      }
      return null;
    };
    for (const m of html.matchAll(LD_RE)) {
      try {
        const r = walk(JSON.parse(m[1].trim()));
        if (r) return r;
      } catch (_e) { /* not JSON */ }
    }
    return null;
  }

  function fromText(html) {
    const text = html.replace(TAG_RE, " ").replace(/&nbsp;/gu, " ");
    const found = [];
    for (const re of [DZD_AFTER_RE, DZD_BEFORE_RE]) {
      for (const m of text.matchAll(re)) { const v = num(m[1]); if (v) found.push(v); }
    }
    return found.length ? mostCommon(found) : null;
  }

  function fromEmbeddedJson(html) {
    const found = [];
    for (const m of html.matchAll(JSON_PRICE_RE)) {
      const v = num(m[1].split(".")[0]);
      if (v) found.push(v);
    }
    return found.length ? mostCommon(found) : null;
  }

  function extractPrice(html) {
    if (!html) return { price: null, how: "" };
    let v = fromLd(html);
    if (v) return { price: v, how: "json-ld" };
    for (const re of META_RES) {
      const m = html.match(re);
      if (m && num(m[1])) return { price: num(m[1]), how: "meta" };
    }
    v = fromText(html);
    if (v) return { price: v, how: "text" };
    v = fromEmbeddedJson(html);
    return v ? { price: v, how: "page-json" } : { price: null, how: "" };
  }

  function looksLikeChallenge(status, html) {
    if ([403, 429, 503].includes(status)) return true;
    const low = String(html || "").slice(0, 20000).toLowerCase();
    return CHALLENGE.some((c) => low.includes(c));
  }

  root.adraPrice = Object.freeze({ extractPrice, looksLikeChallenge, num });
})(typeof globalThis !== "undefined" ? globalThis : this);
