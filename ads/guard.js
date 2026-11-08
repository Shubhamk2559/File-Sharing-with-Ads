/* Ad-blocker guard (browser side). The SERVER is the judge (ads/gate.py); this script only
 *   1. fetches the server's bait resources and hands the proofs back,
 *   2. probes the ad hosts (DNS-level blockers / Private DNS / VPN filters make these fail),
 *   3. plants a cosmetic bait element (cosmetic filters hide it),
 *   4. notices tampered browser natives.
 * Nothing here grants anything: if this file is disabled or lied to, the grace request simply fails on the server.
 *
 * check() -> { status: 'ok' | 'blocked' | 'offline' | 'slow' | 'error', reasons: [], cid, proofs, cosmetic, tamper }
 *   blocked  = positive evidence of a blocker       offline = our own server unreachable
 *   slow     = inconclusive (timeouts) - never treated as clean when strict
 */
(function (root) {
  "use strict";

  var PROBE_MS = 7000, BAIT_MS = 7000;

  function isNativeFn(fn) {
    try { return typeof fn === "function" && /\{\s*\[native code\]\s*\}/.test(Function.prototype.toString.call(fn)); }
    catch (e) { return false; }
  }

  async function check(env, opts) {
    opts = opts || {};
    var reasons = [];
    var hosts = opts.hosts || [];

    var pingP = env.probe("/api/ping", PROBE_MS, false);
    var hostsP = Promise.all(hosts.map(function (u) { return env.probe(u, PROBE_MS, true); }));
    var cosmeticP = env.cosmetic();
    var chP = opts.api("/api/gate/challenge", {});

    var ping = await pingP;
    if (ping === "fail") return { status: "offline", reasons: ["ping"], cosmetic: true, tamper: false };
    var ch = await chP;
    if (!ch || !ch.ok) {
      var why = ch && ch.code ? ch.code : "challenge";
      return { status: ch && ch.code === "rate_limited" ? "slow" : "error", reasons: [why], error: ch && ch.error, cosmetic: true, tamper: false };
    }

    var baits = ch.baits || [];
    var results = await Promise.all(baits.map(function (b) { return env.loadBait(b.t, b.u, BAIT_MS); }));
    var proofs = {}, gv = env.proofs() || {};
    var blocked = false, slow = ping === "slow";
    baits.forEach(function (b, i) {
      var r = results[i];
      if (b.t === "script") {
        if (r === "ok" && typeof gv[b.k] === "string") proofs[b.k] = gv[b.k];
        else if (r === "slow") { slow = true; }
        else { blocked = true; reasons.push("bait:" + b.k); }       // onerror, or onload with no proof (stubbed by the blocker)
      } else {
        if (r === "ok") proofs[b.k] = true;
        else if (r === "slow") { slow = true; }
        else { blocked = true; reasons.push("bait:" + b.k); }
      }
    });

    var hostRes = await hostsP;
    hostRes.forEach(function (r, i) {
      if (r === "fail") { blocked = true; reasons.push("host:" + hosts[i]); }
      else if (r === "slow") slow = true;
    });

    var cosmetic = await cosmeticP;
    if (!cosmetic) { blocked = true; reasons.push("cosmetic"); }

    var tamper = !!env.tampered();
    if (tamper) reasons.push("tamper");
    if (tamper && opts.strict) blocked = true;

    var status = blocked ? "blocked" : slow ? "slow" : "ok";
    return { status: status, reasons: reasons, cid: ch.cid, proofs: proofs, cosmetic: cosmetic, tamper: tamper };
  }

  // ------------------------------------------------------------------ browser env
  function browserEnv(win) {
    var doc = win.document;
    return {
      probe: function (url, ms, cross) {
        return new Promise(function (resolve) {
          var ctl = typeof win.AbortController === "function" ? new win.AbortController() : null;
          var t = win.setTimeout(function () { if (ctl) ctl.abort(); resolve("slow"); }, ms);
          var o = cross
            ? { method: "HEAD", mode: "no-cors", cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" }
            : { cache: "no-store" };
          if (ctl) o.signal = ctl.signal;
          win.fetch(url, o).then(
            function () { win.clearTimeout(t); resolve("ok"); },
            function (e) { win.clearTimeout(t); resolve(e && e.name === "AbortError" ? "slow" : "fail"); }
          );
        });
      },
      loadBait: function (kind, url, ms) {
        return new Promise(function (resolve) {
          var done = false;
          function fin(v) { if (!done) { done = true; win.clearTimeout(t); resolve(v); } }
          var t = win.setTimeout(function () { fin("slow"); }, ms);
          try {
            if (kind === "script") {
              var s = doc.createElement("script");
              s.async = true; s.onload = function () { fin("ok"); }; s.onerror = function () { fin("fail"); };
              s.src = url; doc.head.appendChild(s);
            } else {
              var im = new win.Image();
              im.onload = function () { fin("ok"); }; im.onerror = function () { fin("fail"); };
              im.src = url;
            }
          } catch (e) { fin("fail"); }
        });
      },
      proofs: function () { return win.__gv || {}; },
      cosmetic: function () {
        return new Promise(function (resolve) {
          var d = doc.createElement("div");
          d.className = "adsbox ad-banner ad_unit textads banner-ads pub_300x250 ad-placement sponsored-ad";
          d.id = "ad-container-" + Math.random().toString(36).slice(2, 8);
          d.setAttribute("style", "position:absolute;left:-9999px;top:0;width:12px;height:12px;pointer-events:none");
          d.textContent = " ";
          doc.body.appendChild(d);
          win.setTimeout(function () {
            var hidden = true;
            try {
              var cs = win.getComputedStyle(d);
              hidden = !d.isConnected || d.offsetHeight === 0 || d.offsetWidth === 0 || cs.display === "none" || cs.visibility === "hidden";
            } catch (e) { hidden = true; }
            try { d.remove(); } catch (e) { /* ignore */ }
            resolve(!hidden);
          }, 250);
        });
      },
      tampered: function () {
        try {
          return !(isNativeFn(win.fetch) && isNativeFn(win.Function.prototype.toString) && isNativeFn(win.XMLHttpRequest) &&
                   isNativeFn(doc.createElement));
        } catch (e) { return true; }
      },
    };
  }

  var api = { check: check, browserEnv: browserEnv, isNativeFn: isNativeFn };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.SmartGuard = api;
})(typeof window !== "undefined" ? window : this);
