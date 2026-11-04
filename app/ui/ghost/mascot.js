// GhostCut Director — Ghost mascot (ONBOARD).
// Plain classic script: window.GhostMark(size, mood) -> inline SVG element.
// Moods idle|scan|cook|bing change the eyes and a CSS class only.
// Hand-drawn, no external images, no network.
(function () {
  var MOODS = { idle: 1, scan: 1, cook: 1, bing: 1 };
  var seq = 0;
  function eyesFor(mood) {
    if (mood === "scan") {
      return '<ellipse class="ghost-eye" cx="24" cy="29" rx="3.4" ry="3.1" fill="#16203a"/>'
        + '<ellipse class="ghost-eye" cx="40" cy="29" rx="3.4" ry="3.1" fill="#16203a"/>'
        + '<circle cx="25.2" cy="28" r="1" fill="#fff" opacity=".9"/>'
        + '<circle cx="41.2" cy="28" r="1" fill="#fff" opacity=".9"/>';
    }
    if (mood === "cook") {
      return '<path class="ghost-eye" d="M20.5 30 Q24 25.5 27.5 30" fill="none" stroke="#16203a" stroke-width="2.2" stroke-linecap="round"/>'
        + '<path class="ghost-eye" d="M36.5 30 Q40 25.5 43.5 30" fill="none" stroke="#16203a" stroke-width="2.2" stroke-linecap="round"/>';
    }
    if (mood === "bing") {
      return '<ellipse class="ghost-eye" cx="24" cy="28" rx="3.2" ry="5.6" fill="#16203a"/>'
        + '<ellipse class="ghost-eye" cx="40" cy="28" rx="3.2" ry="5.6" fill="#16203a"/>'
        + '<circle cx="25" cy="25.5" r="1.3" fill="#fff"/>'
        + '<circle cx="41" cy="25.5" r="1.3" fill="#fff"/>'
        + '<circle cx="32" cy="38" r="2.2" fill="#16203a" opacity=".85"/>';
    }
    return '<ellipse class="ghost-eye" cx="24" cy="28" rx="3" ry="4.8" fill="#16203a"/>'
      + '<ellipse class="ghost-eye" cx="40" cy="28" rx="3" ry="4.8" fill="#16203a"/>'
      + '<circle cx="25" cy="26" r="1" fill="#fff" opacity=".9"/>'
      + '<circle cx="41" cy="26" r="1" fill="#fff" opacity=".9"/>';
  }
  window.GhostMark = function GhostMark(size, mood) {
    var s = (typeof size === "number" && size > 0) ? size : 48;
    var m = MOODS[mood] ? mood : "idle";
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", String(s));
    svg.setAttribute("height", String(s));
    svg.setAttribute("viewBox", "0 0 64 64");
    svg.setAttribute("class", "ghost-mark mood-" + m);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-hidden", "true");
    seq += 1;
    var gid = "gg-" + m + "-" + seq;
    svg.innerHTML =
      '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">'
      + '<stop offset="0%" stop-color="#d6e2ff"/><stop offset="55%" stop-color="#9db8ff"/>'
      + '<stop offset="100%" stop-color="#7ea0f0"/></linearGradient></defs>'
      + '<path d="M32 5 C20 5 13 16 13 29 L13 57 L19.5 51 L26 57 L32 51 L38 57 L44.5 51 L51 57 L51 29 C51 16 44 5 32 5 Z" '
      + 'fill="url(#' + gid + ')" stroke="#c9d8ff" stroke-width="1"/>'
      + eyesFor(m)
      + '<path d="M45 11 L53 5" stroke="#ff6a1a" stroke-width="3.2" stroke-linecap="round"/>';
    return svg;
  };
})();
