// node --test tests/ — ads/guard.js with a fake environment (no DOM, no network).
const test = require("node:test");
const assert = require("node:assert");
const G = require("../ads/guard.js");
const SA = require("../ads/adapters.js");

const BAITS = [{ k: "s1", t: "script", u: "/a?c=n&k=s1" }, { k: "s2", t: "script", u: "/b?c=n&k=s2" },
               { k: "s3", t: "script", u: "/c?c=n&k=s3" }, { k: "i1", t: "img", u: "/d?c=n&k=i1" }];

function env(o) {
  o = o || {};
  const gv = {};
  return {
    probe: async (u) => (u === "/api/ping" ? (o.ping || "ok") : ((o.hosts && o.hosts[u]) || "ok")),
    loadBait: async (kind, url) => {
      const k = /k=(\w+)/.exec(url)[1];
      const r = (o.bait && o.bait[k]) || "ok";
      if (r === "ok" && kind === "script" && !(o.stub && o.stub.includes(k))) gv[k] = "proof-" + k;
      return r;
    },
    proofs: () => gv,
    cosmetic: async () => o.cosmetic !== false,
    tampered: () => !!o.tampered,
  };
}
const okApi = async () => ({ ok: true, cid: "n", ttl: 90, baits: BAITS });
const run = (e, extra) => G.check(e, Object.assign({ hosts: ["https://libtl.com/sdk.js", "https://w.tads.me/widget.js"], api: okApi, strict: true }, extra));

test("clean device: ok with proofs for every bait", async () => {
  const r = await run(env());
  assert.strictEqual(r.status, "ok"); assert.strictEqual(r.cid, "n");
  assert.deepStrictEqual(Object.keys(r.proofs).sort(), ["i1", "s1", "s2", "s3"]);
  assert.strictEqual(r.proofs.i1, true); assert.strictEqual(r.proofs.s1, "proof-s1");
});

test("any blocked bait => blocked (script error, image error)", async () => {
  for (const k of ["s1", "s2", "s3", "i1"]) {
    const r = await run(env({ bait: { [k]: "fail" } }));
    assert.strictEqual(r.status, "blocked", k); assert.ok(r.reasons.includes("bait:" + k));
  }
});

test("blocker that STUBS the script (onload fires, no proof) => blocked", async () => {
  const r = await run(env({ stub: ["s2"] }));
  assert.strictEqual(r.status, "blocked"); assert.ok(r.reasons.includes("bait:s2"));
});

test("ad host unreachable (Private DNS / DNS filter) => blocked; Tads host too", async () => {
  assert.strictEqual((await run(env({ hosts: { "https://libtl.com/sdk.js": "fail" } }))).status, "blocked");
  assert.strictEqual((await run(env({ hosts: { "https://w.tads.me/widget.js": "fail" } }))).status, "blocked");
});

test("hidden cosmetic bait => blocked", async () => {
  const r = await run(env({ cosmetic: false }));
  assert.strictEqual(r.status, "blocked"); assert.ok(r.reasons.includes("cosmetic"));
});

test("timeouts are inconclusive 'slow', never 'ok'", async () => {
  assert.strictEqual((await run(env({ hosts: { "https://libtl.com/sdk.js": "slow" } }))).status, "slow");
  assert.strictEqual((await run(env({ bait: { s1: "slow" } }))).status, "slow");
  assert.strictEqual((await run(env({ ping: "slow" }))).status, "slow");
});

test("own server unreachable => offline (not blocked)", async () => {
  assert.strictEqual((await run(env({ ping: "fail" }))).status, "offline");
});

test("challenge refused: rate limit => slow, other => error; never ok", async () => {
  assert.strictEqual((await run(env(), { api: async () => ({ ok: false, code: "rate_limited" }) })).status, "slow");
  assert.strictEqual((await run(env(), { api: async () => ({ ok: false, code: "session", error: "x" }) })).status, "error");
});

test("tampered natives: flagged always, blocking only when strict", async () => {
  const s = await run(env({ tampered: true }), { strict: true });
  assert.strictEqual(s.status, "blocked"); assert.strictEqual(s.tamper, true);
  const l = await run(env({ tampered: true }), { strict: false });
  assert.strictEqual(l.status, "ok"); assert.strictEqual(l.tamper, true);
});

test("isNativeFn", () => {
  assert.strictEqual(G.isNativeFn(Math.max), true);
  assert.strictEqual(G.isNativeFn(function patched() { return 1; }), false);
  assert.strictEqual(G.isNativeFn(undefined), false);
});

// ---- adapters: a script that loads but never defines its global was neutered, not "slow"
function advEnv(win, loads) {
  let t = 0; const timers = [];
  return {
    win, now: () => t,
    setTimeout: (f, ms) => { const h = { f, at: t + ms, off: false }; timers.push(h); return h; }, clearTimeout: (h) => { if (h) h.off = true; },
    addScript: (url, attrs, ok, err) => { loads ? ok() : err(); }, ensureContainer: () => {},
    advance: async (ms) => { const end = t + ms; while (t < end) { t = Math.min(end, t + 100); timers.slice().filter(h => !h.off && h.at <= t).forEach(h => { h.off = true; h.f(); }); await new Promise(r => setImmediate(r)); } },
  };
}
test("Monetag script loaded but show_<zone> never appears => reason 'blocked'", async () => {
  const e = advEnv({}, true);
  const p = SA.createAdapters(e).monetag.run({ zone: "123", sdk: "x", ymid: "y" }, { onShown() {} });
  await e.advance(6000);
  const r = await p;
  assert.deepStrictEqual([r.status, r.reason, r.shown], ["failed", "blocked", false]);
});
test("Tads widget loaded but tads.init never appears => reason 'blocked'", async () => {
  const e = advEnv({}, true);
  const p = SA.createAdapters(e).tads.run({ unit_id: "1", sdk: "x" }, { onShown() {} });
  await e.advance(6000);
  const r = await p;
  assert.deepStrictEqual([r.status, r.reason], ["failed", "blocked"]);
});

test("runChain remembers that a provider was blocked even if the last one just had no fill", async () => {
  const adapters = { monetag: { run: async () => ({ status: "failed", reason: "blocked", shown: false }) },
                     tads: { run: async () => ({ status: "failed", reason: "no_fill", shown: false }) } };
  const api = async (p, b) => (b.ad_id === "a1" ? { ok: true, next: { ok: true, network: "tads", ad_id: "a2" } } : { ok: true, exhausted: true });
  const out = await SA.runChain({ network: "monetag", ad_id: "a1" }, adapters, api, null);
  assert.deepStrictEqual([out.kind, out.reason, out.sawBlocked, out.displayed], ["exhausted", "no_fill", true, false]);
});
