// node --test tests/   — browser adapter + chain orchestration, with a fake environment (no DOM, no network).
const test = require("node:test");
const assert = require("node:assert");
const SA = require("../ads/adapters.js");

function fakeEnv(win, opts) {
  opts = opts || {};
  let t = 0; const timers = [];
  const env = {
    win, now: () => t,
    setTimeout: (f, ms) => { const h = { f, at: t + ms, off: false }; timers.push(h); return h; },
    clearTimeout: (h) => { if (h) h.off = true; },
    addScript: (url, attrs, ok, err) => { opts.scriptFails ? err() : (opts.onScript && opts.onScript(win), ok()); },
    ensureContainer: () => {},
    advance: async (ms) => { const end = t + ms; while (t < end) { t = Math.min(end, t + 100); timers.slice().filter(h => !h.off && h.at <= t).forEach(h => { h.off = true; h.f(); }); await new Promise(r => setImmediate(r)); } },
  };
  return env;
}
const hooks = () => ({ shownCalls: 0, onShown() { this.shownCalls++; } });

test("Tads widget unavailable -> blocked", async () => {
  const r = await SA.createAdapters(fakeEnv({}, { scriptFails: true })).tads.run({ unit_id: "13070", sdk: "x" }, hooks());
  assert.deepStrictEqual([r.status, r.reason], ["failed", "blocked"]);
});

test("Tads onAdsNotFound -> no_fill; onShowReward -> completed", async () => {
  for (const [mode, want] of [["none", "failed"], ["reward", "completed"]]) {
    let cfgRef;
    const win = { tads: { init: (c) => { cfgRef = c; return { showAd: async () => { setImmediate(() => mode === "reward" ? c.onShowReward("ad1") : c.onAdsNotFound()); } }; } } };
    const r = await SA.createAdapters(fakeEnv(win)).tads.run({ unit_id: "13070" }, hooks());
    assert.strictEqual(r.status, want);
    if (want === "failed") assert.strictEqual(r.reason, "no_fill");
  }
});

test("Tads click is not a completed view (onClickReward ignored)", async () => {
  let c0;
  const win = { tads: { init: (c) => { c0 = c; return { showAd: async () => { setImmediate(() => { c.onClickReward("x"); c.onAdsNotFound(); }); } }; } } };
  const r = await SA.createAdapters(fakeEnv(win)).tads.run({ unit_id: "13070" }, hooks());
  assert.strictEqual(r.status, "failed");
});

test("runChain: first provider fails undisplayed -> server hands next; both fail -> exhausted", async () => {
  const calls = [];
  const adapters = { monetag: { run: async () => ({ status: "failed", reason: "no_fill", shown: false }) },
                     tads: { run: async () => ({ status: "failed", reason: "timeout", shown: false }) } };
  const api = async (path, body) => { calls.push([path, body.event]); return body.event === "fail" && body.ad_id === "a1" ? { ok: true, next: { ok: true, network: "tads", ad_id: "a2" } } : { ok: true, exhausted: true }; };
  const out = await SA.runChain({ ok: true, network: "monetag", ad_id: "a1" }, adapters, api, null);
  assert.strictEqual(out.kind, "exhausted"); assert.strictEqual(out.displayed, false);
  assert.deepStrictEqual(calls.map(c => c[1]), ["fail", "fail"]);
});

test("runChain: displayed-then-failed never chains a second ad", async () => {
  const adapters = { monetag: { run: async (c, h) => { h.onShown(); return { status: "failed", reason: "error", shown: true }; } } };
  const api = async () => ({ ok: true, exhausted: true });
  const out = await SA.runChain({ network: "monetag", ad_id: "a1" }, adapters, api, null);
  assert.deepStrictEqual([out.kind, out.displayed], ["exhausted", true]);
});

test("runChain: skipped reports skip and stops", async () => {
  const seen = [];
  const out = await SA.runChain({ network: "monetag", ad_id: "a1" }, { monetag: { run: async () => ({ status: "skipped", shown: true }) } },
    async (p, b) => { seen.push(b.event); return { ok: true }; }, null);
  assert.strictEqual(out.kind, "skipped"); assert.ok(seen.includes("skip"));
});

test("claim: polls while server says wait, then returns server verdict", async () => {
  let n = 0;
  const r = await SA.claim({ ad_id: "a" }, {}, async () => (++n < 3 ? { ok: true, wait: 1 } : { ok: true, delivered: true }), async () => {}, 10);
  assert.strictEqual(n, 3); assert.strictEqual(r.delivered, true);
});

test("claim: gives up after max tries (no infinite loading)", async () => {
  const r = await SA.claim({ ad_id: "a" }, {}, async () => ({ ok: true, wait: 1 }), async () => {}, 4);
  assert.strictEqual(r.ok, false);
});

test("Tads onShowReward is only a claim: its adId is never sent as proof and a server denial is passed through untouched", async () => {
  const sent = [];
  const api = async (path, body) => { sent.push([path, body]); return { ok: false, error: "This ad provider can't be verified right now. Please try again." }; };
  const res = { status: "completed", shown: true, adId: "tads-reward-123" };       // what the Tads adapter reports on onShowReward
  const r = await SA.claim({ ad_id: "sid1.abcd" }, res, api, async () => {});
  assert.strictEqual(r.ok, false);                                                  // the browser never upgrades a denial
  assert.deepStrictEqual(sent, [["/api/smart/done", { ad_id: "sid1.abcd", completed: true, non_valued: false }]]);
  assert.ok(!JSON.stringify(sent).includes("tads-reward-123"));                     // client-supplied reward id is not a credential
});

test("claim keeps waiting only while the server says wait, then stops", async () => {
  let n = 0;
  const api = async () => (++n < 3 ? { ok: true, wait: 1 } : { ok: false, error: "We couldn't confirm the ad view. Please try again." });
  const r = await SA.claim({ ad_id: "a" }, { status: "completed" }, api, async () => {}, 10);
  assert.strictEqual(n, 3); assert.strictEqual(r.ok, false);
});
