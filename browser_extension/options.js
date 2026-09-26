"use strict";

const api = globalThis.browser ?? globalThis.chrome;      // Firefox / Zen, or Chrome / Edge / Brave

const tokenBox = document.getElementById("token");
const portBox = document.getElementById("port");
const status = document.getElementById("status");

function say(text, ok) {
  status.textContent = text;
  status.className = ok === undefined ? "" : ok ? "ok" : "bad";
}

async function load() {
  const stored = await api.storage.local.get({ port: 47811, token: "" });
  tokenBox.value = stored.token;
  portBox.value = stored.port;
}

async function test() {
  const port = Number(portBox.value) || 47811;
  try {
    const res = await fetch(`http://127.0.0.1:${port}/hello`, {
      headers: { "X-Neon-Token": tokenBox.value.trim() }, cache: "no-store",
    });
    if (res.status === 200) say("Connected to NEON.", true);
    else if (res.status === 403) say("NEON is running, but the token doesn't match.", false);
    else say(`NEON answered with ${res.status}.`, false);
  } catch (e) {
    say("Can't reach NEON. Is it running, with the browser switched on in Settings?", false);
  }
}

document.getElementById("save").addEventListener("click", async () => {
  await api.storage.local.set({ token: tokenBox.value.trim(), port: Number(portBox.value) || 47811 });
  say("Saved.");
  await test();
});
document.getElementById("test").addEventListener("click", test);
load();
