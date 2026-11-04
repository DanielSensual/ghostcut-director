// GhostCut Director — the Import page (ONBOARD). Classic script: window.GCImport.mount(el, ctx).
// Moved out of app.js and rebuilt: card tiles, a destination meter, Ghost's chat with starter chips, the plan card,
// one persistent transfer progress element, shoot bins and a Resume card. Same engine endpoints as before; works
// offline (Muse is optional: when it is unavailable the engine's built-in planner reads the request).
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var STAGES = { sync: "Syncing cameras", words: "Reading the words", transcribe: "Reading the words", tighten: "Picking takes",
    grade: "Grading", angles: "Choosing angles", export: "Timelines", reframe: "Vertical", captions: "Captions", qc: "Checking the cut" };
  var STATE_LABEL = { done: "done", running: "running", skipped: "skipped", failed: "failed", pending: "waiting" };
  var MUSE_NOTE = "Muse unavailable, using the built-in planner.";

  function gbs(n) {
    var v = Number(n || 0);
    if (v >= 1e12) return (v / 1e12).toFixed(2) + " TB";
    if (v >= 1e9) return (v / 1e9).toFixed(v >= 1e11 ? 0 : 1) + " GB";
    if (v >= 1e6) return Math.round(v / 1e6) + " MB";
    return Math.round(v) + " B";
  }
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function dayLabel(s) {
    if (!s) return "";
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s));
    return m ? MONTHS[+m[2] - 1] + " " + (+m[3]) : String(s).slice(0, 10);
  }
  function dayRange(a, b) {
    var x = dayLabel(a), y = dayLabel(b);
    if (!x && !y) return "";
    return x === y || !y ? x : x + " – " + y;
  }
  function isMuseDown(msg) { return /\b402\b|credit|billing|payment required|quota/i.test(String(msg || "")); }

  // Card glyphs: small inline SVGs chosen from the volume's detail string (no images, no network).
  function glyphKind(detail) {
    var d = String(detail || "").toLowerCase();
    if (/sony|xavc|fx3|fx6|a7/.test(d)) return "sony";
    if (/dji|osmo|mavic|pocket/.test(d)) return "dji";
    if (/gopro|hero/.test(d)) return "gopro";
    if (/iphone|apple|ios/.test(d)) return "iphone";
    if (/zoom|rode|tascam|recorder|audio|wav/.test(d)) return "recorder";
    return "card";
  }
  var GLYPH = {
    sony: '<rect x="3" y="7" width="26" height="18" rx="3"/><circle cx="18" cy="16" r="5.5"/><rect x="6" y="4" width="7" height="3" rx="1"/>',
    dji: '<rect x="11" y="3" width="10" height="16" rx="3"/><circle cx="16" cy="9" r="3"/><rect x="13" y="19" width="6" height="10" rx="2"/>',
    gopro: '<rect x="5" y="8" width="22" height="17" rx="4"/><circle cx="19" cy="16.5" r="4.5"/><rect x="8" y="11" width="5" height="3" rx="1"/>',
    iphone: '<rect x="9" y="3" width="14" height="26" rx="3.5"/><circle cx="13.5" cy="7.5" r="1.8"/><line x1="14" y1="26" x2="18" y2="26"/>',
    recorder: '<rect x="9" y="10" width="14" height="19" rx="3"/><rect x="11" y="3" width="10" height="8" rx="4"/><line x1="13" y1="16" x2="19" y2="16"/><line x1="13" y1="20" x2="19" y2="20"/>',
    card: '<path d="M8 4h12l6 6v18H8z"/><line x1="12" y1="4" x2="12" y2="9"/><line x1="16" y1="4" x2="16" y2="9"/><line x1="20" y1="5" x2="20" y2="9"/>',
    drive: '<rect x="4" y="9" width="24" height="15" rx="3"/><circle cx="23" cy="16.5" r="1.6"/><line x1="8" y1="16.5" x2="16" y2="16.5"/>'
  };
  function glyph(kind) {
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 32 32");
    svg.setAttribute("class", "imp-glyph g-" + kind);
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = GLYPH[kind] || GLYPH.card;
    return svg;
  }
  function mark(size, mood) {
    try { if (window.GhostMark) return window.GhostMark(size, mood); } catch (e) { /* glyph below */ }
    var s = document.createElement("span");
    s.className = "imp-mark-glyph";
    s.textContent = "◆";
    return s;
  }

  function mount(el, ctx) {
    ctx = ctx || window.GC || {};
    var h = ctx.h, api = ctx.api, fmt = ctx.fmt || function (t) { return Math.round(t) + " s"; };
    var LIVE = typeof ctx.LIVE === "function" ? !!ctx.LIVE() : !!ctx.LIVE;
    var volumes = [], scans = [], selected = {}, dest = "", plan = null, shootDir = "", wantCut = false;
    var offTimer = 0, cutTimer = 0, scanTimer = 0, planSeq = 0, metaKey = false;

    // ---------- skeleton ----------
    var strip = h("div", { class: "imp-strip", role: "list" },
      ["Import", "Story", "Edit", "Deliver"].map(function (n, i) {
        return h("span", { role: "listitem", class: i === 0 ? "on" : "" }, h("i", {}, String(i + 1)), n);
      }));
    var cardsBox = h("div", { class: "imp-cards" });
    var scanBox = h("div", { class: "ghost-scan" });
    var destBox = h("div", { class: "imp-dests" });
    var shootBox = h("div", { class: "imp-shoot" });
    var chatBox = h("div", { class: "ghost-chat" });
    var planBox = h("div", { class: "imp-plan" });
    var liveBox = h("div", { class: "imp-progress", hidden: true });
    var cutBox = h("div", { class: "imp-cut" });
    var statusLine = h("span", { class: "imp-status" }, LIVE ? "Looking at your drives…" : "Engine offline");
    var input = h("textarea", { rows: 2, placeholder: "Ask Ghost… e.g. the footage is on the SD card, transfer it to my SSD" });
    var micB = h("button", { class: "icon mic", type: "button", title: "Hold to talk" }, "🎙");
    var helpB = h("button", { type: "button", class: "imp-help", title: "Tour and help" }, "Help");
    helpB.onclick = function () { if (window.GCTour) window.GCTour.open(ctx); };

    // ---------- chat ----------
    function say(who, text, extra) {
      var cls = "msg " + (who === "you" ? "you" : "muse") + (extra ? " " + extra : "");
      chatBox.append(h("div", { class: cls },
        who === "you" ? text : [h("div", { class: "who" }, "Ghost"), h("div", {}, text)]));
      chatBox.scrollTop = chatBox.scrollHeight;
    }
    function note(text) { chatBox.append(h("div", { class: "imp-note" }, text)); chatBox.scrollTop = chatBox.scrollHeight; }

    // ---------- tiles ----------
    function scanFor(v) { return scans.find(function (s) { return String(s.volume) === String(v.path); }); }
    var lastSig = "";
    function renderVolumes() {
      // Polls arrive every 1.2 s: redraw only when something changed, so tiles keep focus and do not re-animate.
      var sig = JSON.stringify([volumes, scans, selected, dest, plan && [plan.destination, plan.bytes_to_copy, plan.enough_space], metaKey]);
      if (sig === lastSig) return;
      lastSig = sig;
      var cams = volumes.filter(function (v) { return v.kind === "camera_card"; });
      var dests = volumes.filter(function (v) { return v.kind === "destination"; });
      if (!cams.length) {
        cardsBox.replaceChildren(h("div", { class: "imp-empty" },
          h("div", { class: "imp-wait" }, glyph("card")),
          h("b", {}, "Insert a card"),
          h("span", {}, "Ghost scans it the moment it mounts. The card is only ever read.")));
      } else {
        cardsBox.replaceChildren.apply(cardsBox, cams.map(function (v) {
          var s = scanFor(v) || {};
          var ready = s.state === "ready";
          var done = Number(s.done || 0), total = Number(s.total || 0);
          var fill = h("i", {});
          fill.style.width = (ready ? 100 : total ? Math.min(100, (done / total) * 100) : 4) + "%";
          var box = h("input", { type: "checkbox", "aria-label": "Copy " + v.name });
          box.checked = !!selected[v.path];
          box.onchange = function () { if (box.checked) selected[v.path] = true; else delete selected[v.path]; tile.classList.toggle("sel", box.checked); planByClick(); };
          var facts = [];
          if (s.camera_labels && s.camera_labels.length) facts.push(s.camera_labels.join(", "));
          var range = dayRange(s.date_start, s.date_end);
          if (range) facts.push(range);
          var tile = h("label", { class: "imp-tile" + (box.checked ? " sel" : "") + (s.state === "error" ? " bad" : "") },
            box, glyph(glyphKind(v.detail)),
            h("div", { class: "imp-tile-main" },
              h("div", { class: "imp-tile-top" }, h("b", {}, v.name),
                ready ? h("span", { class: "chip ready" }, "Ready") : s.state === "error" ? h("span", { class: "chip bad" }, "Scan error") : h("span", { class: "chip" }, "Scanning")),
              h("div", { class: "imp-tile-sub" }, [v.detail || "Camera card",
                s.clip_count ? s.clip_count + " clips" : null, s.total_bytes ? gbs(s.total_bytes) : null].filter(Boolean).join(" · ")),
              facts.length ? h("div", { class: "imp-tile-facts" }, facts.join(" · ")) : null,
              h("div", { class: "imp-mini" }, fill),
              h("div", { class: "imp-tile-count" }, s.state === "error" ? (s.message || "Could not read this card") : (ready ? done + "/" + total + " scanned" : "Scanning " + done + "/" + (total || "…")))));
          return tile;
        }));
      }
      if (!dests.length) {
        destBox.replaceChildren(h("div", { class: "imp-empty small" }, glyph("drive"), h("b", {}, "Connect your SSD"),
          h("span", {}, "Ghost copies to a destination drive and verifies every file.")));
      } else {
        if (!dest || !dests.some(function (d) { return d.path === dest; })) dest = dests[0].path;
        destBox.replaceChildren.apply(destBox, dests.map(function (d) {
          var total = Number(d.total_bytes || 0), free = Number(d.free_bytes || 0);
          var used = total ? Math.max(0, (total - free) / total) * 100 : 0;
          var copy = plan && plan.destination === d.path ? Number(plan.bytes_to_copy || 0) : 0;
          var copyPct = total ? Math.min(100 - used, (copy / total) * 100) : 0;
          var short = plan && plan.destination === d.path && plan.enough_space === false;
          var usedI = h("i", { class: "used" }), copyI = h("i", { class: "copy" + (short ? " short" : "") });
          usedI.style.width = used + "%";
          copyI.style.width = copyPct + "%";
          var radio = h("input", { type: "radio", name: "imp-dest", "aria-label": "Copy to " + d.name });
          radio.checked = d.path === dest;
          radio.onchange = function () { dest = d.path; renderVolumes(); planByClick(); };
          return h("label", { class: "imp-tile dest" + (radio.checked ? " sel" : "") + (short ? " bad" : "") }, radio, glyph("drive"),
            h("div", { class: "imp-tile-main" },
              h("div", { class: "imp-tile-top" }, h("b", {}, d.name), h("span", { class: "chip" }, gbs(free) + " free")),
              h("div", { class: "imp-meter" }, usedI, copyI),
              h("div", { class: "imp-tile-count" }, copy ? gbs(copy) + " to copy" + (short ? " · not enough space" : "") : gbs(total) + " drive")));
        }));
      }
      scanBox.replaceChildren.apply(scanBox, scans.filter(function (s) { return s.state === "scanning"; })
        .map(function (s) { return h("div", { class: "ghost-scanline" }, "Scanning " + s.name + " · " + (s.done || 0) + "/" + (s.total || 0)); }));
      var ready = scans.filter(function (s) { return s.state === "ready"; }).length;
      statusLine.textContent = !LIVE ? "Engine offline" : (cams.length ? ready + "/" + cams.length + " card" + (cams.length === 1 ? "" : "s") + " ready" : "Waiting for a card")
        + (dests.length ? " · " + dests[0].name : "") + (metaKey ? " · Muse" : " · built-in planner");
    }
    async function planByClick() {
      var cards = Object.keys(selected);
      if (!cards.length || !dest) return;
      var seq = ++planSeq;
      try {
        var j = await api("/api/offload/plan", { cards: cards, destination: dest });
        if (seq !== planSeq) return;
        say("ghost", j.reply || "Here is the transfer to plan.");
        renderPlan(j);
      } catch (e) { say("ghost", e.message); }
    }

    // ---------- plan card ----------
    function renderPlan(j) {
      plan = j;
      shootDir = j.shoot_dir || shootDir;
      var transfer = h("button", { class: "primary", type: "button" }, "Transfer");
      var change = h("button", { type: "button" }, "Change");
      transfer.onclick = function () { startTransfer(transfer); };
      change.onclick = function () { input.focus(); say("ghost", "Tell me what to change — another card, drive or shoot name — and I will re-plan. Nothing has been copied."); };
      var includeBtn = (j.older_count > 0 && !j.include_all) ? h("button", { type: "button" }, "Include them") : null;
      if (includeBtn) includeBtn.onclick = async function () {
        includeBtn.disabled = true;
        try {
          var all = await api("/api/offload/plan", { cards: (j.cards || []).map(function (c) { return c.volume; }),
            destination: j.destination, project: j.project, include_all: true });
          say("ghost", all.reply || "Here is the transfer to plan.");
          renderPlan(all);
        } catch (e) { say("ghost", e.message); includeBtn.disabled = false; }
      };
      var rows = (j.cards || []).map(function (c) {
        return h("div", { class: "vol plan-row" + (c.skipped ? " skipped" : "") }, glyph(glyphKind(c.detail || c.camera)),
          h("b", {}, c.name), h("span", { class: "muted" }, c.camera || ""),
          c.skipped ? h("span", {}, "skipped · no clips from " + (j.shoot_day_label || dayLabel(j.shoot_day)))
            : h("span", {}, c.clip_count + " clips · " + dayRange(c.date_start, c.date_end) + " · " + gbs(c.total_bytes)));
      });
      planBox.replaceChildren(h("div", { class: "box ghost-plan" },
        h("h3", {}, "Transfer plan"), rows,
        h("div", { class: "vol plan-total" }, h("b", {}, "To " + (j.shoot_dir || "")),
          h("span", {}, gbs(j.bytes_to_copy || 0) + " to copy · " + gbs(j.free_bytes || 0) + " free")),
        j.older_summary ? h("p", { class: "ghost-older" }, j.older_summary) : null,
        h("p", { class: j.enough_space ? "imp-ok" : "err" }, j.enough_space ? "Space check passed. Nothing is copied until you transfer." : "Not enough free space on the destination."),
        h("div", { class: "acts" }, transfer, change, includeBtn)));
      renderVolumes();
    }

    // ---------- transfer progress: one element, updated in place ----------
    var pFill = h("i", {}), pPct = h("b", {}, "0%"), pFile = h("span", { class: "imp-file" }), pFacts = h("span", { class: "muted" });
    liveBox.append(h("div", { class: "imp-progress-top" }, pPct, pFile), h("div", { class: "ghost-bar big" }, pFill), pFacts);
    var rate = { t: 0, b: 0, bps: 0 };
    function showProgress(p) {
      liveBox.hidden = false;
      var done = Number(p.bytes_done || 0), total = Number(p.bytes_total || 0);
      var pct = total ? Math.min(100, (done / total) * 100) : 0;
      var now = Date.now();
      if (rate.t && done > rate.b) { var inst = (done - rate.b) / ((now - rate.t) / 1000); rate.bps = rate.bps ? rate.bps * 0.7 + inst * 0.3 : inst; }
      rate.t = now; rate.b = done;
      pFill.style.width = pct + "%";
      pPct.textContent = Math.floor(pct) + "%";
      pFile.textContent = p.file || "";
      pFacts.textContent = [gbs(done) + " / " + gbs(total), rate.bps ? gbs(rate.bps) + "/s" : null,
        p.eta_s ? "about " + fmt(p.eta_s) + " left" : null].filter(Boolean).join(" · ");
    }
    async function startTransfer(btn) {
      btn.disabled = true;
      try { await api("/api/offload/start", {}); say("ghost", "Copying — every file is checked against the card before it is kept."); followTransfer(btn); }
      catch (e) { say("ghost", e.message); btn.disabled = false; }
    }
    function followTransfer(btn) {
      clearInterval(offTimer);
      rate = { t: 0, b: 0, bps: 0 };
      offTimer = setInterval(async function () {
        try {
          var s = await api("/api/offload/status");
          showProgress(s.progress || {});
          if (s.state === "done" || s.state === "done_with_failures") {
            clearInterval(offTimer);
            if (s.state === "done") { pFill.style.width = "100%"; pPct.textContent = "100%"; }
            say("ghost", s.message || "Transfer finished.");
            if (s.shoot_dir) loadShoot(s.shoot_dir, true);
          }
          if (s.state === "failed") { clearInterval(offTimer); say("ghost", s.message); if (btn) btn.disabled = false; }
        } catch (e) { /* keep polling */ }
      }, 800);
    }

    // ---------- shoot bins + cut ----------
    async function loadShoot(dir, justCopied) {
      shootDir = dir;
      shootBox.replaceChildren(h("h3", {}, "Shoot"), h("div", { class: "ghost-thumbs" },
        [0, 1, 2, 3].map(function () { return h("figure", { class: "skel" }, h("div", { class: "skel-img" }), h("figcaption", {}, " ")); })));
      try {
        var j = await api("/api/offload/shoot?shoot=" + encodeURIComponent(dir));
        var bins = (j.bins || []).map(function (b) {
          var secs = b.clips.reduce(function (a, c) { return a + (c.duration_s || 0); }, 0);
          return h("div", { class: "box imp-bin" },
            h("header", {}, h("b", {}, b.camera), h("span", {}, b.clips.length + " clip" + (b.clips.length === 1 ? "" : "s") + (secs ? " · " + fmt(secs) : ""))),
            h("div", { class: "ghost-thumbs" }, b.clips.map(function (c) {
              var src = "/api/offload/thumb?shoot=" + encodeURIComponent(j.shoot_dir || dir) + "&file=" + encodeURIComponent(c.path || "");
              return h("figure", {}, h("div", { class: "imp-thumb" }, h("img", { src: src, alt: "", loading: "lazy" }),
                c.duration_s != null ? h("span", { class: "dur" }, fmt(c.duration_s)) : null), h("figcaption", {}, c.name));
            })));
        });
        shootBox.replaceChildren.apply(shootBox, [h("h3", {}, "Shoot")].concat(bins.length ? bins : [h("p", { class: "muted" }, "No clips in this shoot folder yet.")]));
      } catch (e) { shootBox.replaceChildren(h("h3", {}, "Shoot"), h("p", { class: "err" }, e.message)); }
      renderCut(justCopied && wantCut);
    }
    function renderCut(offer) {
      var cutBtn = h("button", { class: "primary", type: "button" }, "Make the cut");
      var stages = h("div", { class: "imp-stages" }), msg = h("div", { class: "ghost-cutstat" });
      cutBtn.onclick = async function () {
        cutBtn.disabled = true;
        try { await api("/api/offload/cut", { shoot_dir: shootDir }); msg.textContent = "Cutting… a full cut takes about half an hour."; followCut(cutBtn, stages, msg); }
        catch (e) { msg.textContent = e.message; cutBtn.disabled = false; }
      };
      cutBox.replaceChildren(h("div", { class: "box imp-cutcard" },
        h("h3", {}, "Make the cut"),
        h("p", { class: "muted" }, offer ? "You asked for the cut after the copy. It runs in the background: sync, takes, grade and angles, about half an hour."
          : "Sync, takes, grade and angles run in the background on the copied shoot, about half an hour."),
        h("div", { class: "acts" }, cutBtn), stages, msg));
      if (offer) say("ghost", "The copy is verified. Start the cut now? It takes about half an hour.");
    }
    function followCut(btn, stages, msg) {
      clearInterval(cutTimer);
      cutTimer = setInterval(async function () {
        try {
          var s = await api("/api/offload/cut/status");
          var st = s.stages || {};
          stages.replaceChildren.apply(stages, Object.keys(st).map(function (k) {
            var v = String(st[k] || "");
            return h("span", { class: "stage " + v }, (STAGES[k] || k) + " · " + (STATE_LABEL[v] || v));
          }));
          msg.textContent = s.message || "";
          if (s.state === "done") { clearInterval(cutTimer); location.hash = "#edit"; setTimeout(function () { location.reload(); }, 1200); }
          if (s.state === "failed") { clearInterval(cutTimer); btn.disabled = false; }
        } catch (e) { /* keep polling */ }
      }, 1200);
    }

    // ---------- asking Ghost ----------
    function handleAsk(j) {
      if (j.text) say("ghost", "I heard: " + j.text);
      say("ghost", j.reply || "Here is the transfer to plan.");
      if (j.note) note(j.note);
      if (j.intent && j.intent.make_cut) wantCut = true;
      if (j.opened) { location.hash = "#edit"; setTimeout(function () { location.reload(); }, 800); return; }
      if (j.started) { say("ghost", "Transfer started."); followTransfer(null); return; }
      if (j.shoot_dir) {
        (j.cards || []).forEach(function (c) { if (!c.skipped) selected[c.volume] = true; });
        if (j.destination) dest = j.destination;
        renderPlan(j);
      }
    }
    async function askGhost(text) {
      say("you", text);
      try { handleAsk(await api("/api/offload/ask", { text: text })); }
      catch (e) {
        if (isMuseDown(e.message)) say("ghost", MUSE_NOTE + " Type the request and I will plan it.");
        else say("ghost", "That didn't go through: " + e.message);
      }
    }
    el.__gcAsk = askGhost; // the tour's "Try it" submits through the same path as the form
    var form = h("form", { class: "ask ghost-ask" }, input, micB, h("button", { class: "primary", type: "submit" }, "Send"));
    form.onsubmit = function (e) { e.preventDefault(); var v = input.value.trim(); if (v) { input.value = ""; askGhost(v); } };
    input.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
    var chips = h("div", { class: "imp-chips" }, ["Transfer my SD card to the SSD", "What can you do?", "Open the last job"].map(function (t) {
      return h("button", { type: "button", class: "chip-btn", onclick: function () { askGhost(t); } }, t);
    }));

    // Ghost by voice: hold to talk, heard by the engine's voice path. Meta transcription may be unavailable; then say so.
    var grec = null, gchunks = [];
    function gMime() { return (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported("audio/webm;codecs=opus")) ? "audio/webm;codecs=opus" : ""; }
    async function gStart() {
      if (!LIVE || grec) return;
      if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) { say("ghost", "The microphone is not available in this window — open this page in your browser to talk, or type the request."); return; }
      var stream;
      try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (e) { say("ghost", "The microphone is not available in this window — type the request instead."); return; }
      gchunks = [];
      var m = gMime();
      grec = m ? new MediaRecorder(stream, { mimeType: m }) : new MediaRecorder(stream);
      grec.ondataavailable = function (e) { if (e.data.size) gchunks.push(e.data); };
      grec.start();
      grec._stream = stream;
      micB.classList.add("rec");
      micB.textContent = "●";
    }
    async function gStop() {
      if (!grec) return;
      var done = grec, mime = grec.mimeType || "";
      grec = null;
      micB.classList.remove("rec");
      micB.textContent = "🎙";
      var blob = await new Promise(function (res) { done.onstop = function () { res(new Blob(gchunks, { type: mime })); }; done.stop(); });
      (done._stream.getTracks() || []).forEach(function (t) { t.stop(); });
      var bytes = new Uint8Array(await blob.arrayBuffer()), bin = "";
      for (var i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
      say("you", "🎙 (voice request)");
      try { handleAsk(await api("/api/offload/voice", { audio_b64: btoa(bin), mime: mime })); }
      catch (e) { say("ghost", isMuseDown(e.message) ? "Voice needs Muse, which is unavailable right now — type the request and the built-in planner will read it." : "That didn't go through: " + e.message); }
    }
    micB.addEventListener("pointerdown", function (e) { e.preventDefault(); gStart(); });
    ["pointerup", "pointerleave", "pointercancel"].forEach(function (ev) { micB.addEventListener(ev, gStop); });

    // ---------- resume a job ----------
    var openInput = h("input", { type: "text", placeholder: "Job folder, e.g. the folder a finished cut wrote" });
    var openBtn = h("button", { type: "button", class: "primary" }, "Open job");
    openBtn.onclick = async function () {
      var v = openInput.value.trim();
      if (!v) { openInput.focus(); return; }
      openBtn.disabled = true;
      try { await api("/api/job/open", { path: v }); location.hash = "#edit"; location.reload(); }
      catch (e) { say("ghost", e.message); openBtn.disabled = false; }
    };
    openInput.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); openBtn.click(); } });
    var resume = h("div", { class: "box imp-resume" }, h("h3", {}, "Resume a job"),
      h("p", { class: "muted" }, "Reopen a finished cut and go straight to Edit."),
      h("div", { class: "ghost-open" }, openInput, openBtn));

    // ---------- page ----------
    el.classList.add("imp");
    el.append(
      h("div", { class: "imp-head" }, h("div", {}, h("h2", {}, "Import"),
        h("p", { class: "lede" }, "Ghost moves camera cards to your destination drive and verifies every file. Tell it what to copy — nothing is copied until you transfer.")), strip),
      h("div", { class: "imp-grid" },
        h("section", { class: "imp-left" },
          h("h3", { class: "imp-sec" }, "Camera cards"), cardsBox, scanBox,
          h("h3", { class: "imp-sec" }, "Destination"), destBox,
          shootBox, cutBox, resume),
        h("section", { class: "imp-right box" },
          h("header", { class: "imp-ghost-head" }, h("span", { class: "imp-mark" }, mark(40, "idle")),
            h("div", {}, h("b", {}, "Ghost"), statusLine), helpB),
          chips, chatBox, planBox, liveBox, form)));

    // Polling stops while the view is hidden and resumes when it is shown again.
    async function pollScans() {
      if (!el.isConnected || el.hidden) { stopScans(); return; }
      try { var j = await api("/api/offload/scans"); volumes = j.volumes || []; scans = j.scans || []; renderVolumes(); }
      catch (e) { /* engine away; the next tick retries */ }
    }
    function stopScans() { if (scanTimer) { clearInterval(scanTimer); scanTimer = 0; } }
    function startScans() { if (!LIVE || scanTimer) return; pollScans(); scanTimer = setInterval(pollScans, 1200); }
    try { new MutationObserver(function () { if (el.hidden) stopScans(); else startScans(); }).observe(el, { attributes: true, attributeFilter: ["hidden"] }); } catch (e) { /* old engine */ }

    renderVolumes();
    if (!LIVE) { say("ghost", "Ghost needs the engine: run the app server, then ask here."); return; }
    (async function () {
      try { var st = await api("/api/settings"); metaKey = !!(st && st.meta_key); } catch (e) { /* rules */ }
      try { var b = await api("/api/offload/briefing"); say("ghost", b.reply); }
      catch (e) { say("ghost", "Tell me where the footage is — for example, the footage is on the SD card, transfer it to my SSD."); }
      // A transfer that finished (or is running) before this window opened resumes instead of an empty chat.
      try {
        var s = await api("/api/offload/status");
        if (s.state === "running") { say("ghost", "A transfer is running — following it."); followTransfer(null); }
        else if ((s.state === "done" || s.state === "done_with_failures") && s.shoot_dir) { say("ghost", s.message || "The last transfer finished."); loadShoot(s.shoot_dir, false); }
      } catch (e) { /* nothing to resume */ }
      try { var c = await api("/api/offload/cut/status"); if (c.state === "running") { renderCut(false); var card = cutBox.querySelector(".imp-cutcard"); card.querySelector("button").disabled = true; followCut(card.querySelector("button"), card.querySelector(".imp-stages"), card.querySelector(".ghost-cutstat")); } } catch (e) { /* no cut */ }
    })();
    startScans();
  }

  window.GCImport = { mount: mount, glyphKind: glyphKind };
})();
