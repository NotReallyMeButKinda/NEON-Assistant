// NEON Assistant Bridge -- the browser half of browser_bridge.py. Runs in Firefox / Zen (Manifest V2,
// a background script) and in Chrome, Edge and Brave (Manifest V3, a service worker); build_extension.py
// makes both packages from these same files.
//
// NEON can't call into the browser, so this keeps one long-poll open to NEON on 127.0.0.1 ("anything
// for me?"). When NEON answers with a request ("the page you're on", "your tabs"), it is carried out
// here and the result is posted back. Nothing is sent anywhere else, and nothing is sent unless NEON
// asks for it.

"use strict";

// Firefox has `browser` (promises); Chrome has `chrome` (promises too, under Manifest V3).
const api = globalThis.browser ?? globalThis.chrome;
const toolbar = api.action ?? api.browserAction;      // MV3: action; MV2: browserAction

const DEFAULTS = { port: 47811, token: "" };
const MAX_TEXT = 60000;          // characters of page text sent for one page
// Pages no extension may read: the browsers' own pages and their add-on stores.
const UNREADABLE = /^(about:|moz-extension:|chrome-extension:|view-source:|chrome:|edge:|brave:|resource:|devtools:|https:\/\/addons\.mozilla\.org|https:\/\/chrome\.google\.com\/webstore|https:\/\/chromewebstore\.google\.com|https:\/\/microsoftedge\.microsoft\.com\/addons)/;

let state = "off";               // off | on | key (NEON is there but the token is wrong)
let running = false;             // the poll loop (a Chrome service worker can be stopped and restarted)

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function settings() {
  const stored = await api.storage.local.get(DEFAULTS);
  return { port: Number(stored.port) || DEFAULTS.port, token: String(stored.token || "").trim() };
}

function show(next) {
  if (next === state) return;
  state = next;
  const badge = { on: "", off: "off", key: "key" }[next];
  const title = {
    on: "NEON: connected",
    off: "NEON: not connected (is NEON running?)",
    key: "NEON: wrong token -- paste it from NEON's Settings > Browser",
  }[next];
  toolbar.setBadgeText({ text: badge });
  toolbar.setBadgeBackgroundColor({ color: next === "key" ? "#d9822b" : "#555555" });
  toolbar.setTitle({ title });
}

// ---- reading a page (runs inside the page) -----------------------------------------------------

function extractPage(maxChars) {
  const meta = (name) => {
    const el = document.querySelector(`meta[name="${name}"], meta[property="${name}"]`);
    return el ? String(el.content || "").trim() : "";
  };
  const selection = String(window.getSelection ? window.getSelection() : "").trim();
  const candidates = ["article", "main", "[role=main]", "#content", "#main", ".content"];
  let root = null;
  for (const sel of candidates) {
    const el = document.querySelector(sel);
    if (el && (el.innerText || "").trim().length > 400) { root = el; break; }
  }
  let text = ((root || document.body || {}).innerText || "").trim();
  if (root && document.body && text.length < 1500) text = (document.body.innerText || "").trim();
  text = text.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").slice(0, maxChars);

  const headings = Array.from(document.querySelectorAll("h1, h2, h3"))
    .map((h) => (h.innerText || "").trim()).filter(Boolean).slice(0, 40);

  const pick = (obj, key) => {
    const v = obj ? obj[key] : undefined;
    if (v === undefined || v === null) return undefined;
    if (Array.isArray(v)) return v.map((x) => (typeof x === "object" ? x.name || x["@id"] || "" : String(x)))
      .filter(Boolean).slice(0, 6).join(", ");
    if (typeof v === "object") return v.name || v["@value"] || undefined;
    return String(v).slice(0, 300);
  };
  const structured = [];
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    let data;
    try { data = JSON.parse(script.textContent); } catch (e) { continue; }
    const items = [].concat(Array.isArray(data) ? data : (data && data["@graph"]) || [data]);
    for (const item of items) {
      if (!item || typeof item !== "object") continue;
      const offers = Array.isArray(item.offers) ? item.offers[0] : item.offers;
      const rating = item.aggregateRating;
      const entry = {
        type: pick(item, "@type"), name: pick(item, "name") || pick(item, "headline"),
        author: pick(item, "author"), brand: pick(item, "brand"), director: pick(item, "director"),
        publisher: pick(item, "publisher"), datePublished: pick(item, "datePublished"),
        releaseDate: pick(item, "releaseDate"), startDate: pick(item, "startDate"),
        genre: pick(item, "genre"), platform: pick(item, "gamePlatform"), duration: pick(item, "duration"),
        price: offers ? pick(offers, "price") || pick(offers, "lowPrice") : undefined,
        currency: offers ? pick(offers, "priceCurrency") : undefined,
        availability: offers ? pick(offers, "availability") : undefined,
        rating: rating ? pick(rating, "ratingValue") : undefined,
        ratingCount: rating ? pick(rating, "ratingCount") || pick(rating, "reviewCount") : undefined,
        description: pick(item, "description"),
      };
      for (const k of Object.keys(entry)) if (entry[k] === undefined || entry[k] === "") delete entry[k];
      if (Object.keys(entry).length > 1) structured.push(entry);
      if (structured.length >= 12) break;
    }
  }
  return {
    title: document.title || "", url: location.href,
    description: meta("description") || meta("og:description"),
    selection: selection.slice(0, 20000), headings, text, structured,
    lang: document.documentElement.lang || "",
  };
}

function findOnPage(text) {
  window.getSelection().removeAllRanges();
  return window.find(String(text || ""), false, false, true);
}

// ---- carrying out NEON's requests --------------------------------------------------------------

async function activeTab() {
  const [tab] = await api.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("there's no open tab");
  return tab;
}

function tabInfo(tab) {
  return { id: tab.id, title: tab.title || "", url: tab.url || "", active: !!tab.active,
           windowId: tab.windowId, audible: !!tab.audible, pinned: !!tab.pinned };
}

// Runs `func(...args)` inside the tab and returns its result. `scripting` works in both browsers
// (Firefox 102+ and every Chrome with Manifest V3).
async function inTab(tab, func, args) {
  if (UNREADABLE.test(tab.url || "")) throw new Error("the browser doesn't let extensions read this page");
  const [result] = await api.scripting.executeScript({ target: { tabId: tab.id }, func, args });
  return result ? result.result : undefined;
}

const OPS = {
  async page() {
    return inTab(await activeTab(), extractPage, [MAX_TEXT]);
  },
  async selection() {
    const page = await inTab(await activeTab(), extractPage, [MAX_TEXT]);
    return { selection: page.selection, title: page.title, url: page.url };
  },
  async tabs() {
    const tabs = await api.tabs.query({});
    return tabs.map(tabInfo);
  },
  async activate({ id }) {
    const tab = await api.tabs.update(Number(id), { active: true });
    await api.windows.update(tab.windowId, { focused: true });
    return tabInfo(tab);
  },
  async close({ id }) {
    const tab = id ? await api.tabs.get(Number(id)) : await activeTab();
    await api.tabs.remove(tab.id);
    return tabInfo(tab);
  },
  async find({ text }) {
    const tab = await activeTab();
    const found = await inTab(tab, findOnPage, [String(text || "")]);
    return { found: !!found, title: tab.title };
  },
  async open({ url }) {
    const tab = await api.tabs.create({ url: String(url) });
    return tabInfo(tab);
  },
};

async function reply(cfg, body) {
  try {
    await fetch(`http://127.0.0.1:${cfg.port}/reply`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Neon-Token": cfg.token },
      body: JSON.stringify(body),
    });
  } catch (e) {
    // NEON went away between asking and answering; it will time out on its side.
  }
}

async function run(cfg, req) {
  const op = OPS[req.op];
  if (!op) {
    await reply(cfg, { id: req.id, ok: false, error: `unknown request ${req.op}` });
    return;
  }
  try {
    const data = await op(req.args || {});
    await reply(cfg, { id: req.id, ok: true, data });
  } catch (e) {
    await reply(cfg, { id: req.id, ok: false, error: String((e && e.message) || e) });
  }
}

// ---- the long-poll loop ------------------------------------------------------------------------

async function browserName() {
  try {
    if (api.runtime.getBrowserInfo) {                    // Firefox / Zen
      const info = await api.runtime.getBrowserInfo();
      return info.name || "Firefox";
    }
  } catch (e) { /* fall through */ }
  if (navigator.brave) return "Brave";
  const brands = (navigator.userAgentData && navigator.userAgentData.brands) || [];
  const named = brands.map((b) => b.brand).find((b) => /Edge|Opera|Vivaldi|Chrome/.test(b));
  if (named) return named.replace("Google ", "").replace("Microsoft ", "");
  return /Edg\//.test(navigator.userAgent) ? "Edge" : "Chrome";
}

// NEON holds each poll for up to 20 s; the settings read at the top of every round is also what keeps a
// Chrome service worker awake (it stops after 30 s without an extension call).
async function loop() {
  if (running) return;
  running = true;
  const name = await browserName();
  try {
    for (;;) {
      const cfg = await settings();
      if (!cfg.token) {
        show("key");
        await sleep(4000);
        continue;
      }
      try {
        const res = await fetch(`http://127.0.0.1:${cfg.port}/poll`, {
          headers: { "X-Neon-Token": cfg.token, "X-Neon-Browser": name },
          cache: "no-store",
        });
        if (res.status === 403) {
          show("key");
          await sleep(4000);
          continue;
        }
        show("on");
        if (res.status === 200) {
          const req = await res.json();
          run(cfg, req);             // not awaited: the next poll starts straight away
        }
      } catch (e) {
        show("off");
        await sleep(3000);
      }
    }
  } finally {
    running = false;
  }
}

api.storage.onChanged.addListener(() => { state = ""; loop(); });
// Chrome may still stop the service worker (browser idle, sleep): an alarm every 30 s wakes it and the
// loop starts again. Firefox keeps the background page alive, so this does nothing there.
if (api.alarms) {
  api.alarms.create("neon-keepalive", { periodInMinutes: 0.5 });
  api.alarms.onAlarm.addListener(() => loop());
}
api.runtime.onStartup && api.runtime.onStartup.addListener(() => loop());
show("off");
loop();
