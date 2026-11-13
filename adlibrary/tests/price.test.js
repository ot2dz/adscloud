"use strict";
// Run with:  node --test adlibrary/tests/
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SRC = path.join(__dirname, "..", "src");
require(path.join(SRC, "price", "price_extract.js"));
const { extractPrice, looksLikeChallenge } = globalThis.adraPrice;

test("same rules as the server (price_fetch.py)", () => {
  assert.deepEqual(extractPrice('<script type="application/ld+json">{"@type":"Product","offers":[{"price":"2900.00"}]}</script>'),
    { price: 2900, how: "json-ld" });
  assert.deepEqual(extractPrice('<meta property="og:price:amount" content="4,500">'), { price: 4500, how: "meta" });
  assert.deepEqual(extractPrice("<b>3 900 دج</b> <s>5 900 دج</s> <span>3 900 DA</span>"), { price: 3900, how: "text" });
  assert.deepEqual(extractPrice("<p>nothing</p>"), { price: null, how: "" });
});

test("Lightfunnels: currency first and embedded product JSON", () => {
  const page = '<script type="application/ld+json">{"@type":"Product","name":"Screen"}</script>'
    + "<span>DZ 5,200.00</span> <p>DZ&nbsp;5,200</p>"
    + '<script>w={"price":5200,"compare_at_price":0,"order_bump":{"price":0}}</script>';
  assert.deepEqual(extractPrice(page), { price: 5200, how: "text" });
  assert.deepEqual(extractPrice('<script>p={"price":"3900.00","compare_at_price":0}</script>'),
    { price: 3900, how: "page-json" });
});

test("bot-check pages are reported, not worked around", () => {
  assert.equal(looksLikeChallenge(403, ""), true);
  assert.equal(looksLikeChallenge(200, "<title>Just a moment...</title>"), true);
  assert.equal(looksLikeChallenge(200, "<h1>Product</h1>"), false);
});

// Any chrome.* API: objects all the way down, every call a no-op.
function stubChrome(listeners) {
  const any = () => new Proxy(function () {}, {
    get: (_t, k) => (k === "addListener" ? (fn) => listeners.push(fn) : any()),
    apply: () => undefined
  });
  return new Proxy({}, {
    get: (_t, k) => (k === "runtime"
      ? new Proxy({ id: "ext-id", lastError: null }, { get: (t, kk) => (kk in t ? t[kk] : any()) })
      : any())
  });
}

function loadBackground(fakeFetch) {
  const listeners = [];
  const ctx = {
    console, URL, AbortController, setTimeout, clearTimeout, Promise, JSON, Math, Number, String,
    fetch: fakeFetch,
    chrome: stubChrome(listeners),
    importScripts: (...files) => {
      for (const f of files) {
        vm.runInContext(fs.readFileSync(path.join(SRC, "background", f), "utf8"), ctx);
      }
    }
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(SRC, "background", "background.js"), "utf8"), ctx);
  return { bg: ctx.adraBackground, listeners };
}

test("background fetches one page at a time and reads each price", async () => {
  const calls = [];
  const pages = {
    "https://s.dz/a": { status: 200, text: "<span>DZ 5,200</span>" },
    "https://s.dz/b": { status: 403, text: "<title>Just a moment...</title>" },
    "https://s.dz/c": { status: 200, text: "<p>no price</p>" }
  };
  const fakeFetch = async (url) => {
    calls.push(url);
    const p = pages[url] || { status: 404, text: "" };
    return { status: p.status, text: async () => p.text };
  };
  const { bg } = loadBackground(fakeFetch);
  const res = await bg.fetchPrices(["https://s.dz/a", "https://s.dz/b", "https://s.dz/c", "javascript:alert(1)"]);
  const plain = JSON.parse(JSON.stringify(res));  // results come from another vm context
  assert.deepEqual(plain.map((r) => [r.status, r.price]), [["ok", 5200], ["blocked", null], ["not_found", null]]);
  assert.deepEqual(calls, ["https://s.dz/a", "https://s.dz/b", "https://s.dz/c"]);  // one request each, no retries
});

test("only the platform's own pages may ask for prices", () => {
  const { bg } = loadBackground(async () => ({ status: 200, text: async () => "" }));
  assert.equal(bg.isPlatformSender({ id: "ext-id", url: "https://ads.botdz.com/" }), true);
  assert.equal(bg.isPlatformSender({ id: "ext-id", url: "http://localhost:5002/x" }), true);
  assert.equal(bg.isPlatformSender({ id: "ext-id", url: "https://evil.example/" }), false);
  assert.equal(bg.isPlatformSender({ id: "other", url: "https://ads.botdz.com/" }), false);
});
