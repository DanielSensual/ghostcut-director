// GhostCut Director — first-run tour (ONBOARD).
// Classic script: window.GCTour.open(ctx) shows a 6-step coach card over Import.
// No network is required to open; live steps degrade to calm empty states.
(function () {
  var PREFILL = "the footage is on the SD card, transfer it to my SSD";
  function fallbackH(tag, attrs) {
    var el = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (k === "class") el.className = v;
      else if (k.indexOf("on") === 0) el.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) el.setAttribute(k, v);
    });
    for (var i = 2; i < arguments.length; i++) {
      var kid = arguments[i];
      if (kid == null) continue;
      if (Array.isArray(kid)) { kid.forEach(function (k) { if (k != null) el.append(k.nodeType ? k : document.createTextNode(k)); }); }
      else el.append(kid.nodeType ? kid : document.createTextNode(kid));
    }
    return el;
  }
  function mark(size, mood) {
    try {
      if (window.GhostMark) return window.GhostMark(size, mood);
    } catch (e) { /* fall through to glyph */ }
    var s = document.createElement("span");
    s.className = "gc-tour-glyph";
    s.textContent = "Ghost";
    return s;
  }
  function open(ctx) {
    ctx = ctx || window.GC || {};
    var h = ctx.h || fallbackH;
    var api = ctx.api;
    if (document.querySelector(".gc-tour-overlay")) return;
    try { if (ctx.show) ctx.show("import"); } catch (e) { /* view already there */ }
    var step = 0;
    var pollTimer = 0;
    var prevFocus = document.activeElement;
    var overlay = h("div", { class: "gc-tour-overlay" });
    var card = h("div", { class: "gc-tour-card", role: "dialog", "aria-modal": "true", "aria-label": "Welcome tour" });
    var body = h("div", { class: "gc-tour-body" });
    var dots = h("div", { class: "gc-tour-dots" });
    var backBtn = h("button", { type: "button", class: "gc-tour-btn" }, "Back");
    var skipBtn = h("button", { type: "button", class: "gc-tour-btn gc-tour-skip" }, "Skip");
    var nextBtn = h("button", { type: "button", class: "gc-tour-btn primary" }, "Next");
    var foot = h("div", { class: "gc-tour-foot" }, dots, h("span", { class: "gc-tour-spacer" }), backBtn, skipBtn, nextBtn);
    card.append(body, foot);
    overlay.append(card);
    document.body.append(overlay);
    function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = 0; } }
    function save(skipped) {
      var payload = { onboarding: { seen: true, version: 1, step: step, skipped: !!skipped, completed_at: new Date().toISOString() } };
      try {
        if (api) { api("/api/settings", payload).catch(function () {}); return; }
      } catch (e) { /* never block closing */ }
      try { fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).catch(function () {}); } catch (e) { /* offline */ }
    }
    function close(skipped) {
      stopPoll();
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      save(skipped);
      try { if (prevFocus && prevFocus.focus) prevFocus.focus(); } catch (e) { /* noop */ }
    }
    function onKey(e) {
      if (document.querySelector(".modal:not([hidden])")) return; // Connections (opened from step 2) handles its own keys
      if (e.key === "Escape") { e.preventDefault(); close(true); return; }
      // Keys on the tour stay with the tour: the player's Space/arrow shortcuts would otherwise play the hidden video
      // and swallow Space on the tour's buttons.
      if (card.contains(e.target)) e.stopPropagation();
      if (e.key !== "Tab") return;
      var focusables = Array.prototype.slice.call(card.querySelectorAll("button, [href], input, textarea, select, [tabindex]")).filter(function (n) { return !n.disabled && n.offsetParent !== null; });
      if (!focusables.length) return;
      var first = focusables[0], last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", onKey, true);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(true); });
    function strip() {
      return h("div", { class: "gc-tour-strip" },
        h("span", { class: "on" }, "Import"), h("span", {}, "Story"),
        h("span", {}, "Edit"), h("span", {}, "Deliver"));
    }
    function gb(n) {
      var v = Number(n || 0);
      if (v >= 1e9) return (v / 1e9).toFixed(1).replace(/\.0$/, "") + " GB";
      if (v >= 1e6) return Math.round(v / 1e6) + " MB";
      return Math.round(v) + " B";
    }
    var steps = [
      function () {
        body.replaceChildren(
          h("div", { class: "gc-tour-mascot" }, mark(72, "idle")),
          h("h3", {}, "Welcome to GhostCut Director"),
          h("p", {}, "Cards in, finished cut out. You talk; Ghost and Muse do the work."),
          strip(),
          h("p", { class: "muted" }, "Six short steps. Skip any time — Help brings this back."));
      },
      function () {
        var box = h("div", {}, h("p", { class: "muted" }, "Checking connections…"));
        body.replaceChildren(h("h3", {}, "Connect your tools (optional)"),
          h("p", {}, "Keys stay on this Mac. Nothing is required to import."), box);
        (async function () {
          var eleven = "ElevenLabs not connected — music and sound effects (optional).";
          var museLine = "Ghost is running on rules — its built-in planner reads every request, offline.";
          try {
            var j = await api("/api/connections");
            var list = (j && j.providers) || [];
            var hit = list.find(function (p) { return p.id === "elevenlabs" || /eleven/i.test(p.name || ""); });
            if (hit && hit.connected) eleven = "ElevenLabs connected — music and sound effects ready.";
          } catch (e) { eleven = "Connections unavailable offline — connect later if you want music and sound effects."; }
          try {
            var s = await api("/api/settings");
            if (s && s.meta_key) museLine = "A Muse key is set — Ghost reads requests with Spark, and uses the built-in planner whenever Muse is unavailable.";
          } catch (e) { /* keep rules line */ }
          var openBtn = h("button", { type: "button", class: "gc-tour-btn" }, "Open Connections");
          openBtn.onclick = function () { try { if (ctx.openConn) ctx.openConn(); } catch (e) { /* dialog unavailable */ } };
          box.replaceChildren(h("p", {}, eleven), h("p", {}, museLine), h("p", {}, openBtn));
        })();
      },
      function () {
        var box = h("div", {}, h("p", { class: "muted" }, "Looking for drives…"));
        body.replaceChildren(h("h3", {}, "Pick your destination"),
          h("p", {}, "Copies land in a dated folder on this drive. The card is only read."), box);
        (async function () {
          try {
            var j = await api("/api/offload/volumes");
            var dests = ((j && j.volumes) || []).filter(function (v) { return v.kind === "destination"; });
            if (!dests.length) { box.replaceChildren(h("p", {}, "Connect your SSD, then come back to this step.")); return; }
            box.replaceChildren.apply(box, dests.map(function (d) {
              var total = Number(d.total_bytes || 0), free = Number(d.free_bytes || 0);
              var pct = total ? Math.max(2, Math.min(100, (free / total) * 100)) : 0;
              var bar = h("div", { class: "gc-tour-meter" }, h("i", {}));
              bar.firstChild.style.width = pct + "%";
              return h("div", { class: "gc-tour-drive" }, h("b", {}, d.name + " "), h("span", { class: "muted" }, gb(free) + " free"), bar);
            }));
          } catch (e) { box.replaceChildren(h("p", {}, "Connect your SSD — Ghost needs a destination drive.")); }
        })();
      },
      function () {
        var status = h("p", { class: "gc-tour-waiting" }, "waiting for a card");
        body.replaceChildren(h("h3", {}, "Insert a camera card"),
          h("p", {}, "Ghost scans it in the background and counts the clips."), status,
          h("p", { class: "muted" }, "You can go Back or Skip — scanning continues on Import."));
        stopPoll();
        var check = async function () {
          try {
            var j = await api("/api/offload/scans");
            var scans = (j && j.scans) || [];
            var vols = (j && j.volumes) || [];
            var ready = scans.find(function (s) { return s.state === "ready" || (s.clip_count || 0) > 0; });
            if (ready) {
              var vol = vols.find(function (v) { return String(v.path) === String(ready.volume); });
              var detail = (vol && vol.detail) || "Sony";
              var count = ready.clip_count || ready.total || 0;
              status.className = "gc-tour-found";
              status.textContent = "Found a " + detail + " card, " + count + " clips";
              stopPoll();
              var at = step; setTimeout(function () { if (overlay.isConnected && step === at) go(at + 1); }, 700);
            }
          } catch (e) { /* keep waiting */ }
        };
        check();
        pollTimer = setInterval(check, 1200);
      },
      function () {
        var proj = null;
        try { proj = ctx.project ? ctx.project() : null; } catch (e) { proj = null; }
        if (proj && proj.cutJob) {
          body.replaceChildren(h("h3", {}, "Open a job"),
            h("p", {}, "A job is already open. Use the Resume card on Import to reopen a finished job, or head to Edit."),
            h("p", { class: "muted" }, "A full cut takes about half an hour, so Resume is the fast path in a demo."));
          return;
        }
        var chatTa = document.querySelector("#view-import textarea");
        if (chatTa && !chatTa.value) chatTa.value = PREFILL; // pre-filled; Try it sends it
        var tryBtn = h("button", { type: "button", class: "gc-tour-btn primary" }, "Try it");
        tryBtn.onclick = function () {
          var view = document.querySelector("#view-import");
          var done = false;
          try {
            if (view && typeof view.__gcAsk === "function") { if (chatTa && chatTa.value === PREFILL) chatTa.value = ""; view.__gcAsk(PREFILL); done = true; }
          } catch (e) { /* fall through to form */ }
          if (!done) {
            var ta = view ? view.querySelector("textarea") : null;
            if (ta) {
              ta.value = PREFILL;
              var form = ta.closest("form");
              if (form) { if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); }
            }
          }
          go(step + 1);
        };
        body.replaceChildren(h("h3", {}, "Ask Ghost"),
          h("p", {}, "Say where the footage is and where it should go. Ghost plans first — nothing copies until Transfer."),
          h("p", { class: "gc-tour-prefill" }, PREFILL), h("p", {}, tryBtn),
          h("p", { class: "muted" }, "Typing, the mic, and the starter chips all go the same way."));
      },
      function () {
        body.replaceChildren(h("h3", {}, "Transfer, then Make the cut"),
          h("p", {}, "Transfer streams each file and renames it only when its checksum matches the card. The card is never written."),
          h("p", {}, "Make the cut syncs every clip, picks takes, grades the cameras and chooses an angle per line."),
          h("p", {}, "Honestly: a full cut takes about half an hour. Reopen a finished job from Resume when you need it now."));
      }
    ];
    function renderDots() {
      dots.replaceChildren.apply(dots, steps.map(function (_, i) {
        var d = h("span", { class: i === step ? "on" : "" });
        d.setAttribute("aria-label", "Step " + (i + 1));
        return d;
      }));
    }
    function go(n) {
      stopPoll();
      step = Math.max(0, Math.min(steps.length - 1, n));
      renderDots();
      backBtn.disabled = step === 0;
      nextBtn.textContent = step === steps.length - 1 ? "Finish" : "Next";
      steps[step]();
      try { nextBtn.focus(); } catch (e) { /* noop */ }
    }
    backBtn.onclick = function () { go(step - 1); };
    skipBtn.onclick = function () { close(true); };
    nextBtn.onclick = function () {
      if (step === steps.length - 1) close(false);
      else go(step + 1);
    };
    go(0);
  }
  window.GCTour = { open: open };
  // The ? Help button in the top bar reopens the tour any time (scripts load at the end of <body>).
  var helpBtn = document.getElementById("helpBtn");
  if (helpBtn) helpBtn.addEventListener("click", function () { open(window.GC); });
})();

