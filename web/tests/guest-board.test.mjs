import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const main = readFileSync(new URL("../src/main.tsx", import.meta.url), "utf8");
const guest = readFileSync(new URL("../src/GuestBoard.tsx", import.meta.url), "utf8");
const sw = readFileSync(new URL("../public/sw.js", import.meta.url), "utf8");

test("guest board mounts outside the authenticated voice app", () => {
  assert.match(main, /guestMode = window\.location\.pathname === "\/guest\/board"/);
  assert.match(main, /guestMode \? <GuestBoard \/> : <App \/>/);
  assert.match(main, /!guestMode && "serviceWorker" in navigator/);
  assert.doesNotMatch(guest, /createLiveClient|Microphone|voice-orb|navigator\.mediaDevices/);
});

test("guest link is fragment-exchanged and strips the bearer token", () => {
  assert.match(guest, /hashToken\(\)/);
  assert.match(guest, /exchangeGuestToken\(token\)/);
  assert.match(guest, /window\.history\.replaceState/);
  assert.match(guest, /requestFullscreen\(\)/);
  assert.match(guest, /onPointerMove/);
  assert.match(guest, /onWheel/);
});

test("service worker never caches or offline-falls-back guest routes", () => {
  assert.match(sw, /url\.pathname\.startsWith\("\/guest\/"\)/);
  assert.match(sw, /event\.respondWith\(fetch\(new Request\(request, \{ cache: "no-store" \}\)\)\)/);
});
