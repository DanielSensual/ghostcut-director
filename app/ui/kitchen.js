// KITCHEN: interactive ghost-kitchen loading screen for the Edit tab.
// Plain classic script; three.js is lazy-loaded on first start so the
// Edit tab's load time is untouched. No network, no sound files.
(() => {
  "use strict";
  const $id = (id) => document.getElementById(id);
  const overlay = () => $id("kitchen");
  const canvasEl = () => $id("kitchenCanvas");
  const captionEl = () => $id("kitchenCaption");
  const readoutEl = () => $id("kitchenReadout");
  const CAPTIONS = [
    "Folding in a little B-roll",
    "Whisking the sync to soft peaks",
    "Marinating shot 7 of 23",
    "Teaching the ghosts to count frames",
    "Seasoning the cut to taste",
    "Proofing the timeline until it rises",
    "Deglazing the timeline with a splash of B-roll",
    "Simmering the angles until tender",
    "Plating the shots in order",
    "Folding silence in gently",
  ];
  const STAGE_ORDER = ["sync", "words", "tighten", "grade", "angles", "finish", "export", "reframe", "captions", "qc"];
  const STAGE_ACTION = {
    sync: "stir", words: "toss", tighten: "season", grade: "sparkle",
    angles: "peek", finish: "plate", export: "plate", reframe: "peek",
    captions: "toss", qc: "peek",
  };
  const STAGE_CAPTION = {
    sync: "Whisking the sync to soft peaks",
    words: "Tossing the words into the pot",
    tighten: "Trimming the fat off every take",
    grade: "Sprinkling a little colour magic",
    angles: "Peeking over the cards for the best angle",
    finish: "Plating the finished cut",
    export: "Plating the finished cut",
    reframe: "Peeking over the cards for the best angle",
    captions: "Tossing the letters onto the plate",
    qc: "Tasting the cut before it leaves the kitchen",
  };
  const CYCLE = ["stir", "season", "toss", "peek"];
  let active = false;
  let mode = null;
  let isPreview = false;
  let THREE = null;
  let renderer = null;
  let scene = null;
  let camera = null;
  let raf = 0;
  let lastFrame = 0;
  let accumulator = 0;
  let clockStart = 0;
  let captionTimer = 0;
  let cycleTimer = 0;
  let captionIndex = 0;
  let currentAction = "stir";
  let actionUntil = 0;
  let ghosts = [];
  let bubbles = [];
  let fallingCards = [];
  let sparkles = [];
  let letterBlocks = [];
  let potGroup = null;
  let plateGroup = null;
  let cardProto = null;
  let shared = null;
  let resizeObs = null;
  let lastDone = -1;
  let lastTotal = null;
  let lastPhase = null;
  let dressIndex = -1;
  let previewTimers = [];
  let audioCtx = null;
  let finishing = false;
  let failing = false;
  let gen = 0; // bumps on every start/stop so a late three.js import never builds a second scene
  let thumbCount = 0; // demo/thumbs holds one still per 2 s of the cut; shots map onto it by position
  let fallbackEl = null;
  let dragGhost = null;
  let dragMoved = false;
  let downPos = null;
  let downTime = 0;
  let raycaster = null;
  let pointerVec = null;

  const reducedMotion = () => {
    try { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); }
    catch (e) { return false; }
  };
  const fmtEta = (s) => {
    if (s == null || !isFinite(s)) return "";
    if (s >= 90) return `about ${Math.round(s / 60)} min left`;
    if (s >= 10) return `about ${Math.round(s)} s left`;
    return `about ${Math.max(1, Math.round(s))} s left`;
  };
  const setCaption = (text) => {
    const el = captionEl(); if (el) el.textContent = text || "";
  };
  const setReadout = (text, shimmer) => {
    const el = readoutEl(); if (!el) return;
    el.textContent = text || "";
    el.classList.toggle("kitchen-shimmer", !!shimmer);
  };
  const thumbUrl = (index) => `demo/thumbs/${String(Math.max(1, index)).padStart(4, "0")}.jpg`;
  // Shot d of N (1-based) -> the still from the middle of its stretch of the cut, when the cut's length is known.
  const cardThumb = (d) => {
    const tot = lastTotal || 0;
    if (!thumbCount || !tot) return Math.max(1, d);
    return Math.max(1, Math.min(thumbCount, 1 + Math.floor(((Math.max(1, d) - 0.5) / tot) * thumbCount)));
  };

  // ---------- CSS fallback (reduced motion / no WebGL / no vendor) ----------
  function showFallback() {
    const ov = overlay(); if (!ov) return;
    disposeScene();
    if (!fallbackEl) {
      fallbackEl = document.createElement("div");
      fallbackEl.className = "kitchen-fallback";
      const mascot = document.createElement("div");
      mascot.className = "kitchen-mascot";
      let mark = null;
      try { mark = window.GhostMark ? window.GhostMark(64, "cook") : null; } catch (e) { mark = null; }
      if (mark && typeof mark === "object" && mark.nodeType) mascot.appendChild(mark);
      else if (mark && typeof mark === "string" && mark.trim().startsWith("<")) mascot.innerHTML = mark;
      else if (mark) mascot.textContent = String(mark);
      else mascot.textContent = "\u{1F47B}";
      const label = document.createElement("div");
      label.textContent = "The ghosts are cooking\u2026";
      label.style.color = "var(--muted, #8a93a8)";
      fallbackEl.appendChild(mascot);
      fallbackEl.appendChild(label);
      ov.appendChild(fallbackEl);
    }
    fallbackEl.hidden = false;
    const cv = canvasEl(); if (cv) cv.hidden = true;
  }
  function hideFallback() {
    if (fallbackEl) fallbackEl.hidden = true;
    const cv = canvasEl(); if (cv) cv.hidden = false;
  }

  // ---------- audio: two-note bell, created on first click only ----------
  function playBing() {
    try {
      if (!audioCtx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        audioCtx = new AC();
      }
      if (audioCtx.state === "suspended") audioCtx.resume();
      const t0 = audioCtx.currentTime;
      [[880, 0], [1318.5, 0.12]].forEach(([freq, off]) => {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = "sine"; osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, t0 + off);
        gain.gain.exponentialRampToValueAtTime(0.18, t0 + off + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + off + 0.55);
        osc.connect(gain); gain.connect(audioCtx.destination);
        osc.start(t0 + off); osc.stop(t0 + off + 0.6);
      });
    } catch (e) { /* audio is a garnish */ }
  }
  function heeHee(x, y) {
    const ov = overlay(); if (!ov) return;
    const b = document.createElement("div");
    b.className = "kitchen-heehee"; b.textContent = "hee hee";
    b.style.left = `${x}px`; b.style.top = `${y}px`;
    ov.appendChild(b);
    setTimeout(() => b.remove(), 1050);
  }

  // ---------- three.js scene (primitives only, shared geo/mat) ----------
  function disposeScene() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    if (resizeObs) { try { resizeObs.disconnect(); } catch (e) { /* ignore */ } resizeObs = null; }
    if (scene) {
      scene.traverse((obj) => {
        if (obj.geometry && !(shared && Object.values(shared.geo || {}).includes(obj.geometry))) {
          try { obj.geometry.dispose(); } catch (e) { /* ignore */ }
        }
        const mats = Array.isArray(obj.material) ? obj.material : (obj.material ? [obj.material] : []);
        mats.forEach((m) => { try { if (m.map) m.map.dispose(); } catch (e) { /* ignore */ } try { m.dispose(); } catch (e) { /* ignore */ } });
      });
    }
    if (shared) {
      Object.values(shared.geo || {}).forEach((g) => { try { g.dispose(); } catch (e) { /* ignore */ } });
      Object.values(shared.mat || {}).forEach((m) => { try { m.dispose(); } catch (e) { /* ignore */ } });
    }
    if (renderer) { try { renderer.dispose(); } catch (e) { /* ignore */ } renderer = null; }
    scene = null; camera = null; ghosts = []; bubbles = []; fallingCards = [];
    sparkles = []; letterBlocks = []; potGroup = null; plateGroup = null; shared = null;
  }
  function makeGhost(scale, color, withHat) {
    const T = THREE;
    const g = new T.Group();
    const bodyMat = new T.MeshLambertMaterial({ color, transparent: true, opacity: 0.92 });
    const body = new T.Mesh(shared.geo.capsule, bodyMat);
    body.scale.setScalar(scale);
    body.position.y = 0.55 * scale;
    g.add(body);
    // wavy hem: small spheres around the base
    for (let i = 0; i < 5; i++) {
      const s = new T.Mesh(shared.geo.hemSphere, bodyMat);
      const a = (i / 5) * Math.PI * 2;
      s.position.set(Math.cos(a) * 0.34 * scale, 0.08 * scale, Math.sin(a) * 0.2 * scale);
      s.scale.setScalar(scale);
      g.add(s);
    }
    const eyeMat = shared.mat.eye;
    [-1, 1].forEach((side) => {
      const eye = new T.Mesh(shared.geo.eye, eyeMat);
      eye.position.set(side * 0.16 * scale, 0.78 * scale, 0.36 * scale);
      eye.scale.setScalar(scale);
      g.add(eye);
    });
    if (withHat) {
      const hatMat = shared.mat.hat;
      const brim = new T.Mesh(shared.geo.hatBrim, hatMat);
      brim.position.y = 1.18 * scale; brim.scale.setScalar(scale); g.add(brim);
      const top = new T.Mesh(shared.geo.hatTop, hatMat);
      top.position.y = 1.38 * scale; top.scale.setScalar(scale); g.add(top);
      const puff = new T.Mesh(shared.geo.hatPuff, hatMat);
      puff.position.y = 1.52 * scale; puff.scale.setScalar(scale); g.add(puff);
    }
    // spoon for the chef
    if (withHat) {
      const spoon = new T.Group();
      const handle = new T.Mesh(shared.geo.spoonHandle, shared.mat.spoon);
      handle.rotation.z = 0.9; handle.position.set(0.5 * scale, 0.6 * scale, 0.3 * scale);
      spoon.add(handle);
      g.add(spoon);
      g.userData.spoon = spoon;
    }
    g.userData.scale = scale;
    g.userData.baseY = 0;
    g.userData.spinVel = 0;
    g.userData.giggleUntil = 0;
    g.userData.danceUntil = 0;
    g.userData.phase = Math.random() * Math.PI * 2;
    g.userData.bodyMat = bodyMat;
    return g;
  }
  function buildScene() {
    const T = THREE;
    scene = new T.Scene();
    scene.background = new T.Color(0x0b0d12);
    const cv = canvasEl();
    const w = (cv && cv.clientWidth) || 640;
    const hgt = (cv && cv.clientHeight) || 360;
    camera = new T.PerspectiveCamera(42, Math.max(0.2, w / Math.max(1, hgt)), 0.1, 100);
    camera.position.set(0, 2.4, 6.2);
    camera.lookAt(0, 0.9, 0);
    renderer = new T.WebGLRenderer({ canvas: cv, antialias: false, powerPreference: "low-power" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    renderer.setSize(w, hgt, false);
    renderer.outputColorSpace = T.SRGBColorSpace;
    shared = {
      geo: {
        capsule: new T.CapsuleGeometry(0.42, 0.55, 3, 8),
        hemSphere: new T.SphereGeometry(0.13, 6, 4),
        eye: new T.SphereGeometry(0.055, 5, 3),
        hatBrim: new T.CylinderGeometry(0.46, 0.46, 0.09, 12),
        hatTop: new T.CylinderGeometry(0.34, 0.38, 0.3, 10),
        hatPuff: new T.SphereGeometry(0.3, 7, 5),
        spoonHandle: new T.CylinderGeometry(0.03, 0.03, 0.7, 5),
        box: new T.BoxGeometry(1, 1, 1),
        card: new T.BoxGeometry(0.92, 0.52, 0.03),
        bubble: new T.SphereGeometry(0.07, 6, 4),
        spark: new T.OctahedronGeometry(0.07, 0),
        letter: new T.BoxGeometry(0.18, 0.18, 0.18),
      },
      mat: {
        eye: new T.MeshLambertMaterial({ color: 0x10131a }),
        hat: new T.MeshLambertMaterial({ color: 0xf2f4fa }),
        spoon: new T.MeshLambertMaterial({ color: 0x8a93a8 }),
        counter: new T.MeshLambertMaterial({ color: 0x161a23 }),
        wall: new T.MeshLambertMaterial({ color: 0x10131a }),
        pot: new T.MeshLambertMaterial({ color: 0x2e3445 }),
        potRim: new T.MeshLambertMaterial({ color: 0x3d465e }),
        glow: new T.MeshLambertMaterial({ color: 0xff6a1a, emissive: 0xff6a1a, emissiveIntensity: 1.6 }),
        bubble: new T.MeshLambertMaterial({ color: 0xffd9b8, transparent: true, opacity: 0.85, emissive: 0xff8a45, emissiveIntensity: 0.4 }),
        cardPlain: new T.MeshLambertMaterial({ color: 0x9db8ff }),
        spark: new T.MeshLambertMaterial({ color: 0xffe9a8, emissive: 0xffb84d, emissiveIntensity: 1.2 }),
        dark: new T.MeshLambertMaterial({ color: 0x05070a }),
        plate: new T.MeshLambertMaterial({ color: 0xe8ebf2 }),
      },
    };
    // lights: warm point over the pot + cool rim, plus soft ambient
    scene.add(new T.AmbientLight(0x333344, 1.2));
    scene.add(new T.AmbientLight(0x8899bb, 0.55));
    const warm = new T.PointLight(0xff8a45, 18, 12, 1.6);
    warm.position.set(0, 2.2, 1.2);
    scene.add(warm);
    const rim = new T.DirectionalLight(0x9db8ff, 1.4);
    rim.position.set(-3, 3, -2);
    scene.add(rim);
    const fill = new T.DirectionalLight(0xffffff, 0.5);
    fill.position.set(2, 3, 4);
    scene.add(fill);
    // back wall + counter
    const wall = new T.Mesh(shared.geo.box, shared.mat.wall);
    wall.scale.set(9, 5, 0.15); wall.position.set(0, 2.0, -1.6); scene.add(wall);
    const counter = new T.Mesh(shared.geo.box, shared.mat.counter);
    counter.scale.set(9, 0.18, 2.6); counter.position.set(0, 0.02, 0.4); scene.add(counter);
    const counterFront = new T.Mesh(shared.geo.box, shared.mat.counter);
    counterFront.scale.set(9, 1.0, 0.15); counterFront.position.set(0, -0.5, 1.65); scene.add(counterFront);
    // pan + spoon silhouettes on the wall
    const pan = new T.Group();
    const panBody = new T.Mesh(new T.CylinderGeometry(0.42, 0.42, 0.06, 16), shared.mat.dark);
    panBody.rotation.x = Math.PI / 2; pan.add(panBody);
    const panHandle = new T.Mesh(shared.geo.box, shared.mat.dark);
    panHandle.scale.set(0.09, 0.5, 0.04); panHandle.position.set(0.45, 0.3, 0); panHandle.rotation.z = -0.7; pan.add(panHandle);
    pan.position.set(-2.4, 2.5, -1.45); scene.add(pan);
    const spoonWall = new T.Group();
    const sw1 = new T.Mesh(shared.geo.box, shared.mat.dark); sw1.scale.set(0.07, 0.7, 0.04); spoonWall.add(sw1);
    const sw2 = new T.Mesh(shared.geo.bubble, shared.mat.dark); sw2.scale.set(1.4, 1.9, 0.5); sw2.position.y = -0.45; spoonWall.add(sw2);
    spoonWall.position.set(2.5, 2.6, -1.45); spoonWall.rotation.z = 0.25; scene.add(spoonWall);
    // the big pot with orange glow
    potGroup = new T.Group();
    const potBody = new T.Mesh(new T.CylinderGeometry(0.95, 0.8, 0.85, 18, 1, true), shared.mat.pot);
    potBody.position.y = 0.55; potGroup.add(potBody);
    const potBottom = new T.Mesh(new T.CylinderGeometry(0.8, 0.8, 0.08, 18), shared.mat.pot);
    potBottom.position.y = 0.14; potGroup.add(potBottom);
    const rimRing = new T.Mesh(new T.TorusGeometry(0.95, 0.07, 4, 18), shared.mat.potRim);
    rimRing.rotation.x = Math.PI / 2; rimRing.position.y = 0.98; potGroup.add(rimRing);
    const glowDisc = new T.Mesh(new T.CircleGeometry(0.88, 18), shared.mat.glow);
    glowDisc.rotation.x = -Math.PI / 2; glowDisc.position.y = 0.96; potGroup.add(glowDisc);
    potGroup.position.set(0, 0.1, 0.35);
    scene.add(potGroup);
    // rising bubbles
    for (let i = 0; i < 8; i++) {
      const b = new T.Mesh(shared.geo.bubble, shared.mat.bubble);
      b.userData.seed = Math.random();
      b.position.set((Math.random() - 0.5) * 1.2, 1.05, 0.35 + (Math.random() - 0.5) * 0.8);
      scene.add(b); bubbles.push(b);
    }
    // sparkles (grade) — hidden until sprinkled
    for (let i = 0; i < 14; i++) {
      const s = new T.Mesh(shared.geo.spark, shared.mat.spark);
      s.visible = false; s.userData.seed = Math.random();
      scene.add(s); sparkles.push(s);
    }
    // letter blocks (captions) — small coloured boxes
    const letterCols = [0xff6a1a, 0x2dd4bf, 0x9db8ff, 0x7c6cf2, 0xe6b35a];
    for (let i = 0; i < 6; i++) {
      const m = new T.Mesh(shared.geo.letter, new T.MeshLambertMaterial({ color: letterCols[i % letterCols.length] }));
      m.visible = false; m.userData.seed = i / 6;
      scene.add(m); letterBlocks.push(m);
    }
    // plate for the plating beat (hidden until finish)
    plateGroup = new T.Group();
    const plate = new T.Mesh(new T.CylinderGeometry(0.7, 0.55, 0.08, 16), shared.mat.plate);
    plateGroup.add(plate);
    const plateCard = new T.Mesh(shared.geo.card, shared.mat.cardPlain.clone());
    plateCard.position.y = 0.1; plateCard.rotation.x = -0.2; plateGroup.add(plateCard);
    plateGroup.userData.card = plateCard;
    plateGroup.visible = false;
    plateGroup.position.set(-1.6, 1.2, 0.8);
    scene.add(plateGroup);
    // ghosts: head chef + smaller helpers
    const chef = makeGhost(1.15, 0xeef2ff, true);
    chef.position.set(-1.55, 0.95, 0.15); chef.userData.baseY = 0.95; chef.userData.homeX = -1.55;
    scene.add(chef); ghosts.push(chef);
    const cols = [0x9db8ff, 0x2dd4bf, 0xcfc4ff];
    const spots = [[1.65, 1.15, -0.1], [0.9, 1.75, -0.7], [-0.7, 1.85, -0.8]];
    spots.forEach((p, i) => {
      const g = makeGhost(0.62 + (i % 2) * 0.12, cols[i], false);
      g.position.set(p[0], p[1], p[2]); g.userData.baseY = p[1]; g.userData.homeX = p[0];
      scene.add(g); ghosts.push(g);
    });
    raycaster = new T.Raycaster();
    pointerVec = new T.Vector2();
    bindPointer();
    // ResizeObserver on .player keeps the canvas fitted without layout thrash
    const player = document.querySelector(".player");
    if (player && window.ResizeObserver) {
      resizeObs = new ResizeObserver(() => resize());
      resizeObs.observe(player);
    }
    resize();
  }
  function resize() {
    if (!renderer || !camera) return;
    const cv = canvasEl(); if (!cv) return;
    const parent = cv.parentElement;
    const w = Math.max(2, (parent && parent.clientWidth) || cv.clientWidth || 640);
    const hgt = Math.max(2, (parent && parent.clientHeight) || cv.clientHeight || 360);
    camera.aspect = w / hgt; camera.updateProjectionMatrix();
    renderer.setSize(w, hgt, false);
  }
  function splash() {
    if (!scene || !THREE) return;
    for (let i = 0; i < 4; i++) {
      const m = new THREE.Mesh(shared.geo.bubble, shared.mat.bubble);
      const a = (i / 4) * Math.PI * 2 + Math.random();
      m.position.set(Math.cos(a) * 0.3, 1.12, 0.35 + Math.sin(a) * 0.3);
      m.userData.vel = new THREE.Vector3(Math.cos(a) * 0.6, 1.1 + Math.random() * 0.5, Math.sin(a) * 0.6);
      m.userData.life = 0.55;
      scene.add(m);
      fallingCards.push({ splash: m });
    }
  }
  function dropCard(index) {
    if (!scene || !THREE || !shared) return;
    const T = THREE;
    const mat = shared.mat.cardPlain.clone();
    const mesh = new T.Mesh(shared.geo.card, mat);
    mesh.position.set((Math.random() - 0.5) * 0.5, 3.4, 0.35);
    mesh.rotation.set(0.3, Math.random() * 0.6 - 0.3, 0.15);
    mesh.userData.vy = 0;
    scene.add(mesh);
    fallingCards.push({ mesh, vy: 0, index });
    try {
      new T.TextureLoader().load(thumbUrl(cardThumb(index)), (tex) => {
        try { tex.colorSpace = T.SRGBColorSpace; } catch (e) { /* older */ }
        mat.map = tex; mat.color.set(0xffffff); mat.needsUpdate = true;
      }, undefined, () => { /* plain card fallback stays */ });
    } catch (e) { /* plain card fallback stays */ }
  }
  function setAction(name) {
    if (currentAction === name) return;
    currentAction = name;
    actionUntil = performance.now() + 2200;
    sparkles.forEach((s) => { s.visible = (name === "sparkle" || name === "season"); });
    letterBlocks.forEach((b) => { b.visible = (name === "toss"); });
    if (plateGroup) plateGroup.visible = (name === "plate");
  }

  // ---------- pointer interaction (raycast) ----------
  function pointerNDC(ev) {
    const cv = canvasEl(); if (!cv || !pointerVec) return null;
    const r = cv.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    pointerVec.x = ((ev.clientX - r.left) / r.width) * 2 - 1;
    pointerVec.y = -((ev.clientY - r.top) / r.height) * 2 + 1;
    return { x: ev.clientX - r.left, y: ev.clientY - r.top, rect: r };
  }
  function pickGhost(ev) {
    if (!raycaster || !camera || !scene) return null;
    const p = pointerNDC(ev); if (!p) return null;
    raycaster.setFromCamera(pointerVec, camera);
    const hits = raycaster.intersectObjects(ghosts, true);
    if (!hits.length) return null;
    let obj = hits[0].object;
    while (obj && !ghosts.includes(obj)) obj = obj.parent;
    return obj;
  }
  function bindPointer() {
    const cv = canvasEl(); if (!cv || cv._kitchenBound) return;
    cv._kitchenBound = true;
    cv.addEventListener("pointermove", (ev) => {
      if (dragGhost && downPos) {
        const dx = ev.clientX - downPos.x, dy = ev.clientY - downPos.y;
        if (Math.abs(dx) + Math.abs(dy) > 6) dragMoved = true;
        if (dragMoved && raycaster && camera) {
          const p = pointerNDC(ev); if (!p) return;
          raycaster.setFromCamera(pointerVec, camera);
          const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -(dragGhost.userData.baseY || 1));
          const hit = new THREE.Vector3();
          if (raycaster.ray.intersectPlane(plane, hit)) {
            dragGhost.position.x = Math.max(-3.2, Math.min(3.2, hit.x));
            dragGhost.position.z = Math.max(-0.9, Math.min(1.1, hit.z));
          }
        }
        return;
      }
      const hit = pickGhost(ev);
      cv.style.cursor = hit ? "pointer" : "";
    });
    cv.addEventListener("pointerdown", (ev) => {
      const hit = pickGhost(ev);
      downPos = { x: ev.clientX, y: ev.clientY }; downTime = performance.now(); dragMoved = false;
      dragGhost = hit;
      if (hit) { try { cv.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ } }
    });
    cv.addEventListener("pointerup", (ev) => {
      const wasDrag = dragMoved;
      const g = dragGhost;
      dragGhost = null;
      if (!g) return;
      if (wasDrag) {
        g.userData.spinVel = 7;
        return;
      }
      // click: squash-and-stretch giggle + bubble + bing
      const p = pointerNDC(ev);
      g.userData.giggleUntil = performance.now() + 650;
      playBing();
      if (p) heeHee(p.x, p.y);
    });
    cv.addEventListener("dblclick", (ev) => {
      const g = pickGhost(ev);
      if (g) { g.userData.danceUntil = performance.now() + 1600; g.userData.spinVel = 5; playBing(); }
    });
  }

  // ---------- render loop: manual RAF capped at 30 fps ----------
  function isPaused() {
    if (document.hidden) return true;
    const view = $id("view-edit");
    if (view && view.hidden) return true;
    return false;
  }
  function loop(now) {
    if (!active) return;
    raf = requestAnimationFrame(loop);
    if (!renderer || !scene || !camera) return;
    if (isPaused()) return;
    if (!lastFrame) lastFrame = now;
    const dtMs = now - lastFrame;
    lastFrame = now;
    accumulator += dtMs;
    if (accumulator < 33) return;
    const dt = Math.min(0.05, accumulator / 1000);
    accumulator = 0;
    const t = (now - clockStart) / 1000;
    // ghosts: sine bob, giggle squash, dance, spin, stir lean
    ghosts.forEach((g, gi) => {
      const baseY = g.userData.baseY || g.position.y;
      let y = baseY + Math.sin(t * 1.7 + g.userData.phase) * 0.09;
      let sx = 1, sy = 1;
      if (performance.now() < g.userData.giggleUntil) {
        const k = Math.sin((g.userData.giggleUntil - performance.now()) / 650 * Math.PI);
        sx = 1 + 0.28 * k; sy = 1 - 0.3 * k; y += 0.06 * k;
      } else if (performance.now() < g.userData.danceUntil) {
        y += Math.abs(Math.sin(t * 10)) * 0.22;
        g.rotation.y += dt * 9;
      }
      if (g.userData.spinVel) {
        g.rotation.y += g.userData.spinVel * dt;
        g.userData.spinVel *= Math.pow(0.12, dt);
        if (Math.abs(g.userData.spinVel) < 0.05) g.userData.spinVel = 0;
      }
      g.position.y = y;
      g.scale.set(sx, sy, sx);
      if (g.userData.spoon) {
        g.userData.spoon.rotation.y = currentAction === "stir" ? Math.sin(t * 3.2) * 0.55 : Math.sin(t * 1.2) * 0.15;
        g.userData.spoon.rotation.z = currentAction === "stir" ? Math.cos(t * 3.2) * 0.25 : 0;
      }
      if (currentAction === "sad") {
        g.position.y = baseY - 0.18 + Math.sin(t * 1.1 + g.userData.phase) * 0.03;
        g.rotation.x = 0.35;
      } else if (currentAction === "peek" && gi > 0) {
        g.position.y = (g.userData.baseY || y) + Math.sin(t * 2.2 + gi) * 0.05 - 0.12;
        g.rotation.x = 0.28;
      } else if (gi > 0) g.rotation.x *= 0.9;
    });
    // bubbles rise and reset
    bubbles.forEach((b) => {
      b.position.y += dt * (0.35 + b.userData.seed * 0.4);
      b.position.x += Math.sin(t * 2 + b.userData.seed * 9) * dt * 0.08;
      if (b.position.y > 1.75) { b.position.y = 1.02; b.position.x = (Math.random() - 0.5) * 1.2; }
      const s = 0.7 + 0.5 * Math.sin(t * 3 + b.userData.seed * 12);
      b.scale.setScalar(Math.max(0.4, s));
    });
    // falling cards + splash particles
    for (let i = fallingCards.length - 1; i >= 0; i--) {
      const c = fallingCards[i];
      if (c.splash) {
        c.splash.userData.life -= dt;
        c.splash.position.addScaledVector(c.splash.userData.vel, dt);
        c.splash.userData.vel.y -= 3.2 * dt;
        if (c.splash.userData.life <= 0) { scene.remove(c.splash); fallingCards.splice(i, 1); }
        continue;
      }
      c.vy += 5.5 * dt;
      c.mesh.position.y -= c.vy * dt;
      c.mesh.rotation.x += dt * 1.4; c.mesh.rotation.z += dt * 0.7;
      if (c.mesh.position.y <= 1.08) {
        splash();
        scene.remove(c.mesh);
        try { if (c.mesh.material.map) c.mesh.material.map.dispose(); c.mesh.material.dispose(); } catch (e) { /* ignore */ }
        fallingCards.splice(i, 1);
      }
    }
    // sparkles drift over the pot
    sparkles.forEach((s) => {
      if (!s.visible) return;
      s.position.set(Math.sin(t * 1.4 + s.userData.seed * 20) * 1.1, 1.5 + Math.sin(t * 2.1 + s.userData.seed * 30) * 0.35, 0.35 + Math.cos(t * 1.1 + s.userData.seed * 17) * 0.5);
      s.rotation.y += dt * 3; s.rotation.x += dt * 2;
    });
    // letter blocks toss in an arc
    letterBlocks.forEach((b, bi) => {
      if (!b.visible) return;
      const ph = (t * 0.7 + b.userData.seed) % 1;
      b.position.set(-1.2 + ph * 2.4, 1.1 + Math.sin(ph * Math.PI) * 1.15, 0.45);
      b.rotation.x += dt * 4; b.rotation.y += dt * 3;
      b.visible = true; void bi;
    });
    // plate lift during the finish beat is driven by finish()
    try { renderer.render(scene, camera); } catch (e) { showFallback(); }
  }
  let captionMode = "rotate";
  let lastCycleAt = 0;
  let cycleIndex = 0;
  function clearKitchenTimers() {
    if (captionTimer) { clearInterval(captionTimer); captionTimer = 0; }
    if (cycleTimer) { clearInterval(cycleTimer); cycleTimer = 0; }
    previewTimers.forEach((id) => clearTimeout(id));
    previewTimers = [];
  }
  function rotateCaption() {
    if (captionMode !== "rotate") return;
    captionIndex = (captionIndex + 1) % CAPTIONS.length;
    const base = CAPTIONS[captionIndex];
    if (lastTotal && lastDone >= 0 && base.startsWith("Marinating")) {
      setCaption(`Marinating shot ${Math.min(lastTotal, lastDone + 1)} of ${lastTotal}`);
    } else setCaption(base);
  }
  function cycleTick() {
    if (!active || finishing) return;
    if (lastPhase === "plan" || lastPhase === "join") return;
    if (captionMode === "stage") return;
    const ratio = (lastTotal && lastDone > 0) ? lastDone / lastTotal : 0;
    const interval = 4000 - Math.round(ratio * 1800);
    const now = performance.now();
    if (now - lastCycleAt < interval) return;
    lastCycleAt = now;
    cycleIndex = (cycleIndex + 1) % CYCLE.length;
    setAction(CYCLE[cycleIndex]);
  }
  function dropMany(fromDone, toDone) {
    for (let d = fromDone + 1; d <= toDone; d++) {
      const idx = d;
      const delay = (d - fromDone - 1) * 140;
      if (delay === 0) dropCard(idx);
      else previewTimers.push(setTimeout(() => dropCard(idx), delay));
    }
  }
  function applyShots(done, total, pct, eta, prefix) {
    const tot = total != null ? total : lastTotal;
    if (done != null && lastDone >= 0 && done > lastDone) dropMany(lastDone, done);
    else if (done != null && lastDone < 0 && done > 0) dropMany(-1, Math.min(done, 3));
    if (done != null) lastDone = done;
    if (tot != null) lastTotal = tot;
    const p = pct != null ? Math.round(pct) : (tot ? Math.round((100 * (done || 0)) / tot) : null);
    const parts = [];
    if (tot != null && done != null) parts.push(`shots ${done}/${tot}`);
    if (p != null) parts.push(`${p}%`);
    const etaText = fmtEta(eta);
    if (etaText) parts.push(etaText);
    setReadout(prefix + parts.join(" \u00b7 "), false);
  }
  function stageOf(st) {
    const stages = (st && typeof st.stages === "object" && st.stages) || {};
    for (const name of STAGE_ORDER) {
      if (!(name in stages)) return name;
    }
    return "export";
  }

  // ---------- public API ----------
  function update(st) {
    if (!active || finishing || failing || !st || typeof st !== "object") return;
    const prefix = isPreview ? "preview \u00b7 " : "";
    if (st.state === "failed") { fail(st.message || "Re-render failed"); return; }
    // stage-style status (offload cut): choreography by stage
    if (st.stages && typeof st.stages === "object") {
      const stage = stageOf(st);
      captionMode = "stage"; lastPhase = null;
      setAction(STAGE_ACTION[stage] || "stir");
      setCaption(STAGE_CAPTION[stage] || CAPTIONS[0]);
      const doneCount = STAGE_ORDER.filter((n) => n in st.stages).length;
      setReadout(`${prefix}${stage} \u00b7 ${doneCount}/${STAGE_ORDER.length} stages`, false);
      return;
    }
    // cutjob / preview shape: {phase, done, total, pct, eta_s}
    if (st.phase != null || st.done != null || st.total != null) {
      const phase = st.phase || "shots";
      lastPhase = phase;
      if (phase === "plan") {
        captionMode = "plan";
        setCaption("Picking the angles");
        setReadout(`${prefix}Picking the angles`, true);
        setAction("stir");
        if (st.done != null) lastDone = st.done;
        if (st.total != null) lastTotal = st.total;
        return;
      }
      if (phase === "join") {
        captionMode = "join";
        setCaption("Joining the plates");
        setReadout(`${prefix}Joining the plates`, true);
        setAction("plate");
        if (st.total != null) {
          if (lastDone >= 0 && st.total > lastDone) dropMany(lastDone, st.total);
          lastTotal = st.total; lastDone = st.total;
        }
        return;
      }
      captionMode = "rotate";
      applyShots(st.done != null ? st.done : lastDone, st.total != null ? st.total : lastTotal,
        st.pct != null ? st.pct : null, st.eta_s != null ? st.eta_s : null, prefix);
      return;
    }
    // dress shape: {state:"running", step, pct, eta_s} — pct drives cards
    if (mode === "dress" || (st.pct != null && st.step != null)) {
      captionMode = st.step ? "stage" : "rotate";
      if (st.step) setCaption(String(st.step));
      const tot = st.total != null ? st.total : (st.n != null ? st.n : (lastTotal || 23));
      lastTotal = tot;
      const pct = Math.max(0, Math.min(100, +st.pct || 0));
      const target = Math.floor((pct / 100) * tot);
      if (target > dressIndex) { dropMany(dressIndex, target); dressIndex = target; }
      lastDone = target; lastPhase = "shots";
      applyShots(target, tot, pct, st.eta_s != null ? st.eta_s : null, prefix);
      // applyShots would double-drop; dressIndex already advanced, keep lastDone in sync
      lastDone = target;
      return;
    }
    // legacy shape: {step, i, n, pct, eta_s}
    if (st.i != null || st.n != null || st.pct != null) {
      captionMode = "rotate";
      const done = st.i != null ? st.i : null;
      const tot = st.n != null ? st.n : lastTotal;
      const pct = st.pct != null ? st.pct : (done != null && tot ? (100 * done) / tot : null);
      lastPhase = "shots";
      applyShots(done, tot, pct, st.eta_s != null ? st.eta_s : null, prefix);
      if (st.step) setCaption(String(st.step));
      return;
    }
    if (st.message) setCaption(String(st.message));
  }
  function start(opts) {
    if (active && failing) stop(); // a retry during the 2 s sad-ghost beat gets a fresh kitchen
    if (active) return;
    const ov = overlay(); if (!ov) return;
    mode = (opts && opts.mode) || "cutjob";
    isPreview = mode === "preview";
    thumbCount = Math.max(0, Math.floor(+(opts && opts.thumbs) || 0));
    if (!thumbCount) {
      try { const d = +(window.PROJECT && window.PROJECT.duration); if (d > 0) thumbCount = Math.floor(d / 2); } catch (e) { thumbCount = 0; }
    }
    active = true; finishing = false; failing = false; currentAction = "stir";
    const myGen = ++gen;
    lastDone = -1; lastTotal = null; lastPhase = null; dressIndex = -1;
    captionMode = "rotate"; captionIndex = 0; cycleIndex = 0; lastCycleAt = performance.now();
    clockStart = performance.now(); lastFrame = 0; accumulator = 0;
    ov.hidden = false;
    ov.classList.remove("kitchen-fading");
    const cv0 = canvasEl(); if (cv0) { cv0.hidden = false; cv0.style.opacity = ""; }
    setCaption(CAPTIONS[0]);
    setReadout(isPreview ? "preview \u00b7 Picking the angles" : "Picking the angles", true);
    captionTimer = setInterval(rotateCaption, 2500);
    cycleTimer = setInterval(cycleTick, 500);
    if (reducedMotion()) { showFallback(); return; }
    hideFallback();
    // lazy three.js: dynamic import keeps Edit's first load untouched
    import("./vendor/three.module.min.js").then((m) => {
      if (!active || finishing || failing || myGen !== gen) return;
      THREE = m;
      try {
        buildScene();
        hideFallback();
        clockStart = performance.now();
        raf = requestAnimationFrame(loop);
      } catch (e) { showFallback(); }
    }).catch(() => { if (active && myGen === gen) showFallback(); });
  }
  function finish() {
    if (!active || finishing) return Promise.resolve();
    finishing = true;
    clearKitchenTimers();
    const ov = overlay();
    return new Promise((resolve) => {
      const done = () => {
        if (ov) ov.hidden = true;
        disposeScene();
        hideFallback();
        active = false; finishing = false; failing = false; mode = null; isPreview = false; gen++;
        try { const v = $id("video"); if (v && v.pause) v.pause(); } catch (e) { /* ignore */ }
        resolve();
      };
      if (!scene || !renderer || !plateGroup) {
        setTimeout(done, 120);
        return;
      }
      // plating beat (~600 ms): ghost lifts a plate, card scales up, canvas fades
      try {
        plateGroup.visible = true;
        plateGroup.position.set(-1.4, 1.15, 0.9);
        if (ghosts[0]) ghosts[0].position.x = -1.4;
      } catch (e) { /* beat is decorative */ }
      const cv = canvasEl();
      if (ov) ov.classList.add("kitchen-fading");
      const t0 = performance.now();
      const beat = setInterval(() => {
        const k = Math.min(1, (performance.now() - t0) / 600);
        try {
          if (plateGroup) {
            plateGroup.position.y = 1.15 + k * 0.45;
            plateGroup.position.x = -1.4 + k * 1.4;
            const card = plateGroup.userData.card;
            if (card) card.scale.setScalar(1 + k * 2.6);
          }
          if (cv) cv.style.opacity = String(1 - k);
        } catch (e) { /* ignore */ }
        if (k >= 1) { clearInterval(beat); done(); }
      }, 16);
    });
  }
  function fail(msg) {
    if (!active || finishing || failing) return;
    failing = true;
    clearKitchenTimers();
    captionMode = "stage";
    setAction("sad");
    const text = msg ? String(msg) : "Something went wrong in the kitchen";
    setCaption(text);
    setReadout((isPreview ? "preview \u00b7 " : "") + text, false);
    // sad-ghost beat: hold the message about 2 s, then hide and leave the untouched cut's frame in the player
    previewTimers.push(setTimeout(() => {
      if (!active || finishing) return;
      const ov = overlay(); if (ov) ov.hidden = true;
      disposeScene(); hideFallback();
      active = false; failing = false; mode = null; isPreview = false; gen++;
    }, 2000));
  }
  function stop() {
    if (!active) return;
    clearKitchenTimers();
    const ov = overlay(); if (ov) { ov.hidden = true; ov.classList.remove("kitchen-fading"); }
    disposeScene(); hideFallback();
    active = false; finishing = false; failing = false; mode = null; isPreview = false; gen++;
  }
  function preview() {
    if (active) stop();
    start({ mode: "preview" });
    const total = 23;
    update({ phase: "plan", done: 0, total, pct: null, eta_s: null });
    for (let i = 1; i <= total; i++) {
      const doneN = i;
      previewTimers.push(setTimeout(() => {
        update({ phase: "shots", done: doneN, total, pct: Math.round((100 * doneN) / total), eta_s: Math.round((total - doneN) * 0.9) });
      }, 3000 + doneN * 900));
    }
    previewTimers.push(setTimeout(() => {
      update({ phase: "join", done: total, total, pct: 99, eta_s: 0 });
    }, 3000 + total * 900 + 500));
    previewTimers.push(setTimeout(() => { finish(); }, 3000 + total * 900 + 1700));
  }
  document.addEventListener("visibilitychange", () => { lastFrame = 0; });
  window.Kitchen = { start, update, finish, fail, stop, preview };
  const maybePreview = () => { if (window.location.hash === "#edit/kitchen") preview(); };
  window.addEventListener("load", maybePreview);
  window.addEventListener("hashchange", maybePreview);
  if (document.readyState === "complete") maybePreview();
})();
