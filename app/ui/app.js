// GhostCut Director — app UI. Talks to the local engine (app/engine.py) when it serves the page; opened as a plain
// file it falls back to window.PROJECT (demo/project.js) with scripted Muse replies. No build step.
(async () => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const h = (tag, attrs = {}, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") el.className = v;
      else if (k === "style") el.style.cssText = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) el.setAttribute(k, v);
    }
    for (const k of kids.flat()) if (k != null) el.append(k.nodeType ? k : document.createTextNode(k));
    return el;
  };
  const fmt = (t) => {
    t = Math.max(0, t);
    const m = Math.floor(t / 60), s = Math.floor(t % 60);
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  };
  const api = async (path, body) => {
    const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  };
  // ONBOARD: helpers for import.js / onboard.js (merged: other builds may also set window.GC)
  window.GC = Object.assign(window.GC || {}, { h, $, $$, api, fmt, LIVE: () => LIVE, show: (v) => show(v),
    openConn: () => { renderConn(); connDlg.hidden = false; }, project: () => P });

  // ---------- data: the engine if it's there, else the static demo ----------
  let P = window.PROJECT, LIVE = false, EDITS = [], ENGINE = {};
  if (location.protocol.startsWith("http")) {
    try { ({ project: P, edits: EDITS, engine: ENGINE } = await api("/api/project")); LIVE = true; } catch { /* static demo */ }
  }
  if (!P) {
    $("#noVideo").hidden = false;
    $("#projTitle").textContent = "No demo project";
    return;
  }
  // Render state from the engine: {state: idle|running|refused|failed|failed_verification|cancelled|promoted|interrupted, version}.
  let RS = ENGINE?.render || { state: "idle", version: null };
  const BUSY = { accept: "Rendering — accept after it finishes", undo: "Rendering — undo after it finishes", revert: "Rendering — revert after it finishes" };
  const busy = (btn, kind) => {
    btn.dataset.busy = kind;
    if (RS.state === "running") { btn.disabled = true; btn.title = BUSY[kind]; }
    return btn;
  };
  const SEC = ["var(--sec0)", "var(--sec1)", "var(--sec2)", "var(--sec3)"];
  const VQ = P.version ? `?v=${encodeURIComponent(P.version)}` : ""; // each render swaps the media under the same names
  const thumb = (t) => `demo/thumbs/${String(Math.min(Math.floor(t / 2) + 1, Math.floor(P.duration / 2))).padStart(4, "0")}.jpg${VQ}`;
  const pct = (t) => `${(t / P.duration) * 100}%`;
  const LIB = () => Object.fromEntries((P.library || []).map((e) => [e.tag, e]));
  // The preview is the middle frame of the insert Render places (render_plan.in_point): mid of the shot's clean range in
  // file seconds. V, W and P shots are 2× prerenders whose library range is in on-screen seconds, hence ÷2. No range (or
  // an empty one): the middle of the file. Sent to the ms, so 8.75 doesn't round to a frame 50 ms away.
  const frameUrl = (tag) => {
    const e = LIB()[tag] || {}, k = /^[VWP]/.test(tag) ? 2 : 1, b = e.best;
    const t = b && b[1] > b[0] ? (b[0] + b[1]) / 2 / k : (e.dur || 0) / 2;
    return `/api/frame?tag=${encodeURIComponent(tag)}&t=${t.toFixed(3)}`;
  };
  const MANUAL = "inserted to cover the background";

  // ---------- top bar ----------
  $("#projTitle").textContent = P.title;
  $("#projSub").textContent = [P.client, P.shootDate && `shot ${P.shootDate}`, fmt(P.duration)].filter(Boolean).join(" · ");
  const spendBtn = $("#spendBtn"), pop = $("#spendPop");
  function renderSpend() {
    if (!P.spend) { spendBtn.hidden = true; return; }
    const muse = ENGINE.museSpent || 0, total = P.spend.total + muse;
    spendBtn.textContent = `$${total.toFixed(2)} Meta`;
    const rows = [...P.spend.rows];
    if (ENGINE.museCalls) rows.push(["Muse edit requests (Spark)", ENGINE.museCalls, muse]);
    pop.replaceChildren(
      h("h4", { style: "margin-top:0" }, "Meta API spend · this project"),
      h("table", {},
        ...rows.map(([what, n, usd]) => h("tr", {}, h("td", {}, what), h("td", {}, `×${n}`), h("td", {}, `$${usd.toFixed(2)}`))),
        h("tr", { class: "tot" }, h("td", {}, "Total, all versions"), h("td", {}), h("td", {}, `$${total.toFixed(2)}`))),
      h("p", { class: "muted", style: "margin:10px 0 0;font-size:11.5px" }, "Rows above the Muse line were entered by hand from the spend ledger. Camera originals stay on this Mac: Spark receives 360p proxies and transcripts. The optional voice-isolation step (mix --isolate) sends that audio to ElevenLabs."));
  }
  renderSpend();
  spendBtn.onclick = (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; };
  document.addEventListener("click", (e) => { if (!pop.contains(e.target)) pop.hidden = true; });

  // ---------- connections: outside-service keys (per-machine, never shown back) ----------
  const connBtn = $("#connBtn"), connDlg = $("#connDlg");
  let CONN = null;
  const CAP_LABEL = { sfx: "Sound effects", music: "music", isolation: "voice isolation" };
  async function loadConn() {
    if (!LIVE) { connBtn.hidden = true; return; }
    try {
      CONN = (await api("/api/connections")).providers || [];
    } catch { CONN = null; }
    if (!CONN) { connBtn.hidden = true; return; }
    const n = CONN.filter((p) => p.connected).length;
    connBtn.textContent = n ? `Connections · ${n} connected` : "Connections · none connected";
  }
  function connStatus(p) {
    if (!p.connected) return "Not connected";
    if (p.source === "env") return `Connected — from ${(p.env_vars || []).join(", ")}`;
    return "Connected — saved in Keychain";
  }
  function connCaps(p) {
    if ((p.capabilities || []).length) return p.capabilities.map((c) => CAP_LABEL[c] || c).join(", ");
    return p.unavailable_reason || "";
  }
  function closeConn() {
    $$("input", connDlg).forEach((i) => (i.value = ""));
    connDlg.hidden = true;
  }
  function renderConn() {
    const body = $("#connDlgBody");
    const blocks = (CONN || []).map((p) => {
      const status = h("div", { class: "conn-status" }, connStatus(p));
      const caps = h("div", { class: "muted" }, connCaps(p));
      const msg = h("div", { class: "conn-msg", id: `connMsg-${p.id}` });
      const inputs = p.id === "higgsfield"
        ? [h("input", { type: "password", autocomplete: "off", placeholder: "Key ID" }),
           h("input", { type: "password", autocomplete: "off", placeholder: "Key secret" })]
        : [h("input", { type: "password", autocomplete: "off", placeholder: `Paste ${p.name} API key` })];
      const saveBtn = h("button", {}, "Save");
      const testBtn = h("button", {}, "Test");
      const removeBtn = h("button", {}, "Remove");
      if (p.source === "env") {
        removeBtn.disabled = true;
        removeBtn.title = `Unset ${(p.env_vars || []).join(" and ")} to disconnect`;
      }
      const say = (text, bad) => msg.replaceChildren(h("span", { class: bad ? "err" : "ok" }, text));
      saveBtn.onclick = async () => {
        const payload = { provider: p.id };
        if (p.id === "higgsfield") {
          payload.key_id = inputs[0].value;
          payload.key_secret = inputs[1].value;
        } else {
          payload.key = inputs[0].value;
        }
        inputs.forEach((i) => (i.value = ""));
        saveBtn.disabled = true;
        try {
          CONN = (await api("/api/connections/add", payload)).providers || CONN;
          renderConn();
          await runConnTest(p.id);
        } catch (e) { say(e.message, true); saveBtn.disabled = false; }
      };
      testBtn.onclick = () => runConnTest(p.id);
      removeBtn.onclick = async () => {
        removeBtn.disabled = true;
        try {
          CONN = (await api("/api/connections/remove", { provider: p.id })).providers || CONN;
          renderConn();
        } catch (e) { say(e.message, true); removeBtn.disabled = false; }
      };
      return h("div", { class: "conn-provider" }, h("b", {}, p.name), status, caps,
        h("div", { class: "conn-inputs" }, ...inputs), h("div", { class: "conn-acts" }, saveBtn, testBtn, removeBtn), msg);
    });
    body.replaceChildren(...blocks);
    const n = (CONN || []).filter((p) => p.connected).length;
    connBtn.textContent = n ? `Connections · ${n} connected` : "Connections · none connected";
  }
  async function runConnTest(id) {
    const block = $("#connDlgBody");
    const msg = $(`#connMsg-${id}`, block);
    const btns = $$("button", block);
    const testBtn = btns.find((b) => b.textContent === "Test");
    if (testBtn) { testBtn.textContent = "Testing…"; testBtn.disabled = true; }
    const say = (text, bad) => msg && msg.replaceChildren(h("span", { class: bad ? "err" : "ok" }, text));
    try {
      const j = await api("/api/connections/test", { provider: id });
      CONN = j.providers || CONN;
      const t = (j.tests || []).find((x) => x.provider === id);
      renderConn();
      const again = $(`#connMsg-${id}`);
      if (again && t) again.replaceChildren(h("span", { class: t.ok ? "ok" : "err" }, t.message));
    } catch (e) { say(e.message, true); }
    const back = $$("button", $("#connDlgBody")).find((b) => b.textContent === "Testing…");
    if (back) { back.textContent = "Test"; back.disabled = false; }
  }
  connBtn.onclick = () => { renderConn(); connDlg.hidden = false; };
  $("#connDlgClose").onclick = closeConn;
  connDlg.addEventListener("click", (e) => { if (e.target === connDlg) closeConn(); });
  loadConn();
  $("#engineDot").classList.toggle("live", LIVE);
  $("#engineDot").title = LIVE ? "Engine connected: Muse runs on Spark" : "Static demo: Muse replies are scripted";

  // Cut-job mode: the project is a director cut job, so voice notes, taste and re-render apply.
  const isCutJob = () => !!(P && P.cutJob);
  const micBtn = $("#micBtn");
  micBtn.hidden = !(LIVE && isCutJob());
  function micBanner() {
    const url = `${location.origin}/app/ui/`;
    $("#micBanner").replaceChildren(
      "The microphone is not available in this window. Open this page in your browser at ",
      h("code", {}, url),
      " to record by voice, or drop a voice memo file here.");
    $("#micBanner").hidden = false;
    micBtn.disabled = true;
  }
  if (LIVE && isCutJob() && !(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) micBanner();

  // ---------- views ----------
  const rendered = {};
  const show = (v) => {
    $$(".view").forEach((el) => (el.hidden = el.id !== `view-${v}`));
    $$("#steps button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
    if (!rendered[v] && PAGES[v]) { PAGES[v]($(`#view-${v}`)); rendered[v] = true; }
    if (v === "edit") layout();
  };
  $$("[data-view]").forEach((b) => (b.onclick = () => show(b.dataset.view)));

  // ---------- player ----------
  const video = $("#video");
  video.poster = `demo/poster.jpg${VQ}`;
  video.src = `demo/video.mp4${VQ}`;
  video.onerror = () => ($("#noVideo").hidden = false);
  $("#tcDur").textContent = fmt(P.duration);
  const playBtn = $("#playBtn");
  const toggle = () => (video.paused ? video.play() : video.pause());
  playBtn.onclick = toggle;
  video.onplay = () => { playBtn.textContent = "❚❚"; tick(); };
  video.onpause = () => (playBtn.textContent = "▶");
  document.addEventListener("keydown", (e) => {
    if (!$("#connDlg").hidden) { if (e.key === "Escape") { $$("input", $("#connDlg")).forEach((i) => (i.value = "")); $("#connDlg").hidden = true; } return; }
    if (!$("#renderDlg").hidden) { if (e.key === "Escape") $("#renderDlg").hidden = true; return; }
    if (/TEXTAREA|INPUT|SELECT/.test(document.activeElement.tagName)) return;
    if (e.code === "Space") { e.preventDefault(); toggle(); }
    if (e.code === "ArrowLeft") seek(video.currentTime - (e.shiftKey ? 5 : 1));
    if (e.code === "ArrowRight") seek(video.currentTime + (e.shiftKey ? 5 : 1));
  });
  const seek = (t) => { video.currentTime = Math.max(0, Math.min(P.duration - 0.05, t)); update(); };

  // ---------- sidebar ----------
  P.sections.forEach((s, i) => {
    $("#sectionList").append(h("li", { onclick: () => seek(s.t + 0.05), "data-i": i },
      h("span", { class: "dot", style: `background:${SEC[i % 4]}` }), s.name, h("span", { class: "len" }, fmt(s.d))));
  });
  (P.media || []).forEach((m) => {
    $("#binList").append(h("li", { class: m.flag ? "flag" : "" }, m.bin, h("span", { class: "n" }, m.count), h("span", { class: "note" }, m.note)));
  });

  // ---------- timeline ----------
  const scroller = $("#tracksScroll"), tracks = $("#tracks"), playhead = $("#playhead");
  let zoom = 1, selectedId = null;
  const place = (el, t, d) => { el.style.left = pct(t); el.style.width = pct(d); return el; };
  const select = (info) => {
    selectedId = info.id;
    $$(".clip.sel").forEach((x) => x.classList.remove("sel"));
    $(`.clip[data-id="${CSS.escape(info.id)}"]`)?.classList.add("sel");
    renderWhy(info); tab("why");
  };
  const PENDING = { drop: "dropped", change: "changed", add: "new" };

  function renderTimeline() {
    ["#trkSections", "#trkBroll", "#trkCards", "#trkTalk"].forEach((s) => $(s).replaceChildren());
    $$("#ruler .flagm").forEach((m) => m.remove());
    P.sections.forEach((s, i) => $("#trkSections").append(place(
      h("div", { class: "clip sec", style: `background:${SEC[i % 4]}`, title: s.name, onclick: () => seek(s.t + 0.05) }, s.name), s.t, s.d)));
    P.broll.forEach((b) => {
      const img = b.pending === "add" || b.pending === "change" ? (LIVE ? frameUrl(b.tag) : "") : thumb(b.t + b.d / 2);
      const el = place(h("div", {
        class: `clip br${b.pending ? ` p-${b.pending}` : ""}`, "data-id": b.id,
        title: `${b.tag}${b.pending ? ` · ${PENDING[b.pending]} · waiting to render` : ""}`,
        style: img ? `background-image:url(${img})` : "",
      }, b.tag), b.t, b.d);
      el.onclick = () => { select({ type: "broll", ...b }); seek(b.t + 0.1); };
      $("#trkBroll").append(el);
    });
    P.cards.forEach((c) => {
      const el = place(h("div", { class: `clip card${c.pending ? ` p-${c.pending}` : ""}`, "data-id": c.id, title: c.lines.join(" / ") }, c.lines.join(" · ")), c.t, c.d);
      el.onclick = () => { select({ type: "card", ...c }); seek(c.t + 0.3); };
      $("#trkCards").append(el);
    });
    P.talk.forEach((c) => {
      const el = place(h("div", { class: "clip tk", "data-id": c.id, title: `${c.topic} · ${c.src}` }, h("img", { src: thumb(c.t + c.d / 2), alt: "" }), h("span", {}, c.topic)), c.t, c.d);
      el.onclick = () => { select({ type: "talk", ...c }); seek(c.t + 0.05); };
      $("#trkTalk").append(el);
    });
    (P.cuts || []).forEach((c) => $("#trkTalk").append(place(
      h("div", { class: "cutspan", title: `Cut ${c.d.toFixed(2)} s · ${c.why || ""} · waiting to render`, onclick: () => seek(c.t - 1) }), c.t, Math.max(c.d, 0.4))));
    (P.findings || []).forEach((f, i) => {
      const m = h("div", { class: `flagm ${f.state}`, title: f.title, style: `left:${pct(f.t)}` });
      m.onclick = (e) => { e.stopPropagation(); tab("review"); pickFinding(i); };
      $("#ruler").append(m);
    });
    if (selectedId) $(`.clip[data-id="${CSS.escape(selectedId)}"]`)?.classList.add("sel");
    $("#pending").hidden = !P.pending;
    $("#pendingN").textContent = `${P.pending} change${P.pending === 1 ? "" : "s"}`;
  }

  const drawEnv = (canvas, data, color) => {
    if (!data) return;
    const w = canvas.clientWidth, hh = canvas.clientHeight, dpr = devicePixelRatio || 1;
    canvas.width = w * dpr; canvas.height = hh * dpr;
    const g = canvas.getContext("2d");
    g.scale(dpr, dpr); g.fillStyle = color;
    const per = data.length / w;
    for (let x = 0; x < w; x++) {
      let v = 0;
      for (let k = Math.floor(x * per); k < Math.floor((x + 1) * per) + 1 && k < data.length; k++) v = Math.max(v, data[k]);
      const y = Math.max(1, v * v * (hh - 4));
      g.fillRect(x, (hh - y) / 2, 1, y);
    }
  };
  const ruler = () => {
    $$("#ruler .tick").forEach((t) => t.remove());
    const pxPerSec = tracks.clientWidth / P.duration;
    const step = [5, 10, 15, 30, 60, 120, 300].find((s) => s * pxPerSec >= 70) || 300;
    for (let t = 0; t < P.duration; t += step) $("#ruler").prepend(h("div", { class: "tick", style: `left:${pct(t)}` }, fmt(t)));
  };
  function layout() {
    tracks.style.width = `${scroller.clientWidth * zoom}px`;
    ruler();
    drawEnv($("#cvVoice"), P.env.voice, "#2dd4bf");
    drawEnv($("#cvMusic"), P.env.music, "#7c6cf2");
    update();
  }
  $("#zoom").oninput = (e) => {
    const center = video.currentTime / P.duration;
    zoom = +e.target.value; layout();
    scroller.scrollLeft = center * tracks.clientWidth - scroller.clientWidth / 2;
  };
  tracks.addEventListener("click", (e) => {
    if (e.target.closest(".clip, .flagm, .cutspan")) return;
    const r = tracks.getBoundingClientRect();
    seek(((e.clientX - r.left) / r.width) * P.duration);
  });
  addEventListener("resize", layout);

  let segI = -1, wordEl = null;
  function update() {
    const t = video.currentTime || 0;
    $("#tcNow").textContent = fmt(t);
    playhead.style.left = pct(t);
    const s = P.sections.findIndex((x) => t >= x.t && t < x.t + x.d);
    const c = P.talk.find((x) => t >= x.t && t < x.t + x.d);
    $("#where").textContent = [P.sections[s]?.name, c?.topic].filter(Boolean).join(" › ");
    $$("#sectionList li").forEach((li, i) => li.classList.toggle("on", i === s));
    if (zoom > 1) { // keep the playhead in view after seeks and while playing
      const x = (t / P.duration) * tracks.clientWidth;
      if (x < scroller.scrollLeft + 40 || x > scroller.scrollLeft + scroller.clientWidth - 80) scroller.scrollLeft = x - 120;
    }
    followWords(t);
  }
  function tick() { update(); if (!video.paused) requestAnimationFrame(tick); }
  video.ontimeupdate = update;

  // ---------- right panel ----------
  function tab(name) {
    $$("#panelTabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
    $$(".panel .tab").forEach((el) => (el.hidden = el.id !== `tab-${name}`));
    if (name === "words") followWords(video.currentTime || 0, true);
  }
  $$("#panelTabs button").forEach((b) => (b.onclick = () => tab(b.dataset.tab)));

  // Muse
  const chat = $("#chat");
  const scrollChat = () => (chat.scrollTop = chat.scrollHeight);
  const museMsg = (m) => {
    const el = h("div", { class: `msg muse${m.scripted ? " scripted" : ""}` },
      h("div", { class: "who" }, "Muse", m.scripted ? h("span", { class: "tag" }, "SCRIPTED EXAMPLE") : null), h("div", {}, m.text));
    if (m.changes) el.append(h("div", { class: "changes" }, m.changes.map(([a, b]) => h("div", {}, h("b", {}, a), h("span", {}, b)))));
    if (m.verify) el.append(m.scripted ? h("div", { class: "note" }, m.verify) : h("div", { class: `verify${m.model ? " model" : ""}` }, m.verify));
    if (m.steps) el.append(h("ol", { class: "thinking" }, m.steps.map((s) => h("li", {}, s))));
    const acts = h("div", { class: "acts" });
    if (m.seek != null) acts.append(h("button", { onclick: () => seek(m.seek) }, `Show ${fmt(m.seek)}`));
    if (m.proposal) {
      const ok = h("button", { class: "ok" }, "Accept");
      const no = h("button", {}, "Reject");
      ok.onclick = () => acts.replaceChildren(h("span", { class: "applied" }, "Accepted (static demo: nothing changes)"));
      no.onclick = () => acts.replaceChildren(h("span", { class: "applied", style: "color:var(--dim)" }, "Rejected · kept as is"));
      acts.append(ok, no);
    } else if (m.changes && !m.scripted) acts.append(h("span", { class: "applied" }, "✓ Applied"));
    if (acts.children.length) el.append(acts);
    return el;
  };
  const addMsg = (m) => { chat.append(m.who === "you" ? h("div", { class: `msg you${m.scripted ? " scripted" : ""}` }, m.text) : museMsg(m)); scrollChat(); };
  // The opening conversation is hand-written demo content (from the --extra file), not Muse output: say so.
  if (P.chat?.length) chat.append(h("div", { class: "divider" }, "Scripted example · written by hand during development, not produced by Muse"));
  (P.chat || []).forEach((m) => addMsg({ ...m, scripted: true }));
  if (P.chat?.length) chat.append(h("div", { class: "divider" }, LIVE ? "Live from here · Muse runs on Spark" : "Static demo · replies below are scripted too"));

  function applyResult(j) {
    ({ project: P, edits: EDITS, engine: ENGINE } = j);
    renderTimeline(); renderFindings(); renderSpend(); renderButton();
    window.GCDress?.refresh?.(); // DRESS
  }

  function proposalMsg(r) {
    const el = h("div", { class: "msg muse" }, h("div", { class: "who" }, "Muse"), h("div", {}, r.reply));
    if (r.answers?.length) el.append(h("div", { class: "answers" }, r.answers.map((a) => h("button", { onclick: () => seek(a.t) }, `${fmt(a.t)} · ${a.label}`))));
    const picks = [];
    if (r.ops?.length) {
      el.append(h("div", { class: "ops" }, r.ops.map((op) => {
        const cb = h("input", { type: "checkbox", checked: !op.blocked || null, disabled: op.blocked || null });
        picks.push(cb);
        const shot = /^(swap|add)_broll$/.test(op.op) && LIB()[op.src] ? h("img", { class: "opframe", src: frameUrl(op.src), alt: "", loading: "lazy" }) : null;
        const at = op.t >= 0 ? op.t : (P.broll.concat(P.cards).find((x) => x.id === op.target)?.t ?? null);
        return h("label", { class: `op${op.blocked ? " blocked" : ""}` }, cb,
          h("div", {},
            h("b", {}, op.label), at != null ? h("a", { class: "go", onclick: (e) => { e.preventDefault(); seek(at); } }, fmt(at)) : null,
            h("div", { class: "why" }, op.why), shot,
            h("ul", { class: "opchecks" }, op.checks.map((c) => h("li", { class: c.ok ? "ok" : c.hard ? "bad" : "warn" }, c.text)))));
      })));
    }
    el.append(h("div", { class: "meta" }, `◆ ${r.model} · ${r.cached ? "cached · $0.00" : `$${r.usd.toFixed(3)} · ${Math.round(r.seconds)} s`} · checks are measured`));
    const acts = h("div", { class: "acts" });
    if (r.ops?.some((o) => !o.blocked)) {
      const ok = busy(h("button", { class: "ok" }, "Accept"), "accept");
      const no = h("button", {}, "Reject");
      ok.onclick = async () => {
        const pick = picks.map((c, i) => (c.checked ? i : -1)).filter((i) => i >= 0);
        ok.disabled = true;
        try {
          const j = await api("/api/accept", { id: r.id, pick });
          applyResult(j);
          const edit = j.edits[j.edits.length - 1];
          picks.forEach((c) => (c.disabled = true));
          const undo = busy(h("button", {}, "Undo"), "undo");
          undo.onclick = async () => {
            try {
              applyResult(await api("/api/undo", { edit: edit.id }));
              acts.replaceChildren(h("span", { class: "applied", style: "color:var(--dim)" }, "Undone"));
            } catch (e) { acts.append(h("span", { class: "err" }, e.message)); }
          };
          acts.replaceChildren(h("span", { class: "applied" }, `✓ Applied ${pick.length} · waiting to render`), undo);
        } catch (e) { ok.disabled = RS.state === "running"; acts.append(h("span", { class: "err" }, e.message)); }
      };
      no.onclick = () => { picks.forEach((c) => (c.disabled = true)); acts.replaceChildren(h("span", { class: "applied", style: "color:var(--dim)" }, "Rejected · kept as is")); };
      acts.append(ok, no);
    } else if (r.ops?.length) acts.append(h("span", { class: "applied", style: "color:var(--red)" }, "Every change was blocked by a check"));
    if (acts.children.length) el.append(acts);
    return el;
  }

  async function askLive(text) {
    addMsg({ who: "you", text });
    const started = Date.now();
    const status = h("span", {}, "Spark is reading the cut…");
    const think = h("div", { class: "msg muse" }, h("div", { class: "who" }, "Muse"), h("div", { class: "thinking" }, status));
    chat.append(think); scrollChat();
    const timer = setInterval(() => (status.textContent = `Spark is reading the cut, transcript and shot library · ${Math.round((Date.now() - started) / 1000)} s`), 1000);
    try {
      const r = await api("/api/muse", { text, at: video.currentTime || 0 });
      think.replaceWith(proposalMsg(r));
      ENGINE.museSpent = (ENGINE.museSpent || 0) + (r.usd || 0);
      ENGINE.museCalls = (ENGINE.museCalls || 0) + (r.cached ? 0 : 1);
      renderSpend();
    } catch (e) {
      think.replaceWith(museMsg({ text: `That didn't go through: ${e.message}` }));
    } finally { clearInterval(timer); scrollChat(); }
  }

  // Static demo only: canned replies so the page still shows the flow without the engine.
  const REPLIES = [
    [/export|final cut|resolve|premiere|deliver/i, () => { setTimeout(() => show("deliver"), 700); return { text: "Opening Deliver." }; }],
  ];
  const askStatic = (text) => {
    addMsg({ who: "you", text });
    const hit = REPLIES.find(([re]) => re.test(text));
    addMsg(hit ? { who: "muse", ...hit[1]() } : { who: "muse", text: "This page is the static demo. Run app/engine.py (or app/main.py) and Muse answers through Spark." });
  };
  const reply = (text) => (LIVE ? askLive(text) : askStatic(text));
  (P.suggestions || []).forEach((s) => $("#suggest").append(h("button", { onclick: () => reply(s) }, s)));
  $("#askForm").onsubmit = (e) => {
    e.preventDefault();
    const v = $("#askInput").value.trim();
    if (v) { $("#askInput").value = ""; reply(v); }
  };
  $("#askInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#askForm").requestSubmit(); } });
  $("#undoLast").onclick = async () => {
    if (!LIVE || !EDITS.length) return;
    try { applyResult(await api("/api/undo", {})); } catch (e) { $("#pendingN").textContent = e.message; }
  };

  // ---------- voice notes (cut-job mode): hold to talk, drop a memo, accept, re-render ----------
  const BEAT_OPS = new Set(["swap_take", "drop_beat", "restore_beat", "set_angle", "clear_angle"]);
  let recorder = null, recChunks = [], recAt = 0, recTimer = 0;
  const recMime = () => (typeof MediaRecorder !== "undefined" &&
    MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "");
  async function startTalk() {
    if (!LIVE || !isCutJob() || micBtn.disabled || recorder) return;
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) { micBanner(); return; }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) { micBanner(); return; }
    recAt = video.currentTime || 0;
    recChunks = [];
    const mime = recMime();
    recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    recorder.ondataavailable = (e) => { if (e.data.size) recChunks.push(e.data); };
    recorder.start();
    recorder._stream = stream;
    micBtn.classList.add("rec");
    const t0 = Date.now();
    micBtn.textContent = "● 0:00";
    recTimer = setInterval(() => {
      const s = Math.floor((Date.now() - t0) / 1000);
      micBtn.textContent = `● ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    }, 500);
  }
  async function stopTalk() {
    if (!recorder) return;
    const done = recorder, mime = recorder.mimeType || "";
    recorder = null;
    clearInterval(recTimer);
    micBtn.classList.remove("rec");
    micBtn.textContent = "🎙";
    const blob = await new Promise((resolve) => {
      done.onstop = () => resolve(new Blob(recChunks, { type: mime }));
      done.stop();
    });
    (done._stream.getTracks() || []).forEach((t) => t.stop());
    await uploadVoice(blob, mime, recAt, null);
  }
  micBtn.addEventListener("pointerdown", (e) => { e.preventDefault(); startTalk(); });
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => micBtn.addEventListener(ev, stopTalk));
  document.addEventListener("keydown", (e) => {
    if (e.code !== "KeyV" || e.repeat) return;
    if (!$("#renderDlg").hidden || !$("#confirmDlg").hidden || !$("#connDlg").hidden) return;
    if (/TEXTAREA|INPUT|SELECT/.test(document.activeElement.tagName)) return;
    startTalk();
  });
  document.addEventListener("keyup", (e) => { if (e.code === "KeyV") stopTalk(); });

  function voicePending(head) {
    const el = h("div", { class: "msg muse voice" }, h("div", { class: "who" }, head),
      h("div", { class: "thinking" }, "Transcribing…"));
    $("#voiceList").append(el);
    el.scrollIntoView({ block: "nearest" });
    return el;
  }
  async function uploadVoice(blob, mime, at, name) {
    const buf = await blob.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode(...bytes.subarray(i, i + 8192));
    const card = voicePending(name ? `Voice memo · ${name}` : `Voice note · ${fmt(at)}`);
    try {
      const first = name
        ? await api("/api/voice/file", { name, audio_b64: btoa(bin) })
        : await api("/api/voice/record", { audio_b64: btoa(bin), mime, at_s: at });
      if (first.needs_confirm) {
        const ok = await confirmVoice(first.duration_s, first.estimate_usd);
        if (!ok) { card.remove(); return; }
        card.querySelector(".thinking").textContent = "Transcribing…";
        fillVoice(card, await api("/api/voice/file", { name, audio_b64: btoa(bin), confirm: true }), name);
      } else fillVoice(card, first, name);
    } catch (e) {
      card.replaceChildren(h("div", { class: "who" }, name ? `Voice memo · ${name}` : "Voice note"),
        h("div", {}, `That didn't go through: ${e.message}`));
    }
  }
  function confirmVoice(durationS, estimateUsd) {
    return new Promise((resolve) => {
      $("#confirmMsg").textContent = `This note is ${fmt(durationS)}. Transcribing and resolving it costs about $${(+estimateUsd).toFixed(2)}. Continue?`;
      $("#confirmDlg").hidden = false;
      $("#confirmYes").onclick = () => { $("#confirmDlg").hidden = true; resolve(true); };
      $("#confirmNo").onclick = () => { $("#confirmDlg").hidden = true; resolve(false); };
    });
  }
  $("#confirmDlg").addEventListener("click", (e) => {
    if (e.target.id === "confirmDlg") { $("#confirmDlg").hidden = true; }
  });
  function fillVoice(card, r, name) {
    const notes = r.notes || [];
    let base = 0;
    const cards = notes.map((n) => {
      const c = voiceNoteCard(r, n, base);
      base += (n.ops || []).length;
      return c;
    });
    if (r.error) cards.push(h("div", { class: "msg muse voice" },
      h("div", { class: "who" }, name ? `Voice memo · ${name}` : "Voice note"),
      h("div", {}, r.error)));
    if (name) {
      card.replaceWith(h("div", { class: "memo" }, h("div", { class: "memohead" }, `Voice memo · ${name}`), ...cards));
    } else if (cards.length) {
      card.replaceWith(...cards);
    } else {
      card.replaceChildren(h("div", { class: "who" }, "Voice note"), h("div", {}, "Nothing to resolve."));
    }
    if (cards.length) cards[cards.length - 1].scrollIntoView({ block: "nearest" });
  }
  function voiceNoteCard(r, note, base) {
    const eng = note.engine === "whisper" ? "local Whisper" : "Meta transcription";
    const el = h("div", { class: "msg muse voice" },
      h("div", { class: "who" }, `Voice note · ${fmt(note.at_s || 0)} · ${eng}`),
      h("div", { class: "heard" }, `I heard: ${note.heard || note.raw_text}`));
    if (note.message) el.append(h("div", { class: "note" }, note.message));
    const picks = [];
    if (note.ops?.length) {
      el.append(h("div", { class: "ops" }, note.ops.map((op, i) => {
        const amb = note.status === "ambiguous" && BEAT_OPS.has(op.op);
        const cb = h("input", { type: "checkbox", checked: (!op.blocked || amb) ? true : null, disabled: (op.blocked && !amb) || null });
        picks.push(cb);
        return h("label", { class: `op${op.blocked && !amb ? " blocked" : ""}${amb ? " amb" : ""}`, "data-i": base + i },
          cb,
          h("div", {},
            h("b", {}, op.label),
            note.at_s != null ? h("a", { class: "go", onclick: (e) => { e.preventDefault(); seek(note.at_s); } }, fmt(note.at_s)) : null,
            op.why ? h("div", { class: "why" }, op.why) : null,
            h("ul", { class: "opchecks" }, op.checks.map((c) => h("li", { class: c.ok ? "ok" : c.hard ? "bad" : "warn" }, c.text)))));
      })));
    }
    if (note.status === "ambiguous" && note.candidates?.length) {
      const radios = note.candidates.map((c) => {
        const radio = h("input", { type: "radio", name: `cand-${r.id}-${note.id}`, value: c.beat_id });
        radio.onchange = () => $$(".op.amb input", el).forEach((cb) => { cb.disabled = false; cb.checked = true; });
        return h("label", { class: "cand" }, radio, h("span", {}, `B${c.beat_id} · ${c.label}`),
          h("a", { class: "go", onclick: (e) => { e.preventDefault(); seek(c.at_s); } }, fmt(c.at_s)));
      });
      el.append(h("div", { class: "cands" }, ...radios));
    }
    el.append(h("div", { class: "meta" }, `◆ ${r.model} · $${(r.usd || 0).toFixed(3)} · checks are measured`));
    const acts = h("div", { class: "acts" });
    const someOpen = note.ops?.some((o) => !o.blocked) || note.status === "ambiguous";
    if (someOpen) {
      const ok = h("button", { class: "ok" }, "Accept");
      const all = h("button", {}, "Accept all");
      const no = h("button", {}, "Reject");
      const send = async (which) => {
        const idx = [...el.querySelectorAll(".op")].map((lab) => +lab.dataset.i);
        const cands = {};
        const sel = el.querySelector(".cands input:checked");
        if (sel) cands[note.id] = +sel.value;
        ok.disabled = all.disabled = true;
        try {
          const j = await api("/api/voice/apply", { id: r.id, pick: which === "all" ? idx : picks.map((c, i) => (c.checked ? idx[i] : -1)).filter((i) => i >= 0), candidates: cands });
          applyResult(j);
          loadTaste();
          picks.forEach((c) => (c.disabled = true));
          acts.replaceChildren(h("span", { class: "applied" }, note.ops?.[0]?.op?.startsWith("dress_") ? "✓ Applied · open Dress the cut and Render" : "✓ Applied · re-render to watch it")); // DRESS
        } catch (e) { ok.disabled = all.disabled = false; acts.append(h("span", { class: "err" }, e.message)); }
      };
      ok.onclick = () => send("checked");
      all.onclick = () => send("all");
      no.onclick = () => { picks.forEach((c) => (c.disabled = true)); acts.replaceChildren(h("span", { class: "applied", style: "color:var(--dim)" }, "Rejected · kept as is")); };
      acts.append(ok, all, no);
    } else if (note.ops?.length) {
      acts.append(h("span", { class: "applied", style: "color:var(--red)" }, "Every change was blocked by a check"));
    }
    if (acts.children.length) el.append(acts);
    return el;
  }

  // Drop a memo anywhere onto the Edit view.
  let dragN = 0;
  $("#view-edit").addEventListener("dragover", (e) => {
    if (!LIVE || !isCutJob()) return;
    e.preventDefault();
    $("#dropOverlay").hidden = false;
  });
  $("#view-edit").addEventListener("dragenter", (e) => {
    if (!LIVE || !isCutJob()) return;
    e.preventDefault();
    dragN++;
  });
  $("#view-edit").addEventListener("dragleave", () => {
    if (--dragN <= 0) { dragN = 0; $("#dropOverlay").hidden = true; }
  });
  $("#view-edit").addEventListener("drop", (e) => {
    if (!LIVE || !isCutJob()) return;
    e.preventDefault();
    dragN = 0;
    $("#dropOverlay").hidden = true;
    const f = (e.dataTransfer.files || [])[0];
    if (f) uploadVoice(f, f.type, 0, f.name);
  });

  // Taste: the standing preferences, shown and removable.
  async function loadTaste() {
    if (!LIVE || !isCutJob()) return;
    try {
      renderTaste((await api("/api/taste")).entries || []);
    } catch (e) { /* the panel stays hidden when taste cannot load */ }
  }
  function renderTaste(entries) {
    $("#tastePanel").hidden = false;
    $("#tasteList").replaceChildren(...entries.map((e) => h("li", {},
      h("span", {}, e.text),
      h("button", {
        class: "x", title: "Remove", onclick: async () => {
          try { renderTaste((await api("/api/taste/remove", { id: e.id })).entries || []); }
          catch (err) { $("#tasteList").append(h("li", { class: "err" }, err.message)); }
        },
      }, "×"))));
    if (!entries.length) $("#tasteList").append(h("li", { class: "muted" }, "None yet — say “always…” or “never…” in a note."));
  }
  $("#tasteForm").onsubmit = async (e) => {
    e.preventDefault();
    const v = $("#tasteInput").value.trim();
    if (!v) return;
    $("#tasteInput").value = "";
    try { renderTaste((await api("/api/taste/add", { text: v })).entries || []); }
    catch (err) { $("#tasteList").append(h("li", { class: "err" }, err.message)); }
  };
  loadTaste();
  window.GCDress?.mount({ h, $, api, fmt, LIVE: () => LIVE, project: () => P, isCutJob, seek, applyResult, ask: (t) => reply(t), voiceNoteCard, chat, scrollChat, video }); // DRESS

  // Review
  let filter = "all";
  const SRC = { measured: "Measured", spark: "Spark", you: "You", reshoot: "Reshoot", render: "Render" };
  function renderFindings() {
    const list = $("#findings"); list.replaceChildren();
    if ((P.findings || []).some((f) => !f.edit && !f.render)) // Render's own notes carry render: rNNN
      list.append(h("li", { class: "banner" }, "Findings without a Muse note were written by hand during development (scripted demo content), not generated by the app."));
    (P.findings || []).forEach((f, i) => {
      if (filter !== "all" && f.state !== filter && !(filter === "fixed" && f.state === "ok")) return;
      list.append(h("li", { "data-i": i, onclick: () => pickFinding(i) },
        h("div", { class: "fhead" }, h("span", { class: `b ${f.src}` }, SRC[f.src]), h("span", { class: `state ${f.state}` }, f.state), h("span", { class: "t" }, fmt(f.t))),
        h("div", { class: "ftitle" }, f.title), h("div", { class: "fbody" }, f.body), h("div", { class: "fcheck" }, f.check)));
    });
    const open = (P.findings || []).filter((f) => f.state === "open").length;
    $("#openCount").textContent = open || "";
  }
  function pickFinding(i) {
    $$("#findings li").forEach((li) => li.classList.toggle("sel", +li.dataset.i === i));
    seek(P.findings[i].t);
  }
  $$("#filters button").forEach((b) => (b.onclick = () => {
    filter = b.dataset.f;
    $$("#filters button").forEach((x) => x.classList.toggle("on", x === b));
    renderFindings();
  }));

  // Words: the Whisper transcript of the finished cut, following the playhead.
  const T = P.transcript || [];
  function renderWords(q = "") {
    const box = $("#words"); box.replaceChildren();
    q = q.trim().toLowerCase();
    let hits = 0;
    T.forEach((s, i) => {
      if (q && !s.text.toLowerCase().includes(q)) return;
      hits++;
      box.append(h("div", { class: "seg", "data-i": i },
        h("button", { class: "st", onclick: () => seek(s.t) }, fmt(s.t)),
        h("p", {}, s.w.map((w) => h("span", { class: `w${q && w[0].toLowerCase().includes(q) ? " hit" : ""}`, "data-s": w[1], onclick: () => seek(w[1] + 0.01) }, `${w[0]} `)))));
    });
    $("#wordsCount").textContent = q ? `${hits} of ${T.length} lines` : `${T.length} lines · ${T.reduce((a, s) => a + s.w.length, 0)} words · Whisper on the finished cut`;
    segI = -1;
  }
  function followWords(t, force) {
    if ($("#tab-words").hidden) return;
    let i = T.findIndex((s) => t >= s.t && t < s.e + 0.3);
    if (i !== segI || force) {
      $$("#words .seg.on").forEach((x) => x.classList.remove("on"));
      const row = $(`#words .seg[data-i="${i}"]`);
      row?.classList.add("on");
      if (row && (!video.paused || force)) row.scrollIntoView({ block: "nearest" });
      segI = i;
    }
    if (i < 0) return;
    const w = T[i].w.findIndex((x) => t >= x[1] && t < x[2]);
    const el = w >= 0 ? $(`#words .seg[data-i="${i}"] .w:nth-child(${w + 1})`) : null;
    if (el !== wordEl) { wordEl?.classList.remove("now"); el?.classList.add("now"); wordEl = el; }
  }
  $("#wordsSearch").oninput = (e) => renderWords(e.target.value);

  // Why
  function renderWhy(c) {
    const why = $("#why"); why.replaceChildren();
    const kv = (rows) => h("div", { class: "kv" }, rows.filter(Boolean).flatMap(([k, v]) => [h("b", {}, k), h("span", {}, v)]));
    const lib = LIB();
    const newShot = c.type === "broll" && (c.pending === "add" || c.pending === "change") && LIVE;
    why.append(h("img", { class: "frame", src: newShot ? frameUrl(c.tag) : thumb(c.t + Math.min(c.d / 2, 3)), alt: "" }));
    if (c.pending) why.append(h("div", { class: `pendnote p-${c.pending}` }, `${PENDING[c.pending]} · waiting to render`, c.why ? ` · ${c.why}` : ""));
    if (c.type === "talk") {
      why.append(h("h3", {}, c.topic), kv([["Section", c.section], ["Source", c.src], ["Source in", `${c.in.toFixed(2)} s`], ["Length", `${c.d.toFixed(2)} s`], ["Timeline", fmt(c.t)]]));
      const rows = (P.takes || []).filter((t) => t.topic === c.topic);
      if (rows.length) {
        const pick = rows.find((t) => t.kind === "keeper_take") || rows[0];
        if (isCutJob()) {
          why.append(h("div", { class: "reason" },
            h("small", {}, `Director · keeper take · confidence ${pick.conf ?? "–"}`),
            `Director kept A${pick.aid ?? "?"} — “${pick.reason}”`));
        } else {
          why.append(h("div", { class: "reason" }, h("small", {}, `Spark · ${pick.kind.replace(/_/g, " ")} · confidence ${pick.conf ?? "–"}`), pick.reason));
        }
        why.append(h("h4", {}, `Takes of this moment (${rows.length})`));
        rows.forEach((t) => {
          const row = h("div", { class: `alt${t.kind === "keeper_take" ? " pick" : ""}` }, `${t.clip} @ ${fmt(t.in)} · ${t.kind.replace(/_/g, " ")} — ${t.reason}`);
          if (isCutJob() && LIVE && !t.pick && t.kind === "alternate" && t.aid != null) {
            const use = h("button", { class: "link use-take" }, "Use this take");
            const bid = parseInt(String(t.group || "").replace(/^B/, ""), 10);
            if (!Number.isInteger(bid)) use.disabled = true;
            use.onclick = async () => {
              use.disabled = true;
              try {
                applyResult(await api("/api/voice/apply", { ops: [{ op: "swap_take", beat_id: bid, attempt_id: t.aid, span_id: "", camera: "", pause_limit_s: -1, target_s: -1, text: "", why: "Use this take" }] }));
                use.replaceWith(h("span", { class: "applied" }, "✓ Kept · re-render to watch it"));
              } catch (e) { use.disabled = false; row.append(h("span", { class: "err" }, e.message)); }
            };
            row.append(" ", use);
          }
          why.append(row);
        });
      } else {
        why.append(h("div", { class: "reason" }, h("small", {}, "Measured"), "Cut points set from the 20 ms loudness envelope, in silences; Whisper on the render confirms no clipped words."));
      }
      const said = T.filter((s) => s.e > c.t && s.t < c.t + c.d).map((s) => s.text).join(" ");
      if (said) why.append(h("h4", {}, "What she says"), h("p", { class: "said" }, `“${said.slice(0, 420)}${said.length > 420 ? "…" : ""}”`));
    } else if (c.type === "broll") {
      const e = lib[c.tag] || {};
      why.append(h("h3", {}, c.tag), kv([["Shot", e.what || "—"], c.was_tag && ["Was", c.was_tag], ["Section", c.section], ["Timeline", `${fmt(c.t)} · ${c.d.toFixed(1)} s`], e.q != null && ["Quality", `${e.q}`]]));
      const manual = c.reason === MANUAL;
      const byMuse = c.pending === "add" || c.was_tag; // swaps and additions carry Muse's reason; moves keep the original one
      why.append(h("div", { class: "reason" }, h("small", {}, byMuse ? "Muse · your accepted change" : manual ? "Manual insert" : "Spark · B-roll plan"),
        manual ? "Placed by hand (an --insert during development), not by Spark." : c.reason || "—"));
      const words = T.filter((s) => s.e > c.t && s.t < c.t + c.d).map((s) => s.text).join(" ");
      if (words) why.append(h("h4", {}, "Under the words"), h("p", { class: "said" }, `“${words.slice(0, 300)}”`));
    } else if (c.type === "card") {
      why.append(h("h3", {}, c.lines.join(" · ")), kv([["Caption", c.caption || "—"], c.was_lines && ["Was", c.was_lines.join(" · ")], ["On screen", `${fmt(c.t)} · ${c.d.toFixed(1)} s`]]));
      const said = T.filter((s) => s.e > c.t - 6 && s.t < c.t + 3).map((s) => s.text).join(" ");
      why.append(h("div", { class: "reason" }, h("small", {}, "How it was made"), "Card text and timing were written by hand from the transcript. Cards that Muse adds or edits are checked against the transcript (numbers must be said within ±20 s)."));
      if (said) why.append(h("h4", {}, "Said on camera"), h("p", { class: "said" }, `“${said.slice(0, 300)}”`));
    }
  }

  // ---------- render: plan → start → progress → promote (app/render.py does the work; the engine follows it) ----------
  const bar = $("#renderBar"), dlg = $("#renderDlg");
  const FIX = {
    card16_text: "Card text repaired", voice_sync: "Voice re-synced to the picture", frame_exact_joins: "Frame-exact section joins",
    phone_fps: "Phone copy timing (no extra picture delay)", music_loop: "Music bed no longer loops into its own start",
  };
  const ENDED = {
    refused: "Render refused", failed: "Render failed", failed_verification: "Checks failed · nothing was promoted",
    interrupted: "Render interrupted", cancelled: "Render cancelled", idle: "Render stopped",
  };
  const about = (s) => (s >= 90 ? `About ${Math.round(s / 60)} min` : `About ${Math.max(1, Math.round(s))} s`);
  const say = (x) => (typeof x === "string" ? x : x?.label || x?.title || x?.text || x?.message || x?.reason || JSON.stringify(x));
  let mode = "", pollTimer = 0, polling = false, watchV = null, since = 0;

  let cutjobRunning = false, cutjobTimer = 0;
  function renderButton() {
    const btn = $("#renderBtn");
    if (isCutJob()) {
      btn.textContent = "Re-render cut";
      const dirty = !!(P.cutJob && P.cutJob.renderDirty);
      const why = !LIVE ? "Re-render needs the engine: run app/main.py --job <dir>"
        : cutjobRunning ? "Re-rendering…"
        : !dirty ? "Nothing waiting to re-render"
        : "";
      btn.disabled = !!why;
      btn.title = why || "Re-render the cut with the accepted voice edits";
      renderVersion();
      return;
    }
    const n = P.pending || 0;
    const what = [n && `${n} change${n === 1 ? "" : "s"}`, !P.version && "fixes"].filter(Boolean);
    btn.textContent = what.length ? `Render · ${what.join(" + ")}` : "Render";
    const why = !LIVE ? "Render needs the engine: run app/main.py or app/engine.py"
      : RS.state === "running" ? `Rendering ${RS.version || ""}…`
      : RS.state === "unavailable" ? RS.message || "Render isn't available"
      : !what.length ? "Nothing waiting to render" : "";
    btn.disabled = !!why;
    btn.title = why || "See what the next version changes, then start it";
    renderVersion();
  }
  function renderVersion() {
    const chip = $("#verChip");
    chip.hidden = !P.version;
    if (!P.version) return;
    const rv = LIVE ? busy(h("button", { class: "link", title: "Go back to the previous version" }, "Revert"), "revert") : null;
    if (rv) rv.onclick = askRevert;
    chip.replaceChildren(...[h("b", {}, P.version), rv].filter(Boolean));
  }
  // While a render runs, Accept / Undo / Revert wait: the render took a snapshot of the edit list.
  function lockEdits() {
    const on = RS.state === "running";
    $$("[data-busy]").forEach((b) => { b.disabled = on; b.title = on ? BUSY[b.dataset.busy] : ""; });
    renderButton();
  }
  const setRS = (st) => { RS = { state: st.state, version: st.version, message: st.message }; lockEdits(); };
  const barActs = (...kids) => $("#renderActs").replaceChildren(...kids.filter(Boolean));
  const barText = (s) => { $("#renderText").textContent = s; $("#renderText").title = s; };
  const closeBar = () => { bar.hidden = true; mode = ""; };

  function openDlg(title, start = false) {
    $("#renderDlgTitle").textContent = title;
    $("#renderDlgBody").replaceChildren(h("p", { class: "muted" }, start ? "Planning the next version…" : "Loading…"));
    $("#renderStart").hidden = !start;
    $("#renderStart").disabled = true;
    dlg.hidden = false;
  }
  $("#renderDlgClose").onclick = () => (dlg.hidden = true);
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.hidden = true; });

  async function openPlan() {
    openDlg("Render", true);
    const body = $("#renderDlgBody"), startBtn = $("#renderStart");
    let plan;
    try { plan = await api("/api/render/plan"); } catch (e) { body.replaceChildren(h("p", { class: "err" }, e.message)); return; }
    const changes = plan.changes || [], fixes = plan.fixes || [], refusals = plan.refusals || [], notes = plan.notes || [];
    $("#renderDlgTitle").textContent = `Render ${plan.next_version || ""}`.trim();
    const groups = {};
    changes.forEach((c) => {
      const g = /_card$/.test(c.op) ? "Cards" : c.op === "add_note" ? "Review notes"
        : P.sections[c.section]?.name || (typeof c.section === "string" && c.section) || "Program";
      (groups[g] ||= []).push(c);
    });
    const kids = [h("h4", {}, "Will render"), changes.length
      ? Object.entries(groups).map(([g, cs]) => h("div", { class: "grp" }, h("b", {}, g), h("ul", {}, cs.map((c) => h("li", {}, say(c.label || c.op))))))
      : h("p", { class: "muted" }, "No accepted changes.")];
    if (fixes.length) kids.push(h("h4", {}, "Fixes"), h("ul", {}, fixes.map((f) => h("li", { class: "fix" }, typeof f === "string" ? FIX[f] || f.replace(/_/g, " ") : say(f)))));
    if (refusals.length) kids.push(h("h4", { class: "bad" }, "Can't render"), h("ul", {}, refusals.map((r) => {
      if (!r.edit) return h("li", { class: "refused" }, h("div", {}, h("b", {}, say(r.label || r.op || "plan")), h("small", {}, say(r.reason || "")))); // not from an edit: nothing to undo
      const undo = busy(h("button", {}, "Undo"), "undo");
      undo.onclick = async () => {
        undo.disabled = true;
        try { applyResult(await api("/api/undo", { edit: r.edit })); openPlan(); } catch (e) { undo.disabled = false; undo.after(h("span", { class: "err" }, e.message)); }
      };
      return h("li", { class: "refused" }, h("div", {}, h("b", {}, say(r.label || r.op)), h("small", {}, say(r.reason || ""))), undo);
    })));
    if (notes.length) kids.push(h("h4", {}, "Heads-up"), h("ul", {}, notes.map((n) => h("li", { class: "note" }, say(n)))));
    const dirty = plan.dirty_names || (plan.dirty || []).map((i) => P.sections[i]?.name || i);
    kids.push(h("p", { class: "est" }, [plan.estimate_s > 0 ? about(plan.estimate_s) : "",
      dirty.length ? `re-renders the ${dirty.join(", ")} picture${dirty.length > 1 ? "s" : ""}, then cards, mix and the phone copy` : "cards, mix and the phone copy"].filter(Boolean).join(" · ")),
    h("p", { class: "keep" }, "The current cut stays until the new one passes its checks."));
    if (refusals.length) kids.push(h("p", { class: "err" }, "Start stays off until every change can render: undo the ones above, or ask Muse for a note instead."));
    body.replaceChildren(...kids.flat());
    startBtn.disabled = !!refusals.length || (!changes.length && !fixes.length) || RS.state === "running";
    startBtn.onclick = async () => {
      startBtn.disabled = true;
      try {
        const r = await api("/api/render", {});
        dlg.hidden = true;
        follow(r.version);
      } catch (e) { startBtn.disabled = false; body.append(h("p", { class: "err" }, e.message)); }
    };
  }

  function follow(version) {
    watchV = version; since = Date.now(); mode = "";
    setRS({ state: "running", version });
    showProgress({ version, step: "Starting", pct: 0 });
    clearInterval(pollTimer);
    pollTimer = setInterval(poll, 1000);
    poll();
  }
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const st = await api("/api/render");
      if (st.state === "running") {
        watchV = st.version;
        if (RS.state !== "running" || RS.version !== st.version) setRS(st);
        showProgress(st);
        return;
      }
      if (st.version !== watchV && Date.now() - since < 30000) return; // the child hasn't reported yet
      clearInterval(pollTimer); pollTimer = 0;
      const mine = st.version === watchV ? st : { state: "failed", version: watchV, message: "the render process never reported back", log: st.log };
      if (mine.state !== "promoted") setRS(mine); // promoted: edits stay locked until the page reloads on the new cut
      ended(mine);
    } catch (e) {
      barText(`Can't read the render's progress · ${e.message}`);
    } finally { polling = false; }
  }
  function showProgress(st) {
    // KITCHEN: legacy render progress also feeds the kitchen when present.
    try { if (window.Kitchen) Kitchen.update(st); } catch { /* kitchen is optional */ }
    bar.hidden = false;
    bar.className = "renderbar";
    if (mode !== "running") {
      mode = "running";
      const c = h("button", {}, "Cancel");
      c.onclick = async () => {
        c.disabled = true; c.textContent = "Cancelling…";
        try { await api("/api/render/cancel", {}); } catch (e) { c.disabled = false; c.textContent = "Cancel"; barText(e.message); }
      };
      barActs(c);
    }
    const p = Math.max(0, Math.min(100, +st.pct || 0));
    $("#renderFill").style.width = `${p}%`;
    barText([st.version, st.step || "Starting", st.n ? `step ${st.i || 0} of ${st.n}` : "", `${Math.round(p)}%`].filter(Boolean).join(" · "));
    $("#renderEta").textContent = st.eta_s > 0 ? `${fmt(st.eta_s)} left` : "";
  }
  function ended(st) {
    mode = st.state;
    bar.hidden = false;
    $("#renderEta").textContent = "";
    const checks = Array.isArray(st.checks) ? st.checks : [];
    if (st.state === "promoted") {
      const n = checks.length ? checks.filter((c) => c.status !== "fail").length : +st.checks || 0;
      const head = /ready/.test(st.message || "") ? st.message : `${st.version} ready`; // render says "rNNN ready · N checks passed"
      bar.className = "renderbar ok";
      $("#renderFill").style.width = "100%";
      barText([head, n && !/checks passed/.test(head) ? `${n} checks passed` : "", "loading the new cut…"].filter(Boolean).join(" · "));
      barActs();
      setTimeout(() => location.reload(), 1500);
      return;
    }
    const failing = checks.filter((c) => c.status === "fail").map((c) => (c.value != null ? `${c.name} (${c.value})` : c.name));
    bar.className = `renderbar ${st.state === "cancelled" ? "off" : "bad"}`;
    $("#renderFill").style.width = "0";
    barText([ENDED[st.state] || st.state, st.message, failing.length ? `failing: ${failing.join(", ")}` : "",
      /current cut/.test(st.message || "") ? "" : "the current cut is unchanged"].filter(Boolean).join(" · "));
    barActs(st.log ? h("button", { class: "link", onclick: showLog }, "render.log") : null, h("button", { title: "Dismiss", onclick: closeBar }, "×"));
  }
  async function showLog() {
    openDlg("render.log");
    const body = $("#renderDlgBody");
    try {
      const r = await fetch("/api/render/log");
      const t = await r.text();
      if (!r.ok) throw new Error((() => { try { return JSON.parse(t).error; } catch { return `HTTP ${r.status}`; } })());
      const pre = h("pre", { class: "log" }, t || "(empty)");
      body.replaceChildren(pre);
      pre.scrollTop = pre.scrollHeight;
    } catch (e) { body.replaceChildren(h("p", { class: "err" }, e.message)); }
  }
  function askRevert() {
    if (RS.state === "running") return;
    mode = "ask";
    bar.hidden = false;
    bar.className = "renderbar ask";
    $("#renderFill").style.width = "0";
    $("#renderEta").textContent = "";
    barText(`Go back to the version before ${P.version}? ${P.version} stays on the drive.`);
    const yes = h("button", { class: "danger" }, "Revert");
    yes.onclick = async () => {
      yes.disabled = true;
      try {
        await api("/api/render/revert", {});
        barText("Reverted · loading the previous cut…");
        barActs();
        setTimeout(() => location.reload(), 300);
      } catch (e) { yes.disabled = false; barText(`Can't revert · ${e.message}`); }
    };
    barActs(yes, h("button", { onclick: closeBar }, "Keep"));
  }
  $("#renderBtn").onclick = () => {
    if ($("#renderBtn").disabled) return;
    if (isCutJob()) startCutjobRerender();
    else openPlan();
  };
  async function startCutjobRerender() {
    try {
      await api("/api/cutjob/rerender", {});
    } catch (e) {
      bar.hidden = false;
      bar.className = "renderbar bad";
      $("#renderFill").style.width = "0";
      barText(`Couldn't start the re-render · ${e.message}`);
      barActs(h("button", { title: "Dismiss", onclick: closeBar }, "×"));
      return;
    }
    // KITCHEN: start the ghost kitchen over the player, poster behind it.
    try { if (window.Kitchen) Kitchen.start({ mode: "cutjob", thumbs: Math.floor(P.duration / 2) }); } catch { /* kitchen is optional */ }
    try { video.pause(); } catch { /* ignore */ }
    video.poster = `demo/poster.jpg${VQ}`;
    cutjobRunning = true;
    lockEdits();
    renderButton();
    bar.hidden = false;
    bar.className = "renderbar";
    $("#renderFill").style.width = "50%";
    barText("Re-rendering cut.mp4…");
    barActs();
    clearInterval(cutjobTimer);
    cutjobTimer = setInterval(pollCutjob, 1000);
    pollCutjob();
  }
  async function pollCutjob() {
    try {
      const st = await api("/api/cutjob/rerender");
      if (!cutjobTimer) return; // KITCHEN: a slow poll that lands after another one finished must not hand off twice
      // KITCHEN: every status feeds the scene, before the running return; real shot progress drives the bar too.
      try { if (window.Kitchen) Kitchen.update(st); } catch { /* kitchen is optional */ }
      if (st.state === "running" && st.pct != null) {
        $("#renderFill").style.width = `${Math.max(2, Math.min(99, +st.pct))}%`;
        barText(["Re-rendering cut.mp4", st.total ? `shot ${Math.min(st.total, (st.done || 0) + 1)} of ${st.total}` : "", `${Math.round(st.pct)}%`,
          st.eta_s > 0 ? `${fmt(st.eta_s)} left` : ""].filter(Boolean).join(" · "));
      }
      if (st.state === "running") return;
      clearInterval(cutjobTimer);
      cutjobTimer = 0;
      cutjobRunning = false;
      if (st.state === "done") {
        bar.className = "renderbar ok";
        $("#renderFill").style.width = "100%";
        // KITCHEN: no reload; swap the fresh project in place, then let the kitchen plate the new cut.
        try {
          barText(`${st.message} · plating the new cut…`);
          const j = await api("/api/project");
          ({ project: P, edits: EDITS, engine: ENGINE } = j);
          applyResult(j);
          // KITCHEN: preload="metadata" may stop short of canplay (Safari), so the first decoded frame counts too.
          const ready = new Promise((res) => { video.addEventListener("canplay", res, { once: true }); video.addEventListener("loadeddata", res, { once: true }); setTimeout(res, 4000); });
          video.poster = `demo/poster.jpg?v=${Date.now()}`;
          video.src = `demo/video.mp4?v=${Date.now()}`;
          $("#tcDur").textContent = fmt(P.duration);
          T.splice(0, T.length, ...(P.transcript || [])); // KITCHEN: the Words list reads T, captured at page load
          renderWords(); lockEdits();
          layout();
          await ready;
          if (window.Kitchen) await Kitchen.finish();
          try { video.currentTime = 0; } catch { /* first frame stays */ }
          barText(`${st.message} · the new cut is in the player`);
          barActs(h("button", { title: "Dismiss", onclick: closeBar }, "×"));
        } catch {
          barText(`${st.message} · loading the new cut…`);
          setTimeout(() => location.reload(), 1500);
        }
      } else {
        try { if (window.Kitchen) Kitchen.fail(st.message || "Re-render failed"); } catch { /* KITCHEN: optional */ }
        bar.className = "renderbar bad";
        $("#renderFill").style.width = "0";
        barText(st.message || "Re-render failed");
        barActs(h("button", { title: "Dismiss", onclick: closeBar }, "×"));
        renderButton();
      }
    } catch (e) { barText(`Can't read the re-render's progress · ${e.message}`); }
  }

  // ---------- pages ----------
  const stat = (n, label) => h("div", { class: "stat" }, h("b", {}, n), h("span", {}, label));
  const PAGES = {
    story(el) {
      const TK = P.takes || [];
      const count = (k) => TK.filter((t) => t.kind === k).length;
      el.append(h("h2", {}, "Story"),
        h("p", { class: "lede" }, "Spark watched the house walkthrough with numbered transcript lines beside it and sorted every moment. Whisper keeps the clock. (The other sections' takes were picked by hand, and reclassifying here is not saved yet.)"),
        h("div", { class: "stats" }, stat(TK.length, "moments"), stat(count("keeper_take"), "keepers"), stat(count("alternate_take"), "alternates"),
          stat(count("flub_or_false_start"), "flubs"), stat(count("aside_to_editor"), "asides to the editor"), stat(count("setup_or_technical") + count("off_camera_or_pause"), "setup & pauses"),
          P.takeMeta?.model && stat(`$${(+P.takeMeta.usd).toFixed(2)}`, `${P.takeMeta.model} · ${Math.round(P.takeMeta.seconds)} s`)));
      const groups = [];
      TK.forEach((t) => { let g = groups.find((x) => x.topic === t.topic); if (!g) groups.push((g = { topic: t.topic, rows: [] })); g.rows.push(t); });
      const KINDS = ["keeper_take", "alternate_take", "flub_or_false_start", "aside_to_editor", "setup_or_technical", "off_camera_or_pause"];
      groups.forEach((g) => {
        const keep = g.rows.filter((r) => r.kind === "keeper_take").length;
        el.append(h("div", { class: "group" },
          h("header", {}, h("b", {}, g.topic), h("span", {}, `${g.rows.length} moment${g.rows.length > 1 ? "s" : ""} · ${keep} kept`)),
          g.rows.map((t) => {
            const row = h("div", { class: `take${t.kind === "keeper_take" ? "" : " drop"}` },
              h("span", { class: "star" }, t.kind === "keeper_take" ? "★" : ""),
              h("select", { onchange: (e) => row.classList.toggle("drop", e.target.value !== "keeper_take") },
                KINDS.map((k) => h("option", { value: k, selected: k === t.kind || null }, k.replace(/_/g, " ")))),
              h("span", { class: "mono" }, `${t.clip} ${fmt(t.in)}`), h("span", { class: "mono" }, `${t.d.toFixed(1)} s`),
              h("span", {}, t.reason), h("span", { class: "said" }, `“${t.text.slice(0, 150)}${t.text.length > 150 ? "…" : ""}”`));
            return row;
          })));
      });
    },
    import(el) { window.GCImport.mount(el, window.GC); }, // ONBOARD: the view lives in import.js
    deliver(el) {
      const L = { ...P.mix, ...P.loudness }, f = (v) => (v == null || v === "" ? NaN : +v);
      const lufs = f(L.lufs), tp = f(L.tp), gain = f(L.music_gain_db);
      const num = (v) => (Number.isFinite(v) ? v.toFixed(1).replace("-", "−") : "–");
      const pos = (v) => `${((Math.max(-24, Math.min(0, Number.isFinite(v) ? v : -24)) + 24) / 24) * 100}%`;
      el.append(h("h2", {}, "Deliver"),
        h("p", { class: "lede" }, `Finish here, or hand the cut to your editor with every decision editable. ${P.version
          ? `The loudness figures were measured on the ${P.version} master.` : P.loudness && P.loudness.measured ? `Loudness measured by ${P.loudness.measured}.` : "This cut was built before Render: its loudness figures were entered by hand, and the first render re-measures them."}`),
        h("div", { class: "stats" }, stat(P.version || (isCutJob() ? "cut" : "legacy"), P.version ? "version · rendered by GhostCut" : isCutJob() ? "director cut job" : "version · built before Render"),
          stat(num(lufs), "LUFS integrated"), stat(num(tp), "dBTP true peak"),
          Number.isFinite(gain) ? stat(`${num(gain)} dB`, "music bed gain") : null),
        h("div", { class: "cols" },
          h("div", { class: "box" }, h("h3", {}, "Outputs"),
            [["Master · 1080p59.94 MP4", "−14 LUFS web master"], ["Phone preview · 540p", "under 30 MB, for the client's phone"], ["Final Cut Pro · FCPXML", "cuts, B-roll lane, cards as titles"], ["Premiere Pro · XML", "FCP7 XML"], ["DaVinci Resolve", "imports the FCPXML"], ["Captions · SRT", "from Whisper words on the render"], ["Reshoot list", "missing shots, never generated"]]
              .map(([a, b], i) => h("label", { class: "out" }, h("input", { type: "checkbox", checked: i < 3 || null }), h("div", {}, h("b", {}, a), h("span", {}, b))))),
          h("div", { class: "box" }, h("h3", {}, `Loudness · ${P.version || (isCutJob() ? "cut.mp4" : "legacy build")}`),
            h("div", { class: "big" }, `${num(lufs)} LUFS`), h("p", {}, `True peak ${num(tp)} dBTP · target −14 LUFS ±1, ≤ −1 dBTP`),
            h("div", { class: "meter" }, h("div", { class: "tgt", style: `left:${pos(-15)};right:${`${100 - parseFloat(pos(-13))}%`}` }), h("i", { style: `left:${pos(lufs)}` })),
            h("p", { class: "muted", style: "font-size:11px" }, P.version ? `−24 … 0 LUFS · measured with ffmpeg ebur128 on the ${P.version} master` : P.loudness && P.loudness.measured ? `−24 … 0 LUFS · measured by ${P.loudness.measured}` : "−24 … 0 LUFS · entered by hand, not re-measured")),
          h("div", { class: "box" }, h("h3", {}, "Open before you send"),
            (P.findings || []).filter((f) => f.state === "open").map((f) => h("div", { class: "vol" }, f.title, h("span", {}, fmt(f.t)))),
            P.pending ? h("div", { class: "vol" }, `${P.pending} accepted change${P.pending === 1 ? "" : "s"} waiting to render`, h("span", {}, "Edit")) : null),
        ),
        h("p", {}, h("button", { class: "primary", style: "margin-top:16px", onclick: () => api("/api/job/reveal", {}).catch(() => {}) }, isCutJob() ? "Show the job folder" : "Export selected")));
      if (isCutJob()) api("/api/job/files", {}).then((r) => {  // real deliverables the cut job wrote
        const box = el.querySelector(".cols .box"); if (!box || !r.files || !r.files.length) return;
        box.replaceChildren(h("h3", {}, "Outputs · in the job folder"), ...r.files.map((f) =>
          h("div", { class: "out" }, h("div", {}, h("b", {}, f.label), h("span", {}, `${f.note} · ${f.size}`)),
            h("button", { onclick: () => api("/api/job/reveal", { name: f.name }).catch(() => {}) }, "Show in Finder"))));
      }).catch(() => {});
    },
  };

  renderTimeline();
  renderFindings();
  renderWords();
  lockEdits();
  if (LIVE && RS.state === "running") follow(RS.version); // reattach: the render outlives the window
  const start = location.hash.slice(1).split("/");
  // ONBOARD: first run opens on Import with the tour; an explicit view hash wins; #import/tour forces, #import/notour suppresses.
  let firstRun = false;
  if (LIVE && !PAGES[start[0]]) { try { firstRun = !!(await api("/api/settings")).first_run; } catch { firstRun = true; } }
  show(PAGES[start[0]] ? start[0] : (firstRun ? "import" : "edit"));
  if (window.GCTour && start[1] !== "notour" && ((start[0] === "import" && start[1] === "tour") || firstRun)) requestAnimationFrame(() => setTimeout(() => window.GCTour.open(window.GC), 250));
  tab(["review", "words", "why"].includes(start[1]) ? start[1] : "muse");
  // Open on a chosen frame (the cold open's title card) rather than a black first frame.
  // ONBOARD review: the first-run settings await above can let metadata load first, so seek now when it already has.
  if (P.startAt) { if (video.readyState >= 1) seek(P.startAt); else video.addEventListener("loadedmetadata", () => seek(P.startAt), { once: true }); }
})();
