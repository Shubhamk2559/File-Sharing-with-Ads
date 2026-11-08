/* Smart-ad browser adapters + chain orchestrator.
 *
 * SDK surfaces used (verified against the official docs on 2026-11-05):
 *  - Monetag   : window.show_<ZONE>({ymid, requestVar}) -> Promise, resolves when the ad closes; result.reward_event_type
 *                ('valued' | 'non_valued'). Rejects with e.g. "Empty feed" when nothing can be shown.   (docs.monetag.com / existing integration)
 *  - Tads.me   : window.tads.init({widgetId, debug, onShowReward(adId), onClickReward(adId), onAdsNotFound}) -> controller;
 *                controller.showAd() fetches an ad and opens a fullscreen overlay. Script: https://w.tads.me/widget.js. (docs.tads.me vanilla-js)
 *
 * Rules: a click or a close is NEVER completion. Completion is only the network's own completion callback, and the
 * server decides whether it earns a reward. This file never grants anything.
 *
 * Adapter result: {status: 'completed'|'skipped'|'failed', reason?: 'no_fill'|'timeout'|'blocked'|'error', shown: bool, nonValued?: bool}
 */
(function (root) {
  "use strict";

  var DISPLAY_GUESS_MS = 3000;       // Monetag has no first-frame callback: a failure after this long is treated as "was displayed"
  var LOAD_TIMEOUT_MS = 10000;
  var GLOBAL_WAIT_MS = 4000;         // script loaded (onload) but its global never appeared => it was neutered by a blocker, not "slow"
  var RUN_TIMEOUT_MS = 90000;

  function text(e) {
    if (!e) return "";
    if (typeof e === "string") return e;
    return e.message || e.error || e.reason || e.description || "";
  }

  function loadScript(env, url, attrs) {
    env._scripts = env._scripts || {};
    if (env._scripts[url]) return env._scripts[url];
    env._scripts[url] = new Promise(function (resolve, reject) {
      var done = false;
      var t = env.setTimeout(function () { if (!done) { done = true; reject(new Error("timeout")); } }, LOAD_TIMEOUT_MS);
      env.addScript(url, attrs || {}, function () { if (!done) { done = true; env.clearTimeout(t); resolve(); } },
        function () { if (!done) { done = true; env.clearTimeout(t); reject(new Error("blocked")); } });
    });
    env._scripts[url].catch(function () { delete env._scripts[url]; });
    return env._scripts[url];
  }

  function waitFor(env, test, ms) {
    return new Promise(function (resolve) {
      var t0 = env.now();
      (function tick() {
        if (test()) return resolve(true);
        if (env.now() - t0 > ms) return resolve(false);
        env.setTimeout(tick, 100);
      })();
    });
  }

  function once(hooks) {
    var fired = false;
    return { onShown: function () { if (!fired) { fired = true; hooks.onShown(); } } };
  }

  // ------------------------------------------------------------------ Monetag
  function monetagAdapter(env) {
    return {
      name: "monetag",
      run: async function (cfg, hooks) {
        hooks = once(hooks);
        var fn = "show_" + cfg.zone, win = env.win;
        if (typeof win[fn] !== "function") {
          try { await loadScript(env, cfg.sdk, { "data-zone": cfg.zone, "data-sdk": fn }); }
          catch (e) { return { status: "failed", reason: text(e) === "blocked" ? "blocked" : "timeout", shown: false }; }
          if (!(await waitFor(env, function () { return typeof win[fn] === "function"; }, GLOBAL_WAIT_MS)))
            return { status: "failed", reason: "blocked", shown: false };
        }
        var t0 = env.now(), settled = false, wd;
        var guard = new Promise(function (resolve) {
          wd = env.setTimeout(function () { if (!settled) resolve({ status: "failed", reason: "timeout", shown: true }); }, RUN_TIMEOUT_MS);
        });
        var play;
        try { play = Promise.resolve(win[fn]({ ymid: cfg.ymid, requestVar: "unlock" })); }
        catch (e) { return { status: "failed", reason: "error", shown: false }; }
        var main = play.then(function (result) {
          return { status: "completed", shown: true, nonValued: !!(result && result.reward_event_type === "non_valued") };
        }, function (e) {
          var m = text(e), shown = env.now() - t0 >= DISPLAY_GUESS_MS;
          return { status: "failed", reason: /empty feed/i.test(m) ? "no_fill" : /timeout/i.test(m) ? "timeout" : "error", shown: shown };
        });
        var res = await Promise.race([main, guard]);
        settled = true; env.clearTimeout(wd);
        if (res.shown) hooks.onShown();
        return res;
      },
    };
  }

  // ------------------------------------------------------------------ Tads.me
  function tadsAdapter(env) {
    var cur = null, controllers = {};
    return {
      name: "tads",
      run: async function (cfg, hooks) {
        hooks = once(hooks);
        var win = env.win, id = String(cfg.unit_id);
        if (!(win.tads && win.tads.init)) {
          try { await loadScript(env, cfg.sdk, {}); }
          catch (e) { return { status: "failed", reason: text(e) === "blocked" ? "blocked" : "timeout", shown: false }; }
          if (!(await waitFor(env, function () { return !!(win.tads && win.tads.init); }, GLOBAL_WAIT_MS)))
            return { status: "failed", reason: "blocked", shown: false };
        }
        env.ensureContainer("tads-container-" + id);
        var ctl = controllers[id] || (win.tads.controllers && win.tads.controllers[id]);
        if (!ctl) {
          try {
            ctl = win.tads.init({
              widgetId: id, debug: !!cfg.debug,
              onShowReward: function (adId) { if (cur) cur.reward(adId); },
              onClickReward: function () { /* a click is never a completed rewarded view */ },
              onAdsNotFound: function () { if (cur) cur.noAds(); },
            });
          } catch (e) { return { status: "failed", reason: "error", shown: false }; }
          controllers[id] = ctl;
        }
        return new Promise(function (resolve) {
          var shown = false, settled = false, wd, confirmT;
          function finish(r) {
            if (settled) return; settled = true;
            env.clearTimeout(wd); env.clearTimeout(confirmT); cur = null; resolve(r);
          }
          function markShown() { if (!shown) { shown = true; hooks.onShown(); } }
          cur = {
            reward: function (adId) { markShown(); finish({ status: "completed", shown: true, adId: adId }); },
            noAds: function () { if (!shown) finish({ status: "failed", reason: "no_fill", shown: false }); },
          };
          wd = env.setTimeout(function () { finish({ status: "failed", reason: "timeout", shown: shown }); }, RUN_TIMEOUT_MS);
          Promise.resolve().then(function () { return ctl.showAd(); }).then(function () {
            // showAd() resolves when the overlay opens; wait a beat so a late onAdsNotFound can still win.
            confirmT = env.setTimeout(function () { if (!settled) markShown(); }, 1500);
          }, function () { finish({ status: "failed", reason: "error", shown: shown }); });
        });
      },
    };
  }

  function createAdapters(env) {
    var a = [monetagAdapter(env), tadsAdapter(env)], out = {};
    a.forEach(function (x) { out[x.name] = x; });
    return out;
  }

  // ------------------------------------------------------------------ orchestration
  // api(path, extraBody) -> Promise<json>. Shows at most ONE ad: a fallback is requested only when the server
  // confirms the previous attempt never displayed an ad.
  async function runChain(first, adapters, api, ui) {
    var state = first, sawBlocked = false;           // sawBlocked: some provider's script was blocked/neutered on this device
    for (var guard = 0; guard < 4; guard++) {
      if (ui && ui.loading) ui.loading(state);
      var adapter = adapters[state.network];
      if (!adapter) return { kind: "exhausted", displayed: false };
      var shownP = null;
      var hooks = { onShown: function () { if (!shownP) { if (ui && ui.shown) ui.shown(state); } if (!shownP) shownP = api("/api/smart/report", { ad_id: state.ad_id, event: "shown" }).catch(function () {}); } };
      var res;
      try { res = await adapter.run(state, hooks); } catch (e) { res = { status: "failed", reason: "error", shown: false }; }
      if (shownP) await shownP;
      if (res.reason === "blocked") sawBlocked = true;
      if (res.status === "completed") return { kind: "completed", state: state, res: res };
      if (res.status === "skipped") {
        await api("/api/smart/report", { ad_id: state.ad_id, event: "skip" }).catch(function () {});
        return { kind: "skipped", state: state };
      }
      var r = await api("/api/smart/report", { ad_id: state.ad_id, event: "fail", reason: res.reason || "error" }).catch(function () { return {}; });
      if (r && r.next && r.next.ok) { state = r.next; continue; }
      return { kind: "exhausted", displayed: !!res.shown, reason: res.reason, sawBlocked: sawBlocked };
    }
    return { kind: "exhausted", displayed: false };
  }

  async function claim(state, res, api, sleep, maxTries) {
    for (var i = 0; i < (maxTries || 60); i++) {
      var r = await api("/api/smart/done", { ad_id: state.ad_id, completed: true, non_valued: !!res.nonValued });
      if (!r || !r.wait) return r;
      await sleep(Math.min(r.wait, 3) * 1000 + 100);
    }
    return { ok: false, error: "We couldn't confirm the ad view. Please try again." };
  }

  // ------------------------------------------------------------------ browser env
  function browserEnv(win) {
    var doc = win.document;
    return {
      win: win, now: function () { return Date.now(); },
      setTimeout: function (f, ms) { return win.setTimeout(f, ms); }, clearTimeout: function (t) { win.clearTimeout(t); },
      addScript: function (url, attrs, ok, err) {
        var s = doc.createElement("script"); s.src = url;
        Object.keys(attrs).forEach(function (k) { s.setAttribute(k, attrs[k]); });
        s.onload = ok; s.onerror = function () { win.__sdkErr = 1; err(); };
        doc.head.appendChild(s);
      },
      ensureContainer: function (id) {
        if (doc.getElementById(id)) return;
        var d = doc.createElement("div"); d.id = id; d.style.display = "none"; doc.body.appendChild(d);
      },
    };
  }

  var api = { createAdapters: createAdapters, runChain: runChain, claim: claim, browserEnv: browserEnv, loadScript: loadScript };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.SmartAds = api;
})(typeof window !== "undefined" ? window : this);
