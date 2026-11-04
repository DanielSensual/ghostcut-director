// GhostCut Director — "Dress the cut" panel: title cards, music bed, ghost pop-up (DRESS).
// Mounted by app.js with window.GCDress.mount(ctx); ctx carries h, $, api, fmt, LIVE(), project(), seek,
// applyResult, ask(text). Typed notes in the chat box and in this panel share one interceptor: dress
// notes ("add B-roll where I say synced") go to /api/dress/note; anything else goes to the chat as before.
// Muse Code (muse-wonder-1.0) designed this feature; this file was written by Claude Code after Muse's
// Meta API credit ran out mid-build. Planning works offline (built-in planner) when Muse is unavailable.
(() => {
  let ctx = null, panel = null, pollTimer = null, dressed = false, lastState = "";
  const glyph = () => (window.GhostMark?.svg ? window.GhostMark.svg(18) : null);
  const NOTE_TEXT = {
    dress_sfx_local: "Bing made locally (no ElevenLabs key)",
    dress_music_unavailable: "No music bed (ElevenLabs not connected)",
  };
  const noteText = (n) => NOTE_TEXT[n] || n.replace(/^dress_[a-z_]+:/, "");
  // The engine's 409 while dress.json is frozen (a dressed render or a plan is running).
  const BUSY = /^Dress is (rendering|planning)/;

  function live() { return !!(ctx && ctx.LIVE() && ctx.isCutJob()); }

  async function state() { return ctx.api("/api/dress"); }

  function setVideo(useDressed) {
    const v = ctx.video || document.querySelector("#video");
    if (!v) return Promise.resolve();
    const t = v.currentTime || 0, playing = !v.paused;
    dressed = !!useDressed;
    v.src = dressed ? `demo/dressed.mp4?v=${Date.now()}` : `demo/video.mp4?v=${Date.now()}`;
    return new Promise((resolve) => {
      let done = false;
      const finish = () => { if (done) return; done = true; try { v.currentTime = t; } catch { /* not seekable yet */ } if (playing) v.play().catch(() => {}); resolve(); };
      v.addEventListener("canplay", finish, { once: true });
      setTimeout(finish, 4000);
    });
  }

  function proposalCard(r) {
    const { h } = ctx;
    const note = (r.notes || [])[0] || {};
    const ops = note.ops || r.ops || [];
    const picks = [];
    const el = h("div", { class: "msg muse dressprop" },
      h("div", { class: "who" }, "Ghost · Dress the cut"));
    el.append(h("div", { class: "ops" }, ops.map((op, i) => {
      const cb = h("input", { type: "checkbox", checked: !op.blocked || null, disabled: op.blocked || null });
      picks.push(cb);
      return h("label", { class: `op${op.blocked ? " blocked" : ""}`, "data-i": i }, cb,
        h("div", {},
          h("b", {}, op.label),
          typeof op.t === "number" ? h("a", { class: "go", onclick: (e) => { e.preventDefault(); ctx.seek(op.t); } }, ctx.fmt(op.t)) : null,
          op.why ? h("div", { class: "why" }, op.why) : null,
          h("ul", { class: "opchecks" }, (op.checks || []).map((c) => h("li", { class: c.ok ? "ok" : c.hard ? "bad" : "warn" }, c.text)))));
    })));
    el.append(h("div", { class: "meta" }, "◆ rules · $0.00 · offline, checks are measured"));
    const acts = h("div", { class: "acts" });
    if (ops.some((o) => !o.blocked)) {
      const ok = h("button", { class: "ok" }, "Accept");
      const no = h("button", {}, "Reject");
      ok.onclick = async () => {
        ok.disabled = true;
        try {
          const pick = picks.map((c, i) => (c.checked ? i : -1)).filter((i) => i >= 0);
          const j = await ctx.api("/api/voice/apply", { id: r.id, pick });
          ctx.applyResult(j);
          picks.forEach((c) => (c.disabled = true));
          acts.replaceChildren(h("span", { class: "applied" }, "✓ Applied · open Dress the cut and Render"));
          refresh();
        } catch (e) { ok.disabled = false; acts.append(h("span", { class: "err" }, e.message)); }
      };
      no.onclick = () => { picks.forEach((c) => (c.disabled = true)); acts.replaceChildren(h("span", { class: "applied", style: "color:var(--dim)" }, "Rejected · kept as is")); };
      acts.append(ok, no);
    } else acts.append(h("span", { class: "applied", style: "color:var(--red, #e5484d)" }, "Every change was blocked by a check"));
    el.append(acts);
    return el;
  }

  // One interceptor for the chat box and the panel's text box.
  async function sendNote(text) {
    const chat = ctx.chat || document.querySelector("#chat");
    const v = ctx.video || document.querySelector("#video");
    let r = null;
    try { r = await ctx.api("/api/dress/note", { text, at: v ? v.currentTime || 0 : 0 }); }
    catch (e) {
      if (BUSY.test(e.message || "")) { // 409: say so here instead of handing a dress note to the chat
        chat.append(ctx.h("div", { class: "msg you" }, text));
        chat.append(ctx.h("div", { class: "msg muse dressprop" }, ctx.h("div", { class: "who" }, "Ghost · Dress the cut"),
          ctx.h("span", { class: "err" }, e.message)));
        ctx.scrollChat?.();
        return;
      }
      r = null;
    }
    if (!r || !r.matched) return ctx.ask(text);
    chat.append(ctx.h("div", { class: "msg you" }, text));
    chat.append(proposalCard(r));
    ctx.scrollChat?.();
  }

  function interceptChat() {
    document.addEventListener("submit", (e) => {
      if (!(e.target && e.target.id === "askForm") || !live()) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      const input = document.querySelector("#askInput");
      const text = (input?.value || "").trim();
      if (!text) return;
      input.value = "";
      sendNote(text);
    }, true);
  }

  function render(j) {
    if (!panel) return;
    const { h, fmt } = ctx;
    const s = j?.summary || { exists: false, cards: [] };
    const running = j?.state === "running";
    const lock = running || null; // render lock: no plan edits while the dressed cut renders (409)
    const body = panel.querySelector(".dress-body");
    const rows = [];
    if (!s.exists) rows.push(h("p", { class: "dress-muted" }, "No plan yet. Plan reads the transcript and proposes title cards; music and the bing come from ElevenLabs when it is connected."));
    else {
      const design = s.design || {};
      rows.push(h("div", { class: "dress-meta" }, `Cards by ${design.model === "rules" ? "the built-in planner" : design.model || "?"}${design.cached ? " (cached)" : ""} · providers $${Number(s.provider_spend_usd || 0).toFixed(2)} (est.)`));
      rows.push(h("ul", { class: "dress-cards" }, s.cards.length ? s.cards.map((c) => h("li", { class: c.enabled ? "" : "off" },
        h("input", { type: "checkbox", checked: c.enabled || null, disabled: lock, title: "Show this card", onchange: (e) => edit({ card_id: c.id, enabled: e.target.checked }) }),
        h("a", { class: "go", onclick: (e) => { e.preventDefault(); ctx.seek(c.t); } }, fmt(c.t)),
        h("span", { class: "dress-text" }, c.text),
        h("span", { class: "dress-src" }, c.source),
        h("button", { class: "x", title: "Remove", disabled: lock, onclick: () => edit({ card_id: c.id, remove: true }) }, "×"))) : [h("li", { class: "dress-muted" }, "No cards.")]));
      const music = s.music || {}, ghost = s.ghost || {};
      rows.push(h("div", { class: "dress-toggles" },
        h("label", {}, h("input", { type: "checkbox", checked: music.enabled || null, disabled: lock, onchange: (e) => edit({ ops: [{ op: "dress_set_music", enabled: e.target.checked }] }) }),
          ` Music ${music.has_asset ? `(${Number(music.gain_db).toFixed(0)} dB)` : "(no bed yet)"}`),
        h("label", {}, h("input", { type: "checkbox", checked: ghost.enabled || null, disabled: lock, onchange: (e) => edit({ ops: [{ op: "dress_set_ghost", enabled: e.target.checked }] }) }), " Ghost"),
        h("label", {}, h("input", { type: "checkbox", checked: ghost.sfx || null, disabled: lock, onchange: (e) => edit({ ops: [{ op: "dress_set_sfx", enabled: e.target.checked }] }) }), " Bing")));
      if (s.notes?.length) rows.push(h("ul", { class: "dress-notes" }, s.notes.map((n) => h("li", {}, noteText(n)))));
    }
    body.replaceChildren(...rows);
    const st = j?.status || {};
    const bar = panel.querySelector(".dress-bar");
    bar.hidden = !running;
    panel.querySelector(".dress-fill").style.width = `${Math.max(2, Number(st.pct || 0))}%`;
    panel.querySelector(".dress-status").textContent = running ? `${st.message || "Dressing the cut"} · ${Math.round(st.pct || 0)}%${st.eta_s ? ` · ${st.eta_s} s left` : ""}`
      : j?.state === "failed" ? `Render failed: ${j.message}` : j?.fresh ? "Dressed cut ready" : j?.stale ? "Plan changed since the last render" : "";
    panel.querySelector(".dress-render").disabled = running || !s.exists;
    panel.querySelector(".dress-plan").disabled = running;
    const ask = panel.querySelector(".dress-ask input");
    ask.disabled = running;
    ask.placeholder = running ? "Wait for the dressed render to finish" : "Tell Ghost what to add";
    const sw = panel.querySelector(".dress-switch");
    sw.hidden = !j?.fresh;
    sw.querySelectorAll("button").forEach((b) => b.classList.toggle("on", (b.dataset.v === "dressed") === dressed));
  }

  async function refresh() {
    if (!live() || !panel) return null;
    try { const j = await state(); render(j); return j; } catch (e) { panel.querySelector(".dress-status").textContent = e.message; return null; }
  }

  async function edit(body) {
    try { const j = await ctx.api("/api/dress/edit", body); ctx.applyResult(j); render(j); }
    catch (e) {
      if (BUSY.test(e.message || "")) await poll(); // re-sync: controls lock, the refused toggle snaps back
      panel.querySelector(".dress-status").textContent = e.message;
    }
  }

  async function plan() {
    const status = panel.querySelector(".dress-status");
    status.textContent = "Planning: reading the transcript…";
    panel.querySelector(".dress-plan").disabled = true;
    try { const j = await ctx.api("/api/dress/plan", {}); ctx.applyResult(j); render(j); }
    catch (e) {
      if (BUSY.test(e.message || "")) await poll(); else panel.querySelector(".dress-plan").disabled = false;
      status.textContent = e.message;
    }
  }

  async function poll() {
    clearTimeout(pollTimer);
    const j = await refresh();
    if (!j) return;
    const st = j.status || {};
    if (j.state === "running") {
      window.Kitchen?.update?.({ state: "running", step: "Dressing the cut", pct: st.pct || 0, eta_s: st.eta_s });
      lastState = "running"; // review fix: a panel opened mid-render still swaps the video when it finishes
      pollTimer = setTimeout(poll, 1000);
      return;
    }
    if (lastState === "running" && j.state === "done") {
      try { ctx.applyResult(await ctx.api("/api/project")); } catch { /* the panel still shows the switch */ }
      await setVideo(true);
      render(j);
      await window.Kitchen?.finish?.();
    } else if (lastState === "running" && j.state === "failed") {
      window.Kitchen?.fail?.(j.message || "The dressed render failed");
    }
    lastState = j.state;
  }

  async function startRender() {
    try {
      await ctx.api("/api/dress/render", {});
      lastState = "running";
      window.Kitchen?.start?.({ mode: "dress" });
      poll();
    } catch (e) { panel.querySelector(".dress-status").textContent = e.message; }
  }

  function build() {
    const { h } = ctx;
    const g = glyph();
    panel = h("section", { class: "dress-panel", id: "dressPanel", hidden: true, "aria-label": "Dress the cut" },
      h("header", {}, g || h("span", { class: "dress-glyph" }, "👻"), h("b", {}, "Dress the cut"),
        h("button", { class: "x", title: "Close", onclick: () => (panel.hidden = true) }, "×")),
      h("div", { class: "dress-body" }),
      h("div", { class: "dress-actions" },
        h("button", { class: "dress-plan", onclick: plan }, "Plan"),
        h("button", { class: "dress-render ok", onclick: startRender }, "Render"),
        h("span", { class: "dress-switch", hidden: true },
          h("button", { "data-v": "dressed", onclick: () => { setVideo(true); refresh(); } }, "Dressed"),
          h("button", { "data-v": "plain", onclick: () => { setVideo(false); refresh(); } }, "Plain"))),
      h("div", { class: "dress-bar", hidden: true }, h("div", { class: "dress-fill" })),
      h("div", { class: "dress-status" }),
      h("form", { class: "dress-ask", onsubmit: (e) => { e.preventDefault(); const i = e.target.querySelector("input"); const t = i.value.trim(); if (t) { i.value = ""; sendNote(t); } } },
        h("input", { type: "text", placeholder: "Tell Ghost what to add", "aria-label": "Tell Ghost what to add" })),
      h("p", { class: "dress-muted" }, "Try “add B-roll where I say synced” or “turn the music down”. Typing works everywhere; the mic needs a browser tab (the app window's mic is unavailable)."));
    (document.querySelector("#view-edit") || document.body).append(panel);
  }

  window.GCDress = {
    mount(c) {
      ctx = c;
      const btn = document.querySelector("#dressBtn");
      if (!live()) { if (btn) btn.hidden = true; return; }
      build();
      interceptChat();
      if (btn) btn.onclick = () => { panel.hidden = !panel.hidden; if (!panel.hidden) poll(); };
      poll();
    },
    refresh,
  };
})();
