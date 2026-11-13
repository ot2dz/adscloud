"use strict";

// Runs only on the AdsCloud platform pages (see manifest "matches").
// Lets the platform ask this extension to read competitor prices from the
// user's own browser. The page talks with window.postMessage; only messages
// from this same window and origin are accepted, and only URL lists go out.

(function () {
  const PAGE = "adscloud-page";
  const EXT = "adscloud-ext";
  const version = chrome.runtime.getManifest().version;

  function reply(data) {
    window.postMessage(Object.assign({ source: EXT, version }, data), window.location.origin);
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.origin !== window.location.origin) return;
    const msg = event.data;
    if (!msg || msg.source !== PAGE) return;

    if (msg.type === "ping") {
      reply({ type: "ready", features: ["prices"] });
      return;
    }
    if (msg.type === "fetch-prices" && Array.isArray(msg.urls)) {
      chrome.runtime.sendMessage({ type: "ADRA_FETCH_PRICES", urls: msg.urls.slice(0, 30) }, (res) => {
        const err = chrome.runtime.lastError;
        reply({
          type: "prices",
          id: msg.id,
          ok: !err && !!(res && res.ok),
          error: err ? err.message : (res && res.error) || "",
          results: (res && res.results) || []
        });
      });
    }
  });

  reply({ type: "ready", features: ["prices"] });
})();
