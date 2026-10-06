/* AutoClip Editor UI ------------------------------------------------------- */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let boot = null;           // /api/bootstrap payload
let S = {};                // live settings
let source = null;         // loaded source record
let target = "cam";        // which crop box is being edited
let frameT = 0;            // timestamp shown in the editor
let sourceJob = null;
let renderJob = null;
let pollTimer = null;
let previewTimer = null;
let previewSeq = 0;
let previewUrl = null;
let manual = [];

/* ---------- helpers ---------------------------------------------------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
  });
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res;
}

const post = (path, body) =>
  api(path, { method: "POST", body: JSON.stringify(body || {}) });

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .3s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 320);
  }, kind === "bad" ? 7000 : 3800);
}

function hms(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
           : `${m}:${String(s).padStart(2, "0")}`;
}

function hmsFull(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function parseTime(text) {
  const t = String(text || "").trim();
  if (!t) return 0;
  const parts = t.split(":").map((p) => parseFloat(p) || 0);
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0];
}

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}
function setPath(obj, path, value) {
  const keys = path.split(".");
  let o = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    if (typeof o[keys[i]] !== "object" || o[keys[i]] === null) o[keys[i]] = {};
    o = o[keys[i]];
  }
  o[keys[keys.length - 1]] = value;
}

/* ---------- settings binding ------------------------------------------ */

const FORMAT = {
  top_ratio: (v) => Math.round(v * 100) + "%",
  cap_pos: (v) => Math.round(v * 100) + "%",
  game_focus_x: (v) => (v < 0.34 ? "left" : v > 0.66 ? "right" : "centre"),
  game_focus_y: (v) => (v < 0.34 ? "top" : v > 0.66 ? "bottom" : "middle"),
  game_zoom: (v) => Number(v).toFixed(2) + "×",
  cam_scale: (v) => Math.round(v * 100) + "%",
  cam_offset_x: (v) => `${v > 0 ? "+" : ""}${v} px`,
  cam_offset_y: (v) => `${v > 0 ? "+" : ""}${v} px`,
  top_blur: (v) => (Number(v) ? v : "off"),
  top_dim: (v) => Math.round(v * 100) + "%",
  divider: (v) => (Number(v) ? v + " px" : "none"),
  clip_count: (v) => `${v} clip${v == 1 ? "" : "s"}`,
  target_len: (v) => v + "s",
  clip_min: (v) => v + "s",
  clip_max: (v) => v + "s",
  sensitivity: (v) => (v < 0.3 ? "picky" : v > 0.66 ? "generous" : "balanced"),
  lead_in: (v) => Number(v).toFixed(1) + "s",
  censor_tone: (v) => v + " Hz",
  reel_moment_len: (v) => v + "s",
  censor_gain: (v) => Math.round(v * 100) + "%",
  censor_pad: (v) => Math.round(v * 1000) + " ms",
  cap_size: (v) => v + " px",
  cap_outline: (v) => v + " px",
  cap_shadow: (v) => v + " px",
  cap_max_words: (v) => `${v} word${v == 1 ? "" : "s"}`,
  quality: (v) => (v <= 17 ? "very high" : v <= 21 ? "high" : v <= 25 ? "medium" : "small file"),
};

const VALUE_EL = {
  top_ratio: "#v-top-ratio", game_focus_x: "#v-focus-x", game_focus_y: "#v-focus-y",
  game_zoom: "#v-game-zoom", cam_scale: "#v-cam-scale", cam_offset_x: "#v-cam-ox",
  cam_offset_y: "#v-cam-oy", top_blur: "#v-top-blur", top_dim: "#v-top-dim",
  divider: "#v-divider", clip_count: "#v-clip-count", target_len: "#v-target-len",
  sensitivity: "#v-sensitivity", clip_min: "#v-clip-min",
  clip_max: "#v-clip-max", lead_in: "#v-lead-in",
  reel_moment_len: "#v-reel-moment-len", censor_tone: "#v-censor-tone", censor_gain: "#v-censor-gain", censor_pad: "#v-censor-pad",
  cap_size: "#v-cap-size", cap_pos: "#v-cap-pos", cap_outline: "#v-cap-outline",
  cap_shadow: "#v-cap-shadow", cap_max_words: "#v-cap-words", quality: "#v-quality",
};

function showValue(key) {
  const sel = VALUE_EL[key];
  if (!sel) return;
  const el = $(sel);
  if (!el) return;
  const v = getPath(S, key);
  el.textContent = FORMAT[key] ? FORMAT[key](v) : v;
}

function paintRange(input) {
  const min = parseFloat(input.min || 0), max = parseFloat(input.max || 100);
  const pct = max > min ? ((parseFloat(input.value) - min) / (max - min)) * 100 : 0;
  input.style.setProperty("--pct", pct + "%");
}

function syncUI() {
  $$("[data-setting]").forEach((el) => {
    const key = el.dataset.setting;
    const v = getPath(S, key);
    if (el.classList.contains("seg") || el.classList.contains("modes")) {
      $$("button", el).forEach((b) => b.classList.toggle("on", String(b.dataset.value) === String(v)));
    } else if (el.type === "checkbox") {
      el.checked = !!v;
    } else if (v !== undefined && v !== null) {
      el.value = v;
      if (el.type === "range") paintRange(el);
    }
  });
  Object.keys(VALUE_EL).forEach(showValue);
  const sizeSeg = $("#seg-size");
  if (sizeSeg) {
    const cur = `${S.out_w}x${S.out_h}`;
    $$("button", sizeSeg).forEach((b) => b.classList.toggle("on", b.dataset.value === cur));
  }
  $("#cap-style-hint").textContent = {
    karaoke: "Whole phrase visible, spoken word coloured",
    pop: "Same, plus the spoken word scales up",
    oneword: "One big word at a time",
    plain: "Static phrases, no highlight",
  }[S.cap_style] || "";
  const sens = Number(S.sensitivity);
  $("#sens-hint").textContent = sens < 0.3
    ? "Only stand-out moments — may return fewer than asked"
    : sens > 0.66 ? "Fills the quota even from a quiet stream"
    : "Fills the quota, best moments first";
  syncRange();
  applyMode();
  updateEstimate();
}

function onSettingChanged(key, { preview = true } = {}) {
  if (key === "mode" || key === "censor" || key === "censor_mild" || key === "clean_model") {
    applyMode();
  }
  if (key.startsWith("censor")) scheduleCensorTest();
  showValue(key);
  if (key.startsWith("cam.") || key.startsWith("game.")) placeBox();
  if (preview) schedulePreview();
  updateEstimate();
}

function bindSettings() {
  $$("[data-setting]").forEach((el) => {
    const key = el.dataset.setting;
    if (el.classList.contains("seg") || el.classList.contains("modes")) {
      el.addEventListener("click", (e) => {
        const btn = e.target.closest("button");
        if (!btn) return;
        let v = btn.dataset.value;
        if (v === "true" || v === "false") v = v === "true";
        setPath(S, key, v);
        $$("button", el).forEach((b) => b.classList.toggle("on", b === btn));
        onSettingChanged(key);
      });
      return;
    }
    const handler = () => {
      let v;
      if (el.type === "checkbox") v = el.checked;
      else if (el.type === "range" || el.type === "number") {
        v = parseFloat(el.value);
        if (Number.isNaN(v)) return;
        if (el.type === "range") paintRange(el);
      } else v = el.value;
      setPath(S, key, v);
      onSettingChanged(key);
    };
    el.addEventListener("input", handler);
    el.addEventListener("change", handler);
  });
}

/* ---------- which part of the video to use ------------------------------ */

function rangeSpan() {
  const dur = source ? source.duration : 0;
  const a = Math.max(0, Math.min(S.range_start || 0, dur));
  const b = (S.range_end && S.range_end > a) ? Math.min(S.range_end, dur) : dur;
  return { a, b, dur };
}

function syncRange() {
  const { a, b, dur } = rangeSpan();
  const pa = dur ? (a / dur) * 100 : 0;
  const pb = dur ? (b / dur) * 100 : 100;
  $("#rp-fill").style.left = pa + "%";
  $("#rp-fill").style.width = Math.max(0, pb - pa) + "%";
  $("#rp-a").style.left = pa + "%";
  $("#rp-b").style.left = pb + "%";
  $("#rp-a").dataset.time = hms(a);
  $("#rp-b").dataset.time = S.range_end ? hms(b) : "end";
  $("#rp-t1").textContent = dur ? hms(dur) : "";
  $("#v-range").textContent = (!a && !S.range_end)
    ? "whole video"
    : `${hms(a)} – ${hms(b)}  ·  ${hms(b - a)} of footage`;
  if (document.activeElement !== $("#range-from")) {
    $("#range-from").value = a ? hmsFull(a) : "";
  }
  if (document.activeElement !== $("#range-to")) {
    $("#range-to").value = S.range_end ? hmsFull(b) : "";
  }
  updateEstimate();
}

function setRange(a, b) {
  const dur = source ? source.duration : 0;
  const gap = Math.max(5, Number(S.target_len) || 30);
  a = Math.max(0, Math.min(a, Math.max(0, dur - gap)));
  if (b === null || b === undefined) b = dur;
  b = Math.max(a + gap, Math.min(b, dur));
  S.range_start = Math.round(a);
  // Landing on (or clamped to) the end means "run to the end", not a fixed time.
  S.range_end = b >= dur - 1 ? 0 : Math.round(b);
  syncRange();
}

function initRangePicker() {
  const track = $("#rp-track");
  let handle = null;

  const fracFrom = (e) => {
    const r = track.getBoundingClientRect();
    return Math.max(0, Math.min(1, (e.clientX - r.left) / Math.max(r.width, 1)));
  };
  const apply = (frac) => {
    const { a, b, dur } = rangeSpan();
    if (!dur) return;
    const t = frac * dur;
    if (handle === "a") setRange(t, S.range_end || dur);
    else setRange(a, t);
  };
  const down = (e) => {
    if (!source) return;
    const knob = e.target.closest(".rp-knob");
    if (knob) {
      handle = knob.dataset.h;
    } else {
      const { a, b, dur } = rangeSpan();
      const t = fracFrom(e) * dur;
      handle = Math.abs(t - a) <= Math.abs(t - b) ? "a" : "b";
      apply(fracFrom(e));
    }
    $("#rp-" + handle).classList.add("dragging");
    e.preventDefault();
  };
  const move = (e) => { if (handle) apply(fracFrom(e)); };
  const up = () => {
    if (!handle) return;
    $("#rp-" + handle).classList.remove("dragging");
    handle = null;
  };

  track.addEventListener("pointerdown", down);
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);

  const commit = () => {
    const dur = source ? source.duration : 0;
    const from = $("#range-from").value.trim();
    const to = $("#range-to").value.trim();
    setRange(from ? parseTime(from) : 0, to ? parseTime(to) : dur);
  };
  $("#range-from").addEventListener("change", commit);
  $("#range-to").addEventListener("change", commit);
  $("#btn-range-all").onclick = () => setRange(0, null);
  $("#btn-range-here").onclick = () => {
    if (source) setRange(frameT, S.range_end || source.duration);
  };
}

/* ---------- mode: clips or a cleaned-up full stream --------------------- */

// Measured on this PC with continuous talking; dead air is skipped, so real
// streams usually come in under these.
const SPEECH_SPEED = { tiny: 10, base: 6, small: 2.5, medium: 0.9, "large-v3": 0.45 };

function applyMode() {
  const mode = S.mode || "clips";
  const clean = mode === "clean", reelMode = mode === "reel";
  $$(".clips-only").forEach((el) => el.classList.toggle("hidden", mode !== "clips"));
  $$(".reel-only").forEach((el) => el.classList.toggle("hidden", !reelMode));
  $("#clean-model-field").classList.toggle("hidden", mode === "clips");
  $("#clean-model-field label").textContent = reelMode
    ? "Speech model for quotes and bleeping" : "Speech model for full streams";
  $("#clips-title").textContent = clean ? "Which part to keep"
    : reelMode ? "Where to look" : "Which moments to grab";
  $("#clips-sub").textContent = clean
    ? "Leave it as the whole video, or trim off the start and end"
    : reelMode ? "Moments are only suggested from this stretch"
    : "Loud reactions, hype in chat, and lively talking score highest";
  $("#range-label").firstChild.textContent = clean ? "Part of the stream to save "
    : reelMode ? "Part of the stream to search " : "Which part of the video to use ";
  $("#range-hint").textContent = clean
    ? "Cuts land on the nearest keyframe, so they can be a second or two early"
    : reelMode ? "Skip the intro and outro so they are not suggested"
    : "Clips are only taken from this stretch";
  $("#go").textContent = reelMode ? "Build the highlight reel"
    : !clean ? "Make my clips" : (S.censor ? "Make the clean video" : "Save the video");
  $("#go").disabled = !source || (reelMode && !picked().length) || !!renderJob;
  $("#mild-hint").textContent = S.censor_mild
    ? "Also damn, hell, crap, ass… — usually fine on YouTube"
    : "The words that actually cost you ads";
  const sel = $("#clean-model-select");
  if (sel && sel.value !== S.clean_model) sel.value = S.clean_model;
  const speed = SPEECH_SPEED[S.clean_model] || 2;
  $("#clean-model-hint").textContent = reelMode
    ? "Writes down each moment so you can read what you said"
    : `About ${Math.round(120 / speed)} min per 2 hours of talking`;
  if (reelMode) updateSelection();
  updateEstimate();
}

let censorTimer = null;
function scheduleCensorTest() {
  clearTimeout(censorTimer);
  censorTimer = setTimeout(runCensorTest, 250);
}

async function runCensorTest() {
  const box = $("#censor-out");
  const text = $("#censor-test").value.trim();
  if (!text) { box.innerHTML = "&nbsp;"; return; }
  try {
    const r = await post("/api/censor-test", { text, settings: S });
    // Mark exactly the words the server starred out.
    const before = text.split(/\s+/), after = r.result.split(/\s+/);
    box.innerHTML = after.map((w, i) => (w !== before[i]
      ? `<mark>${escapeHtml(w)}</mark>` : escapeHtml(w))).join(" ")
      + `<span style="float:right;font-size:11.5px">${r.hits
        ? `${r.hits} bleeped` : "nothing bleeped"}</span>`;
  } catch (_) { /* typing faster than the server; ignore */ }
}

/* ---------- highlight reel: pick the moments ------------------------------- */

let moments = [];          // candidates: {id,start,end,peak,score,picked,thumb,line}
let momentsJob = null;
let directPlay = true;
let stopAt = null;

const picked = () => moments.filter((m) => m.picked).sort((a, b) => a.start - b.start);

async function findMoments() {
  if (!source) return;
  $("#btn-find-moments").disabled = true;
  $("#moments-bar-wrap").style.display = "";
  $("#moments-status").textContent = "Starting…";
  try {
    const { job } = await post("/api/moments", { source_id: source.id, settings: S });
    momentsJob = job.id;
    pollMoments();
  } catch (err) {
    toast(err.message, "bad");
    $("#btn-find-moments").disabled = false;
  }
}

async function pollMoments() {
  if (!momentsJob) return;
  let job;
  try { ({ job } = await api("/api/job/" + momentsJob)); } catch (_) { setTimeout(pollMoments, 900); return; }
  $("#moments-bar").style.width = Math.round(job.progress * 100) + "%";
  $("#moments-status").textContent = job.message || "Working…";
  const cands = (job.result && job.result.candidates) || null;
  if (cands) {
    if (job.result.direct !== undefined) directPlay = job.result.direct;
    mergeCandidates(cands);
  }
  if (["done", "error", "cancelled"].includes(job.status)) {
    momentsJob = null;
    $("#btn-find-moments").disabled = false;
    $("#btn-find-moments").textContent = "Find again";
    $("#moments-bar-wrap").style.display = "none";
    if (job.status === "error") toast(job.error, "bad");
    $("#moments-status").textContent = job.status === "done"
      ? `${moments.length} moments found — tick the ones you want`
      : job.status === "cancelled" ? "Stopped — the quotes found so far are kept" : job.error;
    return;
  }
  setTimeout(pollMoments, 900);
}

function mergeCandidates(cands) {
  // Keep the user's ticks and trims; only take new info (thumbs, quotes) from the server.
  const known = new Map(moments.map((m) => [m.key, m]));
  const fresh = cands.map((c) => {
    const key = `${c.id}:${c.peak}`;
    const old = known.get(key);
    if (old) return Object.assign(old, { thumb: c.thumb || old.thumb,
                                           line: c.line !== null ? c.line : old.line });
    return { ...c, key, origStart: c.start, origEnd: c.end };
  });
  const first = !moments.length;
  moments = fresh;
  if (first) {
    $("#moments-area").style.display = "";
    loadPlayerSource();
  }
  renderMoments();
}

function loadPlayerSource() {
  const v = $("#moment-player");
  if (directPlay && source) {
    v.src = `/api/source-video/${source.id}`;
    $("#moment-player-cap").textContent =
      "Click a picture to watch a moment — or scrub anywhere and use “Add the frame I’m on”";
  }
}

function playMoment(m) {
  const v = $("#moment-player");
  $$(".moment.playing").forEach((el) => el.classList.remove("playing"));
  const row = document.querySelector(`.moment[data-key="${CSS.escape(m.key)}"]`);
  if (row) row.classList.add("playing");
  $("#moment-player-cap").textContent =
    `${hms(m.start)} – ${hms(m.end)}  ·  ${m.line || "…"}`;
  if (directPlay) {
    stopAt = m.end;
    const go = () => { v.currentTime = m.start; v.play().catch(() => {}); };
    if (v.readyState >= 1) go();
    else {
      // preload="none" means nothing is fetched until asked; ask now.
      v.addEventListener("loadedmetadata", go, { once: true });
      v.preload = "metadata";
      v.load();
    }
  } else {
    stopAt = null;
    v.src = `/api/snippet/${source.id}?a=${m.start.toFixed(2)}&b=${m.end.toFixed(2)}`;
    v.play().catch(() => {});
  }
  v.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderMoments() {
  const list = $("#moments-list");
  const best = new Set([...moments].sort((a, b) => b.score - a.score)
    .slice(0, Math.max(1, Math.ceil(moments.length / 4))).map((m) => m.key));
  list.innerHTML = "";
  [...moments].sort((a, b) => a.start - b.start).forEach((m) => {
    const row = document.createElement("div");
    row.className = "moment " + (m.picked ? "on" : "off");
    row.dataset.key = m.key;
    const thumb = m.thumb ? `/api/file?path=${encodeURIComponent(m.thumb)}` : "";
    const quote = m.line === null ? `<span class="quote wait">listening…</span>`
      : m.line ? `<span class="quote" title="${escapeAttr(m.line)}">“${escapeHtml(m.line)}”</span>`
      : `<span class="quote wait">nothing clear was said</span>`;
    row.innerHTML = `
      <label class="check"><input type="checkbox" ${m.picked ? "checked" : ""}>
        <span class="box"><svg viewBox="0 0 12 12" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"><path d="M2 6.2l2.6 2.6L10 3.4"/></svg></span></label>
      <div class="thumb">${thumb ? `<img src="${thumb}" alt="">` : ""}</div>
      <div class="meta">
        ${quote}
        <span class="when">${hms(m.start)} – ${hms(m.end)} · ${Math.round(m.end - m.start)}s
          ${best.has(m.key) ? '<span class="best">TOP PICK</span>' : ""}</span>
      </div>
      <div class="trim">
        <div class="row"><span class="lbl">start</span>
          <button class="btn sm ghost" data-t="s-">−2s</button><button class="btn sm ghost" data-t="s+">+2s</button></div>
        <div class="row"><span class="lbl">end</span>
          <button class="btn sm ghost" data-t="e-">−2s</button><button class="btn sm ghost" data-t="e+">+2s</button></div>
      </div>`;
    $("input", row).onchange = (e) => {
      m.picked = e.target.checked;
      row.className = "moment " + (m.picked ? "on" : "off");
      updateSelection();
    };
    $(".thumb", row).onclick = () => playMoment(m);
    $$("[data-t]", row).forEach((b) => b.onclick = () => {
      const dur = source.duration, k = b.dataset.t;
      if (k === "s-") m.start = Math.max(0, m.start - 2);
      if (k === "s+") m.start = Math.min(m.end - 3, m.start + 2);
      if (k === "e-") m.end = Math.max(m.start + 3, m.end - 2);
      if (k === "e+") m.end = Math.min(dur, m.end + 2);
      $(".when", row).firstChild.textContent =
        `${hms(m.start)} – ${hms(m.end)} · ${Math.round(m.end - m.start)}s `;
      updateSelection();
      playMoment(m);
    });
    list.appendChild(row);
  });
  updateSelection();
}

function updateSelection() {
  const sel = picked();
  const total = sel.reduce((s, m) => s + (m.end - m.start), 0);
  const lo = S.reel_min * 60, hi = S.reel_max * 60, scale = hi * 1.2 || 1;
  $("#sel-total").textContent = hms(total);
  $("#sel-count").textContent = `· ${sel.length} moment${sel.length === 1 ? "" : "s"}`;
  $("#sel-target").textContent = ` (aiming for ${S.reel_min}–${S.reel_max} min)`;
  const fill = $("#sel-fill");
  fill.style.width = Math.min(100, (total / scale) * 100) + "%";
  fill.className = total > hi ? "over" : total >= lo ? "ok" : "";
  const band = $("#sel-band");
  band.style.left = (lo / scale) * 100 + "%";
  band.style.width = ((hi - lo) / scale) * 100 + "%";
  $("#go").disabled = !source || !sel.length || !!renderJob;
  updateEstimate();
}

function pickBest() {
  const goal = (S.reel_min + S.reel_max) / 2 * 60, cap = S.reel_max * 60;
  moments.forEach((m) => { m.picked = false; });
  let total = 0;
  for (const m of [...moments].sort((a, b) => b.score - a.score)) {
    const d = m.end - m.start;
    if (total + d > cap) continue;
    m.picked = true;
    total += d;
    if (total >= goal) break;
  }
  renderMoments();
}

function addMomentHere() {
  const v = $("#moment-player");
  if (!source) return;
  let at = directPlay ? v.currentTime : null;
  if (!directPlay) {
    const playing = moments.find((m) => document.querySelector(
      `.moment.playing[data-key="${CSS.escape(m.key)}"]`));
    at = playing ? playing.start + v.currentTime : null;
  }
  if (at === null || Number.isNaN(at)) { toast("Play or scrub the video to the spot first."); return; }
  const len = S.reel_moment_len;
  const start = Math.max(0, at - len * 0.4), end = Math.min(source.duration, start + len);
  const m = { id: -Date.now(), key: `hand:${Date.now()}`, start, end, peak: at, score: 0,
              tier: "hand", picked: true, thumb: "", line: "", origStart: start, origEnd: end };
  moments.push(m);
  renderMoments();
  toast(`Added the moment at ${hms(at)}.`, "good");
}

/* ---------- titles panel ----------------------------------------------------- */

let titlesJobId = null;

function copyText(text, btn) {
  const done = () => {
    if (!btn) return;
    const old = btn.textContent;
    btn.textContent = "Copied"; btn.classList.add("copied");
    setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1300);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else fallbackCopy(text, done);
}
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch (_) { toast("Could not copy.", "bad"); }
  ta.remove();
}

function fillList(ul, items, emptyText) {
  ul.innerHTML = "";
  if (!items.length) {
    ul.innerHTML = `<li class="empty">${escapeHtml(emptyText)}</li>`;
    return;
  }
  items.forEach((text) => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${escapeHtml(text)}</span><button class="btn sm">Copy</button>`;
    $("button", li).onclick = (e) => copyText(text, e.currentTarget);
    ul.appendChild(li);
  });
}

function renderTitles(jobId, info) {
  titlesJobId = jobId;
  $("#titles-panel").style.display = "";
  $("#t-game").value = info.game || "";
  $("#t-tags").value = info.hashtags !== undefined ? info.hashtags : (S.hashtags || "");
  fillList($("#t-quotes"), info.quotes || [],
           "Nothing quotable was said — try the AI ideas, or turn on bleeping/captions so it listens");
  $("#t-desc").value = info.description || "";
  if (info.ai_available) requestAiIdeas();
  else {
    $("#t-ai-note").textContent = "";
    fillList($("#t-ai"), [], "Optional: run  py -3 run.py --get-ai-model  once (1 GB) to turn these on");
    $("#btn-t-ai").style.display = "none";
  }
}

let aiBusy = false;
async function requestAiIdeas() {
  if (aiBusy || !titlesJobId) return;
  aiBusy = true;
  $("#btn-t-ai").disabled = true;
  $("#t-ai-note").textContent = "thinking… (~15 s)";
  $("#t-ai").innerHTML = `<li class="empty">The local AI is writing ideas…</li>`;
  try {
    const r = await post("/api/titles/ai", { job_id: titlesJobId, game: $("#t-game").value,
                                             settings: S });
    fillList($("#t-ai"), r.ideas || [], r.note || "No ideas this time — try again");
    $("#t-ai-note").textContent = r.ideas && r.ideas.length ? "small local AI — some will miss" : "";
  } catch (err) {
    fillList($("#t-ai"), [], "AI ideas failed: " + err.message);
    $("#t-ai-note").textContent = "";
  } finally {
    aiBusy = false;
    $("#btn-t-ai").disabled = false;
  }
}

async function updateTitles() {
  if (!titlesJobId) return;
  try {
    S.hashtags = $("#t-tags").value;          // remembered for the next video
    const { titles } = await post("/api/titles/refresh",
                                  { job_id: titlesJobId, game: $("#t-game").value,
                                    hashtags: S.hashtags });
    fillList($("#t-quotes"), titles.quotes || [], "Nothing quotable was said");
    $("#t-desc").value = titles.description || "";
    requestAiIdeas();
  } catch (err) { toast(err.message, "bad"); }
}

/* ---------- preview ----------------------------------------------------- */

function schedulePreview(delay = 380) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, delay);
}

async function refreshPreview() {
  if (!source) return;
  const seq = ++previewSeq;
  const frame = $("#preview-frame");
  frame.classList.add("busy");
  try {
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: source.id, settings: S, t: frameT }),
    });
    if (seq !== previewSeq) return;
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { msg = (await res.json()).error || msg; } catch (_) {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    if (seq !== previewSeq) return;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(blob);
    $("#preview-img").src = previewUrl;
    $("#preview-cap").textContent = `Live preview · ${S.out_w}×${S.out_h} · ${hms(frameT)}`;
  } catch (err) {
    if (seq === previewSeq) toast("Preview failed: " + err.message, "bad");
  } finally {
    if (seq === previewSeq) frame.classList.remove("busy");
  }
}

/* ---------- crop box ---------------------------------------------------- */

function boxRect() {
  return target === "cam" ? S.cam : S.game;
}

function stageScale() {
  const img = $("#stage-img");
  if (!source || !img.clientWidth) return 1;
  return img.clientWidth / source.width;
}

function placeBox() {
  if (!source) return;
  const k = stageScale();
  const r = boxRect() || {};
  const box = $("#cropbox");
  box.style.left = (r.x || 0) * k + "px";
  box.style.top = (r.y || 0) * k + "px";
  box.style.width = Math.max(6, (r.w || 0) * k) + "px";
  box.style.height = Math.max(6, (r.h || 0) * k) + "px";
  box.classList.toggle("game", target === "game");
  $("#cropbox-tag").textContent =
    (target === "cam" ? "Facecam " : "Gameplay ") + `${Math.round(r.w)}×${Math.round(r.h)}`;
  const camInputs = { "cam.x": r.x, "cam.y": r.y, "cam.w": r.w, "cam.h": r.h };
  if (target === "cam") {
    Object.entries(camInputs).forEach(([key, v]) => {
      const el = $(`[data-setting="${key}"]`);
      if (el && document.activeElement !== el) el.value = Math.round(v);
    });
  }
}

function initBoxDrag() {
  const stage = $("#stage");
  const box = $("#cropbox");
  let mode = null, startPt = null, startRect = null;

  const clampRect = (r) => {
    const W = source.width, H = source.height;
    r.w = Math.max(16, Math.min(Math.round(r.w), W));
    r.h = Math.max(16, Math.min(Math.round(r.h), H));
    r.x = Math.max(0, Math.min(Math.round(r.x), W - r.w));
    r.y = Math.max(0, Math.min(Math.round(r.y), H - r.h));
    return r;
  };

  const down = (e) => {
    if (!source) return;
    const handle = e.target.closest(".h");
    if (!handle && !e.target.closest("#cropbox")) {
      // click on empty frame starts a fresh box
      const k = stageScale();
      const rect = $("#stage-img").getBoundingClientRect();
      const x = (e.clientX - rect.left) / k, y = (e.clientY - rect.top) / k;
      const r = boxRect();
      Object.assign(r, clampRect({ x: x - r.w / 2, y: y - r.h / 2, w: r.w, h: r.h }));
      placeBox();
      schedulePreview();
      return;
    }
    mode = handle ? handle.className.split(" ")[1] : "move";
    startPt = { x: e.clientX, y: e.clientY };
    startRect = { ...boxRect() };
    box.setPointerCapture?.(e.pointerId);
    e.preventDefault();
  };

  const move = (e) => {
    if (!mode || !source) return;
    const k = stageScale();
    let dx = (e.clientX - startPt.x) / k;
    let dy = (e.clientY - startPt.y) / k;
    const r = { ...startRect };
    if (mode === "move") {
      r.x += dx; r.y += dy;
    } else {
      const square = e.shiftKey || (target === "cam" && startRect.w === startRect.h && !e.altKey);
      if (mode === "se") { r.w += dx; r.h += dy; }
      if (mode === "sw") { r.x += dx; r.w -= dx; r.h += dy; }
      if (mode === "ne") { r.w += dx; r.y += dy; r.h -= dy; }
      if (mode === "nw") { r.x += dx; r.w -= dx; r.y += dy; r.h -= dy; }
      if (square) {
        const size = Math.max(16, Math.round((r.w + r.h) / 2));
        if (mode === "nw" || mode === "sw") r.x = startRect.x + startRect.w - size;
        if (mode === "nw" || mode === "ne") r.y = startRect.y + startRect.h - size;
        r.w = r.h = size;
      }
    }
    Object.assign(boxRect(), clampRect(r));
    placeBox();
  };

  const up = () => {
    if (!mode) return;
    mode = null;
    schedulePreview(120);
  };

  stage.addEventListener("pointerdown", down);
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("resize", placeBox);
}

/* ---------- source loading --------------------------------------------- */

async function loadSource(text) {
  if (!text.trim()) { toast("Paste a link or pick a file first.", "bad"); return; }
  $("#source-progress").style.display = "";
  $("#src-bar").style.width = "3%";
  $("#src-stage").textContent = "Starting…";
  try {
    const { job } = await post("/api/source", {
      input: text,
      start: parseTime($("#range-start").value),
      end: parseTime($("#range-end").value),
      max_height: parseInt($("#max-height").value, 10) || 1080,
    });
    sourceJob = job.id;
    pollSource();
  } catch (err) {
    $("#source-progress").style.display = "none";
    toast(err.message, "bad");
  }
}

async function pollSource() {
  if (!sourceJob) return;
  try {
    const { job } = await api("/api/job/" + sourceJob);
    $("#src-bar").style.width = Math.round(job.progress * 100) + "%";
    $("#src-stage").textContent = job.message || job.stage || "Working…";
    if (job.status === "done") {
      sourceJob = null;
      $("#source-progress").style.display = "none";
      adoptSource(job.result.source);
      return;
    }
    if (job.status === "error" || job.status === "cancelled") {
      sourceJob = null;
      $("#source-progress").style.display = "none";
      if (job.status === "error") toast(job.error || "Could not load that stream.", "bad");
      return;
    }
  } catch (err) { /* keep polling */ }
  setTimeout(pollSource, 600);
}

function adoptSource(rec) {
  source = rec;
  if (!S.cam || !S.cam.w || S.cam.x + S.cam.w > rec.width || S.cam.y + S.cam.h > rec.height) {
    S.cam = { ...rec.default_cam };
  }
  S.game = { x: 0, y: 0, w: rec.width, h: rec.height };
  // Fold any older skip-intro/outro trim into the range picker, so it is visible
  // and adjustable rather than silently narrowing the search.
  const skipIn = Number(S.skip_intro) || 0;
  const skipOut = Number(S.skip_outro) || 0;
  S.range_start = Math.max(0, Math.min(skipIn, Math.max(0, rec.duration - 30)));
  S.range_end = skipOut > 0
    ? Math.max(S.range_start + 30, rec.duration - skipOut) : 0;
  if (S.range_end >= rec.duration - 1) S.range_end = 0;
  S.skip_intro = 0;
  S.skip_outro = 0;
  $("#source-loaded").style.display = "";
  $("#drop").style.display = "none";
  $("#range-more").style.display = "none";
  $("#src-title").textContent = rec.title || "(untitled)";
  $("#src-meta").textContent =
    `${rec.width}×${rec.height} · ${hms(rec.duration)} · ${rec.kind.replace("_", " ")}`
    + (rec.has_audio ? "" : " · no audio");
  moments = [];
  momentsJob = null;
  $("#moments-area").style.display = "none";
  $("#moments-list").innerHTML = "";
  $("#btn-find-moments").textContent = "Find moments";
  $("#moments-status").textContent = "Finds about twice as much as you need, so you have a choice";
  ["#card-mode", "#card-layout", "#card-clips", "#card-captions", "#card-moments", "#card-cleanup",
   "#card-output"]
    .forEach((s) => $(s).classList.remove("locked"));
  $("#go").disabled = false;
  const scrub = $("#scrub");
  scrub.max = Math.max(1, Math.floor(rec.duration));
  frameT = Math.min(rec.duration * 0.15, rec.duration - 1);
  scrub.value = frameT;
  paintRange(scrub);
  loadFrame(frameT);
  refreshRecents();
  syncRange();
  toast("Stream loaded — drag the box onto your facecam or PNGTuber.", "good");
}

function loadFrame(t) {
  if (!source) return;
  frameT = Math.max(0, Math.min(t, source.duration - 0.2));
  $("#scrub-val").textContent = hms(frameT);
  const img = $("#stage-img");
  img.onload = () => { placeBox(); $("#src-thumb").src = img.src; };
  img.src = `/api/frame/${source.id}?t=${frameT.toFixed(2)}&w=960`;
  schedulePreview(500);
}

async function refreshRecents() {
  try {
    const { sources } = await api("/api/sources");
    const box = $("#recents");
    box.innerHTML = "";
    sources.slice(0, 6).forEach((s) => {
      const b = document.createElement("button");
      b.textContent = s.title || s.path;
      b.title = s.path;
      b.onclick = () => adoptSource(s);
      box.appendChild(b);
    });
  } catch (_) {}
}

/* ---------- render ------------------------------------------------------ */

function updateEstimate() {
  const el = $("#estimate");
  if (!source) { el.textContent = ""; return; }
  const { a, b } = rangeSpan();
  const searched = Math.max(1, b - a);
  const say = (mins) => (mins < 1.5 ? "about a minute" : `about ${Math.round(mins)} minutes`);
  if (S.mode === "reel") {
    const total = picked().reduce((s, m) => s + (m.end - m.start), 0);
    if (!total) { el.textContent = moments.length ? "Pick some moments" : "Find moments first"; return; }
    el.textContent = `${hms(total)} reel from ${picked().length} moment${picked().length === 1 ? "" : "s"}`
      + ` — ${say(total / 60 / 1.6 + 0.3)}`;
    return;
  }
  if (S.mode === "clean") {
    if (!S.censor || !source.has_audio) {
      el.textContent = `${hms(searched)} of video — lossless copy, takes ${say(searched / 60 / 200 + 0.2)}`;
      return;
    }
    // listening is the slow part; the picture is copied, not re-encoded
    const mins = searched / (SPEECH_SPEED[S.clean_model] || 2) / 60 + searched / 60 / 150 + 0.5;
    el.textContent = `${hms(searched)} of video — up to ${say(mins).replace("about ", "")}`;
    return;
  }
  const n = S.whole_video ? 1 : (manual.length || S.clip_count);
  const len = S.whole_video ? Math.min(searched, 600) : S.target_len;
  const listen = (S.captions || S.censor) ? 0.8 : 0;
  const mins = (n * len * (listen + 0.75)) / 60 + 0.4;
  el.textContent = `${say(mins)[0].toUpperCase()}${say(mins).slice(1)} for `
    + `${n} clip${n === 1 ? "" : "s"}`;
}

async function startRender() {
  if (!source) return;
  S.manual_clips = manual;
  const body = { source_id: source.id, settings: S };
  if (S.mode === "reel") {
    body.selection = picked().map((m) => ({ start: m.start, end: m.end, peak: m.peak,
                                            score: m.score }));
    if (!body.selection.length) { toast("Tick at least one moment first.", "bad"); return; }
    if (momentsJob) momentsJob = null;      // the server stops finding when building starts
  }
  try {
    const { job } = await post("/api/render", body);
    renderJob = job.id;
    $("#card-results").style.display = "";
    $("#titles-panel").style.display = "none";
    $("#card-results h2").textContent = S.mode === "clean" ? "Your video"
      : S.mode === "reel" ? "Your highlight reel" : "Your clips";
    renderMode = S.mode;
    $("#clips").innerHTML = "";
    $("#hype").style.display = "none";
    $("#log").textContent = "";
    $("#go").disabled = true;
    $("#btn-cancel-render").style.display = "";
    $("#card-results").scrollIntoView({ behavior: "smooth", block: "start" });
    clearInterval(pollTimer);
    pollTimer = setInterval(pollRender, 700);
    pollRender();
  } catch (err) {
    toast(err.message, "bad");
  }
}

let lastClipCount = -1;
let renderMode = "clips";

async function pollRender() {
  if (!renderJob) return;
  let job;
  try { ({ job } = await api("/api/job/" + renderJob)); }
  catch (_) { return; }

  $("#job-bar").style.width = Math.round(job.progress * 100) + "%";
  $("#job-pct").textContent = Math.round(job.progress * 100) + "%";
  $("#job-stage").textContent = job.message || job.stage || "Working…";
  $("#results-sub").textContent = job.status === "running"
    ? `${job.stage} · ${Math.round(job.elapsed)}s elapsed` : "";
  if (job.log) $("#log").textContent = job.log.join("\n");

  const clips = (job.result && job.result.clips) || [];
  if (clips.length !== lastClipCount) {
    lastClipCount = clips.length;
    renderClipCards(clips, job.result.out_dir);
  }
  if (job.result && job.result.timeline && $("#hype").style.display === "none") {
    const dur = (job.result.source && job.result.source.duration) || 0;
    const st = job.result.settings || {};
    drawHype(job.result.timeline, job.result.detected || [], dur,
             { a: st.range_start || 0, b: st.range_end || dur });
  }

  if (["done", "error", "cancelled"].includes(job.status)) {
    clearInterval(pollTimer);
    renderJob = null;
    lastClipCount = -1;
    $("#go").disabled = false;
    if (job.status === "done" && job.result && job.result.titles) {
      renderTitles(job.id, job.result.titles);
    }
    applyMode();
    $("#btn-cancel-render").style.display = "none";
    $("#results-sub").textContent = (renderMode !== "clips" ? "" :
      `${clips.length} clip${clips.length === 1 ? "" : "s"} · `)
      + `${Math.round(job.elapsed)}s`;
    if (job.status === "done") {
      toast(renderMode === "reel" ? "Done — your highlight reel is ready."
        : renderMode === "clean"
        ? `Done — ${clips[0] && clips[0].censored ? clips[0].censored + " words bleeped" : "saved"}.`
        : `Done — ${clips.length} clips ready.`, "good");
    }
    else if (job.status === "error") { toast(job.error, "bad"); $("#log-more").open = true; }
  }
}

function renderClipCards(clips, outDir) {
  const wrap = $("#clips");
  wrap.innerHTML = "";
  const wide = clips.length && (clips[0].kind === "clean" || clips[0].kind === "reel");
  wrap.classList.toggle("wide", !!wide);
  if (!clips.length) {
    wrap.innerHTML = `<div class="empty" style="grid-column:1/-1">Working on it…</div>`;
    return;
  }
  const fileIn = (name) => `/api/file?path=${encodeURIComponent(outDir + "\\" + name)}`;
  clips.forEach((c) => {
    const card = document.createElement("div");
    card.className = "clip" + (wide ? " wide" : "");
    const fileUrl = `/api/file?path=${encodeURIComponent(c.path)}`;
    const thumbUrl = c.thumb ? fileIn(c.thumb) : "";
    const len = c.duration >= 120 ? hms(c.duration) : `${Math.round(c.duration)}s`;
    let badge;
    if (wide) badge = `<div class="tier full">${c.kind === "reel" ? "HIGHLIGHT REEL" : "FULL STREAM"}</div>`;
    else if (c.tier === "filler") {
      badge = `<div class="tier filler" title="Picked to fill your count - the stream had fewer clear highlights">EXTRA</div>`;
    } else badge = `<div class="tier" title="A clear highlight">HIGHLIGHT</div>`;
    const bleeped = c.censored && !wide ? ` · ${c.censored} bleeped` : "";
    const report = c.report
      ? `<a class="btn sm ghost" href="${fileIn(c.report)}" target="_blank">What got bleeped</a>` : "";
    card.innerHTML = `
      <div class="media">
        ${thumbUrl ? `<img src="${thumbUrl}" alt="">` : ""}
        <div class="play"><span>
          <svg viewBox="0 0 24 24" width="18" height="18" fill="#111"><path d="M8 5v14l11-7z"/></svg>
        </span></div>
        <div class="badge">${c.kind === "reel" ? `${c.moments} moments`
          : wide ? (c.start ? "from " + c.start_hms : "whole stream") : "at " + c.start_hms}</div>
        ${badge}
        <div class="len">${len}</div>
      </div>
      <div class="info">
        <b>${escapeHtml(wide ? c.name : (c.title || (c.text ? c.text.slice(0, 90) : c.name)))}</b>
        ${wide ? `<span class="sub">${escapeHtml(c.text)}</span>` : ""}
        <span class="sub">${(c.size / 1e6).toFixed(c.size > 1e9 ? 0 : 1)} MB${
          !wide && c.words ? ` · ${c.words} words` : ""}${bleeped}</span>
        <div class="acts">
          <a class="btn sm" href="${fileUrl}&download=1" download>Save</a>
          <button class="btn sm ghost" data-reveal="${escapeAttr(c.path)}">Show</button>
          ${report}
          ${!wide && c.title ? `<button class="btn sm ghost" data-copy="title">Copy title</button>
            <button class="btn sm ghost" data-copy="desc">Copy description</button>` : ""}
        </div>
      </div>`;
    $(".play", card).onclick = () => {
      const media = $(".media", card);
      media.innerHTML = `<video src="${fileUrl}" controls autoplay playsinline></video>`;
    };
    $("[data-reveal]", card).onclick = (e) =>
      post("/api/reveal", { path: e.currentTarget.dataset.reveal });
    $$("[data-copy]", card).forEach((b) => b.onclick = (e) =>
      copyText(b.dataset.copy === "title" ? c.title : c.description, e.currentTarget));
    wrap.appendChild(card);
  });
}

const escapeHtml = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const escapeAttr = (s) => escapeHtml(s);

function drawHype(timeline, detected, duration, range) {
  const cv = $("#hype");
  if (!timeline || !timeline.length) return;
  cv.style.display = "block";
  const w = cv.clientWidth || 900, h = 74;
  cv.width = w * devicePixelRatio;
  cv.height = h * devicePixelRatio;
  const g = cv.getContext("2d");
  g.scale(devicePixelRatio, devicePixelRatio);
  g.clearRect(0, 0, w, h);

  // the stretch that was excluded from the search
  if (duration > 0 && range && (range.a > 0 || range.b < duration)) {
    g.fillStyle = "rgba(6,8,16,.62)";
    if (range.a > 0) g.fillRect(0, 0, (range.a / duration) * w, h);
    if (range.b < duration) {
      const x = (range.b / duration) * w;
      g.fillRect(x, 0, w - x, h);
    }
  }
  // chosen windows
  if (duration > 0) {
    g.fillStyle = "rgba(124,108,255,.22)";
    detected.forEach((c) => {
      const x0 = (c.start / duration) * w, x1 = (c.end / duration) * w;
      g.fillRect(x0, 0, Math.max(2, x1 - x0), h);
    });
  }
  // hype curve
  const grad = g.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, "rgba(34,211,238,.9)");
  grad.addColorStop(1, "rgba(34,211,238,.08)");
  g.beginPath();
  g.moveTo(0, h);
  timeline.forEach((v, i) => g.lineTo((i / (timeline.length - 1)) * w, h - v * (h - 6) - 3));
  g.lineTo(w, h);
  g.closePath();
  g.fillStyle = grad;
  g.fill();
  g.beginPath();
  timeline.forEach((v, i) => {
    const x = (i / (timeline.length - 1)) * w, y = h - v * (h - 6) - 3;
    i ? g.lineTo(x, y) : g.moveTo(x, y);
  });
  g.strokeStyle = "#67e8f9";
  g.lineWidth = 1.3;
  g.stroke();
}

/* ---------- manual clips ------------------------------------------------ */

function renderManual() {
  const box = $("#manual-list");
  box.innerHTML = "";
  if (!manual.length) {
    box.innerHTML = `<span class="hint">None yet — detection will choose for you.</span>`;
  }
  manual.forEach((m, i) => {
    const row = document.createElement("div");
    row.className = "row";
    row.style.marginBottom = "7px";
    row.innerHTML = `<span class="chip">${hms(m.start)} → ${hms(m.end)}
      (${Math.round(m.end - m.start)}s)</span>`;
    const del = document.createElement("button");
    del.className = "btn sm danger";
    del.textContent = "Remove";
    del.onclick = () => { manual.splice(i, 1); renderManual(); updateEstimate(); };
    row.appendChild(del);
    box.appendChild(row);
  });
  updateEstimate();
}

/* ---------- wiring ------------------------------------------------------ */

async function init() {
  boot = await api("/api/bootstrap");
  S = JSON.parse(JSON.stringify(boot.defaults));
  Object.assign(S, boot.last || {});
  S.manual_clips = [];

  const chip = $("#chip-ffmpeg");
  chip.classList.toggle("bad", !boot.ffmpeg.ok);
  chip.lastChild.textContent = boot.ffmpeg.ok ? " ffmpeg ready" : " ffmpeg missing";
  if (!boot.ffmpeg.ok) {
    toast("ffmpeg was not found. Install it with: winget install Gyan.FFmpeg", "bad");
  }

  const fontSel = $("#font-select");
  (boot.fonts || []).forEach((f) => fontSel.add(new Option(f, f)));
  if (!boot.fonts.includes(S.cap_font) && boot.fonts.length) S.cap_font = boot.fonts[0];

  const modelSel = $("#model-select");
  const speed = { tiny: "fastest, rough", base: "fast", small: "recommended",
                  medium: "slow, accurate", "large-v3": "very slow, best" };
  (boot.models || []).forEach((m) =>
    modelSel.add(new Option(`${m.id} — ${speed[m.id] || ""}${m.cached ? "" : " (downloads)"}`, m.id)));

  // For a whole stream speed matters more than polish; it only has to catch swears.
  const cleanSpeed = { tiny: "fastest, misses some", base: "recommended",
                       small: "slower, catches a bit more", medium: "very slow",
                       "large-v3": "far too slow for hours" };
  const cleanSel = $("#clean-model-select");
  (boot.models || []).forEach((m) =>
    cleanSel.add(new Option(`${m.id} — ${cleanSpeed[m.id] || ""}${m.cached ? "" : " (downloads)"}`,
                            m.id)));
  $("#censor-test").addEventListener("input", scheduleCensorTest);

  $("#btn-find-moments").onclick = findMoments;
  $("#btn-pick-best").onclick = pickBest;
  $("#btn-pick-none").onclick = () => { moments.forEach((m) => { m.picked = false; }); renderMoments(); };
  $("#btn-add-moment").onclick = addMomentHere;
  $("#moment-player").addEventListener("timeupdate", (e) => {
    if (stopAt !== null && e.target.currentTime >= stopAt) { e.target.pause(); stopAt = null; }
  });
  $("#moment-player").addEventListener("seeking", (e) => {
    // The viewer scrubbed elsewhere: stop enforcing the old moment's end.
    if (stopAt !== null && (e.target.currentTime > stopAt + 1)) stopAt = null;
  });
  $("#btn-t-ai").onclick = requestAiIdeas;
  $("#btn-t-update").onclick = updateTitles;
  $("#btn-t-desc").onclick = (e) => copyText($("#t-desc").value, e.currentTarget);

  const encSel = $("#encoder-select");
  encSel.add(new Option("Automatic (CPU, best quality)", "auto"));
  (boot.encoders || []).forEach((e) => {
    const label = { libx264: "libx264 (CPU)", h264_amf: "h264_amf (AMD GPU, faster)",
                    h264_nvenc: "h264_nvenc (NVIDIA GPU, faster)",
                    libx265: "libx265 (HEVC, smaller)" }[e];
    if (label) encSel.add(new Option(label, e));
  });

  refreshPresets(boot.presets);
  if (!S.out_folder) $("#out-folder").placeholder = boot.output_dir;

  ["#FFE23D", "#22D3EE", "#7C6CFF", "#34D399", "#F87171", "#FF6BD6", "#FFFFFF"]
    .forEach((c) => {
      const b = document.createElement("button");
      b.style.background = c;
      b.title = c;
      b.onclick = () => {
        S.cap_highlight = c;
        $('[data-setting="cap_highlight"]').value = c;
        schedulePreview();
      };
      $("#hl-swatches").appendChild(b);
    });

  bindSettings();
  initRangePicker();
  syncUI();
  initBoxDrag();
  renderManual();
  refreshRecents();

  /* buttons */
  $("#btn-load").onclick = () => loadSource($("#source-input").value);
  $("#source-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadSource($("#source-input").value);
  });
  $("#btn-browse").onclick = async () => {
    const { path } = await post("/api/browse", { kind: "file" });
    if (path) { $("#source-input").value = path; loadSource(path); }
  };
  $("#btn-cancel-source").onclick = () => sourceJob && post(`/api/job/${sourceJob}/cancel`);
  $("#btn-change-source").onclick = () => {
    $("#source-loaded").style.display = "none";
    $("#drop").style.display = "";
    $("#range-more").style.display = "";
  };
  $("#btn-open-out").onclick = () => post("/api/reveal", { path: S.out_folder || boot.output_dir });
  $("#btn-open-clips").onclick = () => post("/api/reveal", { path: S.out_folder || boot.output_dir });

  $("#seg-target").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    target = b.dataset.value;
    $$("button", $("#seg-target")).forEach((x) => x.classList.toggle("on", x === b));
    placeBox();
  });
  $("#btn-reset-cam").onclick = () => {
    if (!source) return;
    target = "cam";
    $$("button", $("#seg-target")).forEach((x) => x.classList.toggle("on", x.dataset.value === "cam"));
    S.cam = { ...source.default_cam };
    placeBox();
    schedulePreview(80);
  };
  $("#scrub").addEventListener("input", (e) => {
    paintRange(e.target);
    loadFrame(parseFloat(e.target.value));
  });
  $("#btn-refresh-preview").onclick = () => refreshPreview();

  $("#seg-size").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const [w, h] = b.dataset.value.split("x").map(Number);
    S.out_w = w; S.out_h = h;
    $$("button", $("#seg-size")).forEach((x) => x.classList.toggle("on", x === b));
    schedulePreview(80);
  });
  $("#btn-pick-out").onclick = async () => {
    const { path } = await post("/api/browse", { kind: "folder" });
    if (path) { S.out_folder = path; $("#out-folder").value = path; }
  };
  $("#btn-bg-image").onclick = async () => {
    const { path } = await post("/api/browse", { kind: "file" });
    if (path) {
      S.top_bg_image = path;
      $('[data-setting="top_bg_image"]').value = path;
      S.top_bg = "image";
      syncUI();
      schedulePreview(80);
    }
  };
  $("#btn-add-manual").onclick = () => {
    if (!source) return;
    manual.push({ start: Math.round(frameT), end: Math.round(Math.min(frameT + S.target_len, source.duration)) });
    manual.sort((a, b) => a.start - b.start);
    renderManual();
    toast("Added a hand-picked clip.");
  };

  $("#btn-save-preset").onclick = async () => {
    const name = prompt("Name this preset", "My layout");
    if (!name) return;
    const { presets } = await post("/api/presets", { action: "save", name, settings: S });
    refreshPresets(presets);
    toast("Preset saved.", "good");
  };
  $("#btn-del-preset").onclick = async () => {
    const name = $("#preset-select").value;
    if (!name) return;
    const { presets } = await post("/api/presets", { action: "delete", name });
    refreshPresets(presets);
  };
  $("#preset-select").addEventListener("change", (e) => {
    const p = (boot.presets || {})[e.target.value];
    if (!p) return;
    const cam = S.cam, game = S.game;
    Object.assign(S, p);
    if (!p.cam) S.cam = cam;
    if (!p.game) S.game = game;
    syncUI();
    placeBox();
    schedulePreview(80);
    toast("Preset applied.");
  });

  $("#btn-cleanup").onclick = async () => {
    const { freed_mb } = await post("/api/cleanup", {});
    toast(`Freed ${freed_mb} MB.`, "good");
    refreshRecents();
  };

  $("#go").onclick = startRender;
  $("#btn-cancel-render").onclick = () => renderJob && post(`/api/job/${renderJob}/cancel`);

  $$(".card.collapsible > .head").forEach((h) =>
    h.addEventListener("click", () => h.parentElement.classList.toggle("collapsed")));

  initDrop();
}

function refreshPresets(presets) {
  boot.presets = presets || {};
  const sel = $("#preset-select");
  sel.innerHTML = `<option value="">— choose a preset —</option>`;
  Object.keys(boot.presets).forEach((n) => sel.add(new Option(n, n)));
}

function initDrop() {
  const zone = $("#drop");
  ["dragenter", "dragover"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("hot"); }));
  ["dragleave", "drop"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("hot"); }));
  zone.addEventListener("drop", async (e) => {
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!file) {
      const text = e.dataTransfer.getData("text");
      if (text) { $("#source-input").value = text; loadSource(text); }
      return;
    }
    $("#source-progress").style.display = "";
    $("#src-stage").textContent = `Copying ${file.name}…`;
    $("#src-bar").style.width = "8%";
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "upload failed");
      $("#source-input").value = data.path;
      loadSource(data.path);
    } catch (err) {
      $("#source-progress").style.display = "none";
      toast("Could not read that file: " + err.message, "bad");
    }
  });
}

init().catch((err) => {
  document.body.insertAdjacentHTML("afterbegin",
    `<div style="padding:30px;color:#f87171">Could not start: ${escapeHtml(err.message)}</div>`);
});
