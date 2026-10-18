"use strict";

(() => {
  const ADRA_EVENT_NAME = "ADRA_PAGE_RECORDS";
  const adraSeenIds = new Set();

  function adraSafeParsePayload(text) {
    const values = [];
    if (!text) return values;
    try {
      values.push(JSON.parse(text));
      return values;
    } catch (_error) {
      String(text).split("\n").forEach((line) => {
        const trimmed = line.trim();
        if (!trimmed) return;
        try { values.push(JSON.parse(trimmed)); } catch (_ignored) {}
      });
      return values;
    }
  }

  function adraCollectRecords(value, records) {
    if (!value || typeof value !== "object") return;
    if (
      !Array.isArray(value) &&
      value.ad_archive_id != null &&
      value.snapshot &&
      typeof value.snapshot === "object"
    ) {
      const id = String(value.ad_archive_id);
      if (!adraSeenIds.has(id)) {
        adraSeenIds.add(id);
        records.push(value);
      }
    }
    Object.values(value).forEach((child) => adraCollectRecords(child, records));
  }

  function adraDispatchFromPayloads(payloads) {
    const records = [];
    payloads.forEach((payload) => adraCollectRecords(payload, records));
    if (!records.length) return;
    document.dispatchEvent(new CustomEvent(ADRA_EVENT_NAME, { detail: { records } }));
  }

  function adraScanEmbeddedScripts() {
    const scripts = document.querySelectorAll('script[type="application/json"]');
    Array.from(scripts).forEach((script) => {
      const text = script.textContent || "";
      if (!text.includes("ad_archive_id") && !text.includes("search_results_connection")) return;
      adraDispatchFromPayloads(adraSafeParsePayload(text));
    });
  }

  function adraHookXhr() {
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function open(method, url) {
      this.__adraUrl = String(url || "");
      return originalOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function send() {
      this.addEventListener("load", function onLoad() {
        if (this.status < 200 || this.status >= 300) return;
        const url = String(this.responseURL || this.__adraUrl || "");
        if (!url.includes("/api/graphql/")) return;
        adraDispatchFromPayloads(adraSafeParsePayload(this.responseText));
      });
      return originalSend.apply(this, arguments);
    };
  }

  function adraHookFetch() {
    const originalFetch = window.fetch;
    if (typeof originalFetch !== "function") return;
    window.fetch = async function fetchWithAdra(input, init) {
      const response = await originalFetch.apply(this, arguments);
      try {
        const url = typeof input === "string" ? input : input?.url;
        if (String(url || response.url || "").includes("/api/graphql/")) {
          response.clone().text().then((text) => {
            adraDispatchFromPayloads(adraSafeParsePayload(text));
          }).catch(() => {});
        }
      } catch (_error) {}
      return response;
    };
  }

  adraHookXhr();
  adraHookFetch();
  adraScanEmbeddedScripts();
  setInterval(adraScanEmbeddedScripts, 2000);
})();
