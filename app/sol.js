/* BETTER CALL SOL — page behaviors.
   Ransom composer + PRNG lifted from arlan.me/vault/ransom-note; CRT treatment per
   arlan.me/vault/midjourney; typer engine in typer.js. All MIT → free to copy. */

(() => {
  "use strict";

  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── ransom note hero ─────────────────────────────────────────────── */

  // sprite manifest for the letters this page needs (from the vault's manifest.json;
  // real torn-magazine cutouts, Resource Boy pack, royalty-free)
  const RANSOM = {
    J: [[176,"J_1"],[144,"J_2"],[177,"J_3"],[291,"J_4"],[86,"J_5"],[163,"J_6"]],
    U: [[176,"U_1"],[251,"U_2"],[270,"U_3"],[196,"U_4"],[213,"U_5"],[243,"U_6"]],
    S: [[242,"S_1"],[155,"S_2"],[101,"S_3"],[112,"S_4"],[188,"S_5"],[244,"S_6"]],
    T: [[382,"T_1"],[150,"T_2"],[180,"T_3"],[127,"T_4"],[133,"T_5"],[171,"T_6"]],
    C: [[238,"C_1"],[215,"C_2"],[135,"C_3"],[195,"C_4"],[257,"C_5"],[183,"C_6"]],
    A: [[116,"A_1"],[196,"A_2"],[149,"A_3"],[81,"A_4"],[98,"A_5"],[222,"A_6"]],
    L: [[178,"L_1"],[142,"L_2"],[331,"L_3"],[201,"L_4"],[120,"L_5"],[108,"L_6"]],
    O: [[191,"O_1"],[222,"O_2"],[264,"O_3"],[210,"O_4"],[228,"O_5"],[158,"O_6"]],
  };

  // The shipped sprite subset predates the new name, so B, E, and R use original CSS
  // cut-paper variants instead of adding restricted source-pack files to the repository.
  const CUTOUT = [
    [170, "lowercase"],
    [198, "roundel"],
    [178, "neon"],
    [146, "label"],
    [206, "tabloid"],
    [184, "signal"],
  ];

  // mulberry32 + string hash — the vault's deterministic seed pair, verbatim, so a
  // given phrase + roll always reproduces the same note.
  function mulberry32(seed) {
    let a = seed >>> 0;
    return () => {
      a |= 0;
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function hashSeed(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  const PHRASE = "BETTER CALL SOL";
  // playground defaults from the vault entry: tilt 8°, bounce 0.06, scale mix 0.12
  const TILT = 8, BOUNCE = 0.06, SCALE_MIX = 0.12, OVERLAP = 0.1;

  let roll = 0;
  let fitRansom = () => {};

  function composeRansom() {
    const host = document.getElementById("ransom");
    if (!host) return;
    host.innerHTML = "";
    const rng = mulberry32(hashSeed(PHRASE) + roll);
    const lastVariant = {}; // avoid the same cutout twice in a row for a repeated letter
    const wordRatios = [];

    for (const word of PHRASE.split(" ")) {
      const w = document.createElement("span");
      w.className = "rword";
      let wordRatio = 0;
      [...word].forEach((ch, i) => {
        const sprites = RANSOM[ch];
        const variants = sprites || CUTOUT;
        let pick = Math.floor(rng() * variants.length);
        if (variants.length > 1 && pick === lastVariant[ch]) {
          pick = (pick + 1 + Math.floor(rng() * (variants.length - 1))) % variants.length;
        }
        lastVariant[ch] = pick;
        const [width, token] = variants[pick];
        const piece = sprites ? document.createElement("img") : document.createElement("span");
        piece.classList.add("rpiece");
        piece.style.setProperty("--rw", (width / 220).toFixed(3));
        if (sprites) {
          piece.src = `assets/ransom/${token}.webp`;
          piece.alt = "";
          piece.draggable = false;
        } else {
          piece.classList.add("ransom-glyph", `ransom-glyph--${token}`);
          piece.textContent = ch;
          piece.setAttribute("aria-hidden", "true");
        }
        const tilt = (rng() * 2 - 1) * TILT;
        const bounce = (rng() * 2 - 1) * BOUNCE;
        const scale = 1 + (rng() * 2 - 1) * SCALE_MIX;
        piece.style.transform = `rotate(${tilt.toFixed(1)}deg) translateY(${(bounce * 100).toFixed(1)}%) scale(${scale.toFixed(2)})`;
        if (i > 0) piece.style.marginLeft = `calc(var(--rh, 96px) * ${(-(rng() * OVERLAP)).toFixed(3)})`;
        piece.style.zIndex = String(1 + Math.floor(rng() * 8));
        piece.style.position = "relative";
        w.appendChild(piece);
        wordRatio += width / 220;
      });
      host.appendChild(w);
      wordRatios.push(wordRatio);
    }

    // Keep the longer brand name inside the note at every width and after every re-cut.
    const widestWord = Math.max(...wordRatios);
    fitRansom = () => {
      const target = Math.min(116, Math.max(42, window.innerWidth * 0.11));
      const fitted = (host.clientWidth * 0.86) / widestWord;
      host.style.setProperty("--rh", `${Math.max(26, Math.min(target, fitted)).toFixed(1)}px`);
    };
    fitRansom();
  }

  composeRansom();
  document.getElementById("reroll")?.addEventListener("click", () => {
    roll += 1;
    composeRansom();
  });
  window.addEventListener("resize", () => fitRansom(), { passive: true });

  /* ── the typer, on scroll ─────────────────────────────────────────── */

  const typedEls = [...document.querySelectorAll("[data-typer]")];
  if (window.Typer) {
    if (reduceMotion) {
      typedEls.forEach((el) => new Typer(el, { initVisible: true }));
    } else {
      const typers = new Map(
        typedEls.map((el) => [el, new Typer(el, { fps: 23, cycles: 4 })]),
      );
      const io = new IntersectionObserver(
        (entries) => {
          for (const e of entries) {
            if (e.isIntersecting) {
              typers.get(e.target)?.in();
              io.unobserve(e.target);
            }
          }
        },
        { threshold: 0.4 },
      );
      typedEls.forEach((el) => io.observe(el));
    }
  }

  /* ── the monitor: phosphor call sim under CRT glass ───────────────── */

  const PHOSPHOR = "#49f47e";
  const DIM = "#1d6b3c";
  const BG = "#060a07";
  const MONO = '"Courier Prime", "Courier New", ui-monospace, monospace';

  const PHASES = [
    ["DETECTED", 1100],
    ["QUEUED", 900],
    ["CALLING", 2000],
    ["CONNECTED", 800],
    ["DISCUSSING", 2600],
    ["DECIDED", 1100],
    ["RESUMED", 1300],
    ["COMPLETED", 1600],
  ];
  const TOTAL = PHASES.reduce((s, p) => s + p[1], 0);

  const canvas = document.getElementById("callsim");
  const stateLabel = document.getElementById("sim-state");
  const ctx = canvas ? canvas.getContext("2d") : null;

  let W = 0, H = 0;

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(devicePixelRatio || 1, 2);
    W = rect.width;
    H = rect.height;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  const easeOut = (t) => 1 - Math.pow(1 - t, 3);

  function phaseAt(ms) {
    let acc = 0;
    for (let i = 0; i < PHASES.length; i++) {
      acc += PHASES[i][1];
      if (ms < acc) return { i, t: 1 - (acc - ms) / PHASES[i][1] };
    }
    return { i: PHASES.length - 1, t: 1 };
  }

  function glowOn() { ctx.shadowColor = PHOSPHOR; ctx.shadowBlur = 7; }
  function glowOff() { ctx.shadowBlur = 0; }

  function line(x1, y1, x2, y2, color, width, dash) {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash || []);
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function label(text, x, y, color, size) {
    ctx.font = `${size || 10}px ${MONO}`;
    ctx.fillStyle = color;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(text, x, y);
  }

  function drawAgent(x, y, codeProgress, cursorOn) {
    const w = 66, h = 46;
    ctx.beginPath();
    ctx.roundRect(x - w / 2, y - h / 2, w, h, 3);
    glowOn();
    ctx.strokeStyle = PHOSPHOR;
    ctx.lineWidth = 1.4;
    ctx.stroke();
    glowOff();
    const widths = [36, 26, 42, 20, 30];
    const shown = Math.max(2, Math.floor(codeProgress * widths.length));
    for (let i = 0; i < shown && i < widths.length; i++) {
      line(x - w / 2 + 8, y - h / 2 + 10 + i * 6.5,
           x - w / 2 + 8 + widths[i] * Math.min(1, codeProgress * widths.length - i),
           y - h / 2 + 10 + i * 6.5, DIM, 2);
    }
    if (cursorOn) {
      const cy = y - h / 2 + 8 + Math.min(shown, widths.length) * 6.5;
      if (cy < y + h / 2 - 6) {
        glowOn();
        ctx.fillStyle = PHOSPHOR;
        ctx.fillRect(x - w / 2 + 8, cy - 1, 5, 8);
        glowOff();
      }
    }
  }

  function drawDaemon(x, y, active) {
    const w = 42, h = 32;
    ctx.beginPath();
    ctx.roundRect(x - w / 2, y - h / 2 - 6, w, h, 3);
    glowOn();
    ctx.strokeStyle = active ? PHOSPHOR : DIM;
    ctx.lineWidth = 1.4;
    ctx.stroke();
    glowOff();
    ctx.beginPath();
    ctx.strokeStyle = active ? PHOSPHOR : DIM;
    ctx.lineWidth = 1.2;
    ctx.moveTo(x - 13, y - 6);
    ctx.lineTo(x - 5, y - 6);
    ctx.lineTo(x - 2, y - 13);
    ctx.lineTo(x + 2, y - 1);
    ctx.lineTo(x + 5, y - 6);
    ctx.lineTo(x + 13, y - 6);
    if (active) glowOn();
    ctx.stroke();
    glowOff();
    const dy = y + h / 2 + 3;
    ctx.beginPath();
    ctx.ellipse(x, dy, 9, 3, 0, 0, 7);
    ctx.moveTo(x - 9, dy); ctx.lineTo(x - 9, dy + 6);
    ctx.moveTo(x + 9, dy); ctx.lineTo(x + 9, dy + 6);
    ctx.moveTo(x - 9, dy + 6);
    ctx.ellipse(x, dy + 6, 9, 3, 0, 0, Math.PI);
    ctx.strokeStyle = DIM;
    ctx.lineWidth = 1.1;
    ctx.stroke();
  }

  function drawSol(x, y, opts) {
    ctx.beginPath();
    ctx.arc(x, y, 21, 0, 7);
    glowOn();
    ctx.strokeStyle = PHOSPHOR;
    ctx.lineWidth = opts.connected ? 1.8 : 1.4;
    ctx.stroke();
    glowOff();
    ctx.save();
    ctx.translate(x, y + 2);
    ctx.rotate(opts.wiggle || 0);
    ctx.beginPath();
    ctx.arc(0, 3, 9, Math.PI * 1.15, Math.PI * 1.85);
    ctx.strokeStyle = PHOSPHOR;
    ctx.lineWidth = 4.5;
    ctx.lineCap = "round";
    if (opts.connected) glowOn();
    ctx.stroke();
    glowOff();
    ctx.restore();
    ctx.lineCap = "butt";
    if (opts.ring !== undefined) {
      for (let k = 0; k < 2; k++) {
        const p = (opts.ring + k * 0.5) % 1;
        ctx.beginPath();
        ctx.arc(x, y, 21 + p * 22, 0, 7);
        ctx.strokeStyle = `rgba(73, 244, 126, ${(1 - p) * 0.5})`;
        ctx.lineWidth = 1;
        ctx.stroke();
      }
    }
  }

  function drawPacket(x1, x2, y, t) {
    const x = x1 + (x2 - x1) * easeOut(t);
    glowOn();
    ctx.beginPath();
    ctx.arc(x, y, 3.4, 0, 7);
    ctx.fillStyle = PHOSPHOR;
    ctx.fill();
    glowOff();
  }

  function drawWave(x1, x2, y, ms) {
    ctx.beginPath();
    const n = 70;
    for (let i = 0; i <= n; i++) {
      const x = x1 + ((x2 - x1) * i) / n;
      const env = Math.sin((i / n) * Math.PI);
      const a = Math.sin(i * 0.55 + ms * 0.014) * Math.sin(ms * 0.0042 + i * 0.09);
      const yy = y + a * env * 8;
      i === 0 ? ctx.moveTo(x, yy) : ctx.lineTo(x, yy);
    }
    glowOn();
    ctx.strokeStyle = PHOSPHOR;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    glowOff();
  }

  function chip(text, x, y, pop) {
    const s = 0.85 + 0.15 * easeOut(Math.min(1, pop));
    ctx.font = `11px ${MONO}`;
    const half = (ctx.measureText(text).width + 16) / 2;
    x = Math.min(Math.max(x, half + 8), W - half - 8);
    ctx.save();
    ctx.translate(x, y);
    ctx.scale(s, s);
    ctx.beginPath();
    ctx.roundRect(-half, -11, half * 2, 21, 2);
    ctx.fillStyle = BG;
    ctx.fill();
    glowOn();
    ctx.strokeStyle = PHOSPHOR;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = PHOSPHOR;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, 0, 1);
    glowOff();
    ctx.restore();
  }

  function drawRibbon(idx) {
    const y = H - 24;
    ctx.font = `10px ${MONO}`;
    ctx.textBaseline = "top";
    const gap = 16;
    let total = 0;
    const widths = PHASES.map(([name]) => {
      const w = ctx.measureText(name).width;
      total += w;
      return w;
    });
    total += gap * (PHASES.length - 1);
    if (total > W - 28) {
      const name = PHASES[idx][0];
      glowOn();
      ctx.textAlign = "center";
      ctx.fillStyle = PHOSPHOR;
      ctx.fillText(name, W / 2, y);
      glowOff();
      ctx.fillStyle = DIM;
      ctx.textAlign = "right";
      ctx.fillText(`${idx + 1}/${PHASES.length}`, W - 14, y);
      return;
    }
    let x = (W - total) / 2;
    PHASES.forEach(([name], i) => {
      ctx.textAlign = "left";
      if (i === idx) {
        glowOn();
        ctx.fillStyle = PHOSPHOR;
        ctx.fillText(name, x, y);
        line(x, y + 13, x + widths[i], y + 13, PHOSPHOR, 1.25);
        glowOff();
      } else {
        ctx.fillStyle = i < idx ? DIM : "#123a24";
        ctx.fillText(name, x, y);
      }
      x += widths[i] + gap;
    });
  }

  function fmtTimer(ms) {
    const s = Math.floor(ms / 1000);
    return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }

  let start = performance.now();

  function draw(now) {
    const elapsed = (now - start) % TOTAL;
    const { i, t } = reduceMotion ? { i: 4, t: 0.5 } : phaseAt(elapsed);
    const name = PHASES[i][0];
    const ms = reduceMotion ? 1200 : elapsed;

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = BG;
    ctx.fillRect(0, 0, W, H);

    const y = H * 0.46;
    const ax = W * 0.17, dx = W * 0.5, ox = W * 0.83;
    const aEdge = ax + 35, dL = dx - 23, dR = dx + 23, oEdge = ox - 23;

    line(aEdge, y, dL, y, "#123a24", 1);
    line(dR, y, oEdge, y, "#123a24", 1);

    const connected = i >= 3 && i <= 6;
    const talking = name === "DISCUSSING";

    if (name === "CALLING") {
      ctx.save();
      ctx.lineDashOffset = -ms * 0.03;
      line(dR, y, oEdge, y, DIM, 1.3, [4, 6]);
      ctx.restore();
    } else if (connected && !talking) {
      glowOn();
      line(dR, y, oEdge, y, PHOSPHOR, 1.2);
      glowOff();
    }

    const codeProgress = name === "RESUMED" ? 0.4 + t * 0.6 : i >= 7 ? 1 : 0.4;
    const cursorOn = (i <= 1 || i >= 6) && Math.floor(ms / 450) % 2 === 0;
    drawAgent(ax, y, codeProgress, cursorOn);
    drawDaemon(dx, y, i >= 1);
    drawSol(ox, y, {
      connected,
      ring: name === "CALLING" ? (ms % 900) / 900 : undefined,
      wiggle: name === "CALLING" ? Math.sin(ms * 0.05) * 0.06 : 0,
    });

    label("CODEX", ax, y + 34, DIM);
    label("DAEMON", dx, y + 40, DIM);
    label("SOL", ox, y + 34, DIM);

    if (name === "DETECTED") chip("! BLOCKED", ax, y - 44, t * 3);
    if (name === "QUEUED") drawPacket(aEdge, dL, y, t);
    if (name === "DISCUSSING") drawWave(dR + 4, oEdge - 4, y, ms);
    if (name === "DECIDED") chip("DECISION + PIN OK", ox, y - 44, t * 3);
    if (name === "RESUMED") drawPacket(dL, aEdge, y, t);
    if (name === "COMPLETED") chip("RESUMED", ax, y - 44, t * 3);

    if (connected) {
      const callStart = PHASES[0][1] + PHASES[1][1] + PHASES[2][1];
      ctx.font = `11px ${MONO}`;
      ctx.textAlign = "right";
      ctx.textBaseline = "top";
      glowOn();
      ctx.fillStyle = PHOSPHOR;
      ctx.fillText(`REC ● ${fmtTimer(ms - callStart)}`, W - 14, 12);
      glowOff();
    }

    ctx.font = `11px ${MONO}`;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillStyle = DIM;
    ctx.fillText("OUTBOUND · LINE 1", 14, 12);

    // the sweeping sync band, a slow CRT artifact
    if (!reduceMotion) {
      const sweep = ((now * 0.05) % (H * 2.6)) - H * 0.3;
      const g = ctx.createLinearGradient(0, sweep - 30, 0, sweep + 30);
      g.addColorStop(0, "rgba(73,244,126,0)");
      g.addColorStop(0.5, "rgba(73,244,126,0.05)");
      g.addColorStop(1, "rgba(73,244,126,0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, sweep - 30, W, 60);
    }

    drawRibbon(i);

    if (stateLabel) stateLabel.textContent = `LINE 1 · ${name}`;
  }

  function frame(now) {
    draw(now);
    if (!reduceMotion) requestAnimationFrame(frame);
  }

  if (canvas && ctx) {
    resize();
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => { resize(); draw(performance.now()); }).observe(canvas);
    } else {
      addEventListener("resize", () => { resize(); draw(performance.now()); });
    }
    requestAnimationFrame(frame);
  }

  /* ── chroma glow lean (footer) ────────────────────────────────────── */

  const glow = document.getElementById("glow");
  if (glow && !reduceMotion) {
    const tail = document.querySelector(".tail");
    tail.addEventListener("mousemove", (e) => {
      const r = glow.getBoundingClientRect();
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      glow.style.setProperty("--lean-x", String(Math.max(-1, Math.min(1, (e.clientX - cx) / (r.width / 2)))));
      glow.style.setProperty("--lean-y", String(Math.max(-1, Math.min(1, (e.clientY - cy) / 120))));
    });
    tail.addEventListener("mouseleave", () => {
      glow.style.setProperty("--lean-x", "0");
      glow.style.setProperty("--lean-y", "0");
    });
  }

  /* ── copy buttons ─────────────────────────────────────────────────── */

  document.querySelectorAll(".copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = btn.dataset.copy || "";
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        const ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      btn.classList.add("copied");
      clearTimeout(btn._t);
      btn._t = setTimeout(() => btn.classList.remove("copied"), 1400);
    });
  });
})();
