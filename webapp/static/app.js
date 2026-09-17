"use strict";

/* ClipMint — front end.
   Sections: helpers · chrome · setup · source · layout · run · results
             · player · trim · mini player */

const $ = (id) => document.getElementById(id);
const body = document.body;

const EXAMPLES = [
  "30 seconds only",
  "between 20 and 40 seconds",
  "only the funny rage moments",
  "hooks that ask a question",
  "webcam at the top",
  "my webcam is bottom right",
  "gameplay only, no webcam",
  "follow my face",
  "just 5 clips",
  "cut 14:45 to 15:30",
];

const STEPS = [
  ["download", "Fetching"],
  ["transcribe", "Transcribing"],
  ["rank", "Ranking"],
  ["render", "Rendering"],
];

let source = null;       // { source, name }
let specTimer = null;
let es = null;           // EventSource
let jobId = null;
let runs = [];           // every run still on disk, newest first
let clips = [];          // those runs' clips, flattened in display order
let cur = -1;            // index of the clip in the player
let shown = [];          // indices into `clips` currently on screen, in order
let libQ = "";           // library search text
let libWhen = "all";     // library date window: all | 1 | 7 | 30
let libTimer = null;
let trim = null;         // { lo, hi, start, end }
let confirmFn = null;

// A clip knows which run it came from, so the player's buttons keep working
// on clips made in an earlier session rather than only the newest one.
const jobOf = (c) => (c && c.job_id) || jobId;

// ---------------------------------------------------------------- helpers

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function clock(sec) {
  const t = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
           : `${m}:${String(s).padStart(2, "0")}`;
}

// Accepts "1:02:03", "14:45", or bare seconds — whatever the user types.
function parseClock(text) {
  const raw = String(text || "").trim();
  if (!raw) return NaN;
  if (!raw.includes(":")) return parseFloat(raw);
  return raw.split(":").reduce((acc, part) => acc * 60 + (parseFloat(part) || 0), 0);
}

function fmtDur(sec) {
  if (!sec) return "";
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}:${String(s).padStart(2, "0")}`;
}

function fmtSize(b) {
  if (b > 1e9) return (b / 1e9).toFixed(1) + " GB";
  if (b > 1e6) return Math.round(b / 1e6) + " MB";
  return Math.round(b / 1e3) + " KB";
}

let toastTimer = null;
function toast(msg, bad) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("bad", !!bad);
  t.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("on"), bad ? 5200 : 3000);
}

async function api(url, opts) {
  const r = await fetch(url, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `Request failed (${r.status})`);
  return d;
}

function json(method, payload) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) };
}

function loadbar(frac) {
  const el = $("loadbar");
  if (frac === null) { el.classList.remove("on"); el.firstElementChild.style.transform = "scaleX(0)"; return; }
  el.classList.add("on");
  el.firstElementChild.style.transform = `scaleX(${Math.max(0, Math.min(1, frac))})`;
}

function ask(title, text, onYes) {
  $("cTitle").textContent = title;
  $("cText").textContent = text;
  confirmFn = onYes;
  $("confirm").classList.remove("hidden");
  body.classList.add("confirm-on");
}

function closeAsk() {
  $("confirm").classList.add("hidden");
  body.classList.remove("confirm-on");
  confirmFn = null;
}

// ---------------------------------------------------------------- chrome

$("menuBtn").onclick = () => {
  // Wide screens hide the rail; narrow ones reveal it over the content.
  if (window.innerWidth > 1000) body.classList.toggle("guide-off");
  else body.classList.toggle("guide-on");
};

document.querySelectorAll(".g-item[data-go]").forEach((item) => {
  item.onclick = () => {
    const el = $(item.dataset.go);
    if (!el || el.classList.contains("hidden")) return;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    if (window.innerWidth <= 1000) body.classList.remove("guide-on");
  };
});

// Highlight whichever section is on screen, the way a guide rail should.
const spy = new IntersectionObserver((entries) => {
  entries.forEach((e) => {
    if (!e.isIntersecting) return;
    document.querySelectorAll(".g-item[data-go]").forEach((i) =>
      i.classList.toggle("active", i.dataset.go === e.target.id));
  });
}, { rootMargin: "-15% 0px -70% 0px" });
["secCreate", "secLayout", "results", "secHelp"].forEach((id) => {
  const el = $(id);
  if (el) spy.observe(el);
});

/* ---------------------------------------------------------------- theme --
   The theme itself is set in a snippet in <head>, before the first paint.
   This is only the switch: it flips the attribute everything is keyed off,
   remembers the choice, and keeps the button honest about what it does.

   Remembered per machine, in localStorage rather than in settings.json,
   because it is a property of the screen you are looking at — someone with
   the app on a laptop and a bright desktop wants different answers, and a
   setting that syncs would give them one. */
const THEME_KEY = "clipmint.theme";
// What the key was called before the rename. Read, never written, so an
// existing install keeps the theme it chose; see the snippet in <head>.
const LEGACY_THEME_KEY = "sts.theme";

function storedTheme() {
  try {
    return localStorage.getItem(THEME_KEY) || localStorage.getItem(LEGACY_THEME_KEY);
  } catch (e) {
    return null;                              // private mode, or storage blocked
  }
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const btn = $("themeBtn");
  if (!btn) return;
  const toLight = theme === "dark";           // what the button would do next
  btn.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
  // enTitle is where the translator keeps the English a node was born with.
  // Setting title alone would work until someone changed language, at which
  // point the tooltip would revert to whatever it said when the page loaded.
  btn.dataset.enTitle = toLight ? "Switch to light mode" : "Switch to dark mode";
  btn.title = I18N.t(btn.dataset.enTitle);
}

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light"
    ? "light" : "dark";
}

$("themeBtn").onclick = () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* not fatal */ }
};

// A machine that has never been told which theme to use follows the system,
// and keeps following it — so switching macOS or Windows to light at sunset
// takes the app with it. Once the button is pressed, that choice wins and
// this stops applying.
try {
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", (e) => {
    const saved = storedTheme();
    if (saved !== "light" && saved !== "dark") applyTheme(e.matches ? "light" : "dark");
  });
} catch (e) { /* older webview: the theme just stays where it is */ }

applyTheme(currentTheme());

function openDrawer() {
  body.classList.add("drawer-on");
  loadLocations();
  loadCleanup();
  loadYtAccess(false);
  loadYtu();
}
$("settingsBtn").onclick = openDrawer;
$("gSettings").onclick = openDrawer;
$("drawerClose").onclick = () => body.classList.remove("drawer-on");
$("scrim").onclick = () => {
  body.classList.remove("drawer-on");
  if (confirmFn) closeAsk();
};

$("createBtn").onclick = () => {
  $("secCreate").scrollIntoView({ behavior: "smooth", block: "start" });
  $("url").focus();
};

$("cNo").onclick = closeAsk;
$("cYes").onclick = () => { const f = confirmFn; closeAsk(); if (f) f(); };

// ---------------------------------------------------------------- settings

async function checkSetup() {
  try {
    const d = await api("/api/settings");
    if (!d.has_key) $("setup").classList.remove("hidden");
    if (d.provider) {
      $("setProvider").value = d.provider;
      $("setProvider2").value = d.provider;
    }
    keysOnFile = d.keys || {};
    providerPinned = !!d.provider_pinned;
    drawModels(d);
    if (d.daily_limits) $("capOpenai").value = d.daily_limits.openai || "";
    drawSwitcher(d.provider);
    $("setInfo").innerHTML = `
      <div><span>Key</span><b>${d.has_key ? "set — from " + esc(d.source) : "not set"}</b></div>
      <div><span>Model</span><b>${esc(d.model || "—")}</b></div>
      <div><span>ffmpeg</span><b>${d.ffmpeg ? "found" : "missing"}</b></div>`;
    if (!d.ffmpeg) {
      $("srcErr").innerHTML = `<div class="err">ffmpeg isn't on your PATH — clips can't be rendered without it. `
        + `Install it from ffmpeg.org, then restart this app.</div>`;
    }
  } catch (_) { /* an offline settings check is not worth blocking startup */ }
}

// ---------------------------------------------------------------- processor

// What does the heavy work, and why. Shown under the live preview, asked once
// when the page opens and again after every run: the answer is found by
// trying things (a one-second test encode), and a run can change it -- an
// encoder that fails mid-run is dropped for that run.
async function loadProcessor(recheck = false) {
  const info = $("procInfo");
  try {
    const d = await api(`/api/processor${recheck ? "?recheck=true" : ""}`);
    $("procMode").value = d.mode || "auto";
    $("procMode").disabled = !!d.pinned;
    const video = d.video_label
      || (d.checked ? "CPU (x264)" : "checked once the run is resumed");
    const talk = d.transcribe_device === "cuda" ? "NVIDIA GPU (CUDA)" : "CPU";
    info.innerHTML = `
      <div><span>Video encoding</span><b>${esc(video)}</b></div>
      <div><span>Transcription</span><b>${esc(talk)}</b></div>`;
    $("procHint").textContent = d.pinned
      ? "Set by the PROCESSOR environment variable, which outranks this setting."
      : d.transcribe_device === "cuda" ? "" : `Transcription uses the CPU: ${d.transcribe_reason}.`;
  } catch (e) {
    info.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

$("procMode").onchange = async () => {
  $("procMsg").innerHTML = "";
  try {
    await api("/api/processor", json("POST", { mode: $("procMode").value }));
    await loadProcessor();
    toast("Saved — applies from the next clip.");
  } catch (e) {
    $("procMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
};

$("procCheck").onclick = async () => {
  $("procCheck").disabled = true;
  await loadProcessor(true);
  $("procCheck").disabled = false;
};

// Which Gemini model you are on decides how many free runs a day you get —
// 20 on one, hundreds on another. That number belongs next to the choice,
// not in a docs page you would have to go looking for.
function drawModels(d) {
  const fld = $("modelFld"), sel = $("setModel"), hint = $("modelHint");
  // Groq gets a picker too. Its models differ by size rather than by daily
  // allowance, so they carry a written note instead of a number — hiding the
  // control entirely, as this used to, left no way to choose one at all.
  const groq = d.provider === "groq";
  const models = (groq ? d.groq_models : d.gemini_models) || [];
  if (!["gemini", "groq"].includes(d.provider) || !models.length) {
    fld.hidden = true; hint.textContent = ""; return;
  }

  fld.hidden = false;
  // A model we have no confirmed number for says so, rather than showing a
  // guess with the same confidence as a checked one.
  sel.innerHTML = models.map((m) => {
    const note = groq ? m.note
                      : (m.daily_free ? `${m.daily_free}/day free` : "limit unknown");
    return `<option value="${esc(m.value)}">${esc(m.value)} — ${esc(note)}</option>`;
  }).join("");
  if (models.some((m) => m.value === d.model)) sel.value = d.model;
  else sel.insertAdjacentHTML("afterbegin",
    `<option value="${esc(d.model)}" selected>${esc(d.model)} — limit unknown</option>`);

  if (d.model_pinned) {
    sel.disabled = true;
    hint.textContent = `${groq ? "GROQ_MODEL" : "GEMINI_MODEL"} in your `
      + "environment is deciding this one.";
    return;
  }
  sel.disabled = false;
  hint.textContent = groq
    ? "Groq's free tier is the same for every model here — 30 a minute, 1000 a "
      + "day, 8k tokens a minute — so this is a straight quality-for-speed "
      + "trade, not an allowance one."
    : "This list comes from your key, so retired models can't appear. "
      + "Daily limits are the published free-tier numbers where known — the app "
      + "trusts a real quota error over them.";
  sel.onchange = () => saveModel(sel.value, d.provider);
}

async function saveModel(model, provider) {
  const hint = $("modelHint");
  try {
    // as_fallback: change the model, not which provider is active. The
    // provider has to be passed rather than hardcoded, or picking a Groq
    // model files it under GEMINI_MODEL and nothing appears to change.
    await api("/api/settings", json("POST", {
      provider: provider || "gemini", model, as_fallback: true,
    }));
    await checkSetup();
    refreshUsage();
    toast(`Now using ${model}.`);
  } catch (e) {
    hint.textContent = e.message || "Could not change the model.";
    toast(e.message || "Could not change the model.", true);
  }
}

// Which providers already have a key on disk, so the switcher can offer a
// flip instead of demanding a secret that was saved hours ago.
let keysOnFile = {};
let providerPinned = false;

function drawSwitcher(active) {
  const other = active === "gemini" ? "openai" : "gemini";
  const label = other === "gemini" ? "Gemini" : "OpenAI";
  const el = $("switcher");
  if (!keysOnFile[other]) { el.hidden = true; return; }
  el.hidden = false;

  // An LLM_PROVIDER env var wins over the settings file, so saving here would
  // change nothing visible. Better to say that than to offer a dead button.
  if (providerPinned) {
    $("switchNote").textContent =
      `${label} is set up, but LLM_PROVIDER in your environment is deciding.`;
    $("switchBtn").hidden = true;
    return;
  }
  $("switchBtn").hidden = false;
  $("switchNote").textContent = `${label} is set up too.`;
  $("switchBtn").textContent = `Use ${label}`;
  $("switchBtn").onclick = () => switchProvider(other);
}

async function switchProvider(provider) {
  const btn = $("switchBtn");
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = "Switching…";
  try {
    // No api_key: the server falls back to the stored one for this provider.
    await api("/api/settings", json("POST", { provider }));
    await checkSetup();
    refreshUsage();
    toast(`Now using ${provider === "gemini" ? "Gemini" : "OpenAI"}.`);
  } catch (e) {
    toast(e.message || "Could not switch provider.", true);
    btn.textContent = was;
  } finally {
    btn.disabled = false;
  }
}

function fmtSpan(sec) {
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

// One bar per provider that has a number worth filling. Gemini's comes from
// the free tier; OpenAI has no daily cap of its own, so its bar only appears
// once the user sets one — a meter against an invented limit would be worse
// than no meter at all.
function meterHtml(name, p) {
  if (!p || !p.limit) return "";
  const frac = Math.min(1, p.used / p.limit);
  const cls = frac >= 1 ? "spent" : (frac >= 0.7 ? "warn" : "");
  const label = p.exhausted ? `${name} — spent` : `${name} requests`;
  return `<div class="meter">
    <div class="meter-top"><span>${esc(label)}</span><b>${p.used} / ${p.limit}</b></div>
    <div class="meter-track">
      <div class="meter-fill ${cls}" style="width:${Math.round(frac * 100)}%"></div>
    </div>
  </div>`;
}

// A long video costs roughly one request per 20 minutes plus two, so ten left
// is a comfortable run and two is not. Sitting above the render button, this
// is the last thing seen before committing to a download and a transcription.
function drawBudget(g, u) {
  const el = $("budget");
  if (!g || !g.limit) { el.classList.add("hidden"); return; }

  const frac = Math.min(1, g.used / g.limit);
  const fill = $("budgetFill");
  fill.style.width = `${Math.round(frac * 100)}%`;
  fill.classList.toggle("warn", frac >= 0.7 && frac < 1);
  fill.classList.toggle("spent", frac >= 1);

  el.classList.remove("hidden", "low", "out");
  if (g.exhausted) {
    el.classList.add("out");
    $("budgetText").textContent = u.fallback_ready
      ? `Gemini spent for today — this run will continue on OpenAI`
      : `Gemini spent for today — back in ${fmtSpan(u.resets_in_seconds)}`;
  } else {
    if (frac >= 0.7) el.classList.add("low");
    $("budgetText").textContent =
      `${g.remaining} of ${g.limit} API requests left today`;
  }
}

// The free tier caps requests per *day*, so the number that matters is how
// much of today is left — shown before a run, not discovered three chunks in.
async function refreshUsage() {
  try {
    const u = await api("/api/usage");
    const g = u.providers.gemini || null;
    const o = u.providers.openai || null;

    // The bar only means something against a known cap. OpenAI has no daily
    // limit to fill, so it stays a number and the bar stays hidden.
    $("meters").innerHTML = [
      meterHtml("Gemini", g),
      meterHtml("OpenAI", o),
    ].filter(Boolean).join("");

    drawBudget(g, u);

    const rows = [];
    // Anything without a cap has no bar, so it still needs a plain count.
    if (g && !g.limit) rows.push(`<div><span>Gemini</span><b>${g.used} used</b></div>`);
    if (o && !o.limit) rows.push(`<div><span>OpenAI</span><b>${o.used} used</b></div>`);
    if (!g && !o) rows.push(`<div><span>Requests</span><b>none yet today</b></div>`);
    rows.push(`<div><span>Resets in</span><b>${fmtSpan(u.resets_in_seconds)}</b></div>`);
    $("usageInfo").innerHTML = rows.join("");

    let hint;
    if (g && g.exhausted && u.fallback_ready) {
      hint = "Gemini is out for today — runs will continue on OpenAI automatically.";
    } else if (g && g.exhausted) {
      hint = "Gemini is out for today. Add an OpenAI key below to keep going, "
           + "or come back after the reset — a part-finished run picks up where it stopped.";
    } else if (u.fallback_ready) {
      hint = "If Gemini runs out mid-run, OpenAI takes over automatically.";
    } else {
      hint = "A long video costs roughly one request per 20 minutes, plus two.";
    }
    $("usageHint").textContent = hint;
  } catch (_) { /* the app still works without a usage panel */ }
}

// A table, not a ternary: with three providers "gemini or else openai" sends
// Groq users to OpenAI's signup page, which is a dead end they have to work
// out for themselves.
const KEY_LINKS = {
  gemini: ["Get a free Gemini key →", "https://aistudio.google.com/apikey"],
  groq:   ["Get a free Groq key →",   "https://console.groq.com/keys"],
  openai: ["Get an OpenAI key →",     "https://platform.openai.com/api-keys"],
};

function keyLinkFor(provider) {
  const [label, href] = KEY_LINKS[provider] || KEY_LINKS.gemini;
  $("keyLink").textContent = label;
  $("keyLink").href = href;
}
$("setProvider").onchange = () => keyLinkFor($("setProvider").value);

async function saveKey(providerEl, keyEl, btn, msgEl, onDone) {
  const key = keyEl.value.trim();
  if (!key) { msgEl.innerHTML = `<div class="err">Paste a key first.</div>`; return; }
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    await api("/api/settings", json("POST", { provider: providerEl.value, api_key: key }));
    msgEl.innerHTML = `<div class="ok-box">Saved. You're ready to go.</div>`;
    keyEl.value = "";
    checkSetup();
    if (onDone) setTimeout(onDone, 1100);
  } catch (e) {
    msgEl.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

$("setSave").onclick = () =>
  saveKey($("setProvider"), $("setKey"), $("setSave"), $("setMsg"),
          () => $("setup").classList.add("hidden"));

$("setSave2").onclick = () =>
  saveKey($("setProvider2"), $("setKey2"), $("setSave2"), $("setMsg2"), null);

// ------------------------------------------------------------ YouTube sign-in
//
// The fallback for YouTube's "confirm you're not a bot" gate. The app climbs a
// ladder of its own before it ever gets here -- see shorts_generator/local/
// yt_access.py -- so this panel exists for the case where none of that worked
// and somebody needs to hand over real cookies.
//
// The point of the panel is that it answers before it is asked: it says which
// browsers on this PC can actually supply cookies, and why the others can't,
// because "use --cookies-from-browser" is useless advice next to a Chrome that
// encrypts its cookies against everything but itself.

let ytAccess = null;

function ytaRows(a) {
  const rows = [];
  const modes = {
    auto: "Work it out", browser: "Cookies from a browser",
    file: "A cookies.txt file", off: "Never use cookies",
  };
  rows.push(`<div><span>Fallback</span><b>${esc(modes[a.mode] || a.mode)}</b></div>`);

  const usable = (a.browsers || []).filter(b => b.ok);
  const signedIn = usable.filter(b => b.signed_in);
  let ready;
  if (a.mode === "off") ready = "Off";
  else if (signedIn.length) ready = signedIn.map(b => titleCase(b.browser)).join(", ");
  else if (usable.length) ready = usable.map(b => titleCase(b.browser)).join(", ") + " (not signed in)";
  else if (a.file || a.discovered) ready = "A cookies.txt file";
  else ready = "Nothing yet";
  rows.push(`<div><span>Can sign in with</span><b>${esc(ready)}</b></div>`);

  if (a.discovered && !a.file) {
    rows.push(`<div><span>Found on disk</span><b>${esc(a.discovered)}</b></div>`);
  }
  return rows.join("");
}

function titleCase(s) { return (s || "").charAt(0).toUpperCase() + (s || "").slice(1); }

function ytaFillBrowsers(a) {
  const sel = $("ytaBrowser");
  sel.innerHTML = "";
  for (const b of a.browsers || []) {
    const o = document.createElement("option");
    o.value = b.browser;
    // The reason lives in the option itself. A dropdown that lists Chrome and
    // then rejects it on Save is a worse experience than one that says up front
    // which entries are going to work.
    o.textContent = titleCase(b.browser) + I18N.t(
      b.signed_in ? " — signed in"
      : b.ok ? " — has cookies, not signed in"
      : " — can't be read");
    o.disabled = !b.ok;
    sel.appendChild(o);
  }
  if (!sel.options.length) {
    const o = document.createElement("option");
    o.textContent = "No browser on this PC can hand over cookies";
    o.disabled = true;
    sel.appendChild(o);
  }
  if (a.browser) sel.value = a.browser;
}

function ytaSyncFields() {
  const mode = $("ytaMode").value;
  $("ytaBrowserFld").hidden = mode !== "browser";
  $("ytaFileFld").hidden = mode !== "file";

  const a = ytAccess || {};
  let hint;
  if (mode === "off") {
    hint = "The app will still try the tricks that need no sign-in, but when YouTube "
         + "insists on an account the download stops with an explanation.";
  } else if (mode === "browser") {
    const bad = (a.browsers || []).filter(b => !b.ok);
    hint = "Only used when YouTube refuses the download. "
         + (bad.length ? bad[0].reason : "Sign the browser in to YouTube first.");
  } else if (mode === "file") {
    hint = "Export one with a \"Get cookies.txt\" browser extension, with youtube.com open "
         + "and signed in. This is the way round Chrome and Edge locking their own cookies. "
         + "The app copies it and works from the copy, so your export is left alone."
         + (a.drop_folder ? ` A file saved into ${a.drop_folder} is picked up without setting anything here.` : "");
  } else {
    hint = "Tries the download several ways first, and only reaches for cookies if YouTube "
         + "still refuses. Nothing leaves this PC — the cookies go to YouTube, which is "
         + "where they came from. YouTube does get to see which account the downloads "
         + "belong to, so use a spare account if that matters.";
  }
  $("ytaHint").textContent = I18N.t(hint);
}

async function loadYtAccess(recheck) {
  const info = $("ytaInfo");
  info.innerHTML = `<div><span>Checking this PC…</span><b></b></div>`;
  try {
    ytAccess = await api("/api/youtube-access" + (recheck ? "?recheck=true" : ""));
  } catch (_) {
    info.innerHTML = `<div><span>Couldn't check</span><b></b></div>`;
    return;
  }
  info.innerHTML = ytaRows(ytAccess);
  $("ytaMode").value = ytAccess.mode;
  $("ytaFile").value = ytAccess.file || "";
  ytaFillBrowsers(ytAccess);
  ytaSyncFields();

  if (ytAccess.pinned) {
    $("ytaMsg").innerHTML = `<div class="warn-box">A YTDLP_COOKIES environment variable is set, `
      + `and it wins over anything chosen here.</div>`;
  } else if (ytAccess.file_check && !ytAccess.file_check.ok) {
    $("ytaMsg").innerHTML = `<div class="err">${esc(ytAccess.file_check.reason)}</div>`;
  }
}

$("ytaMode").onchange = ytaSyncFields;

$("ytaRecheck").onclick = async () => {
  $("ytaRecheck").disabled = true;
  $("ytaMsg").innerHTML = "";
  try { await loadYtAccess(true); } finally { $("ytaRecheck").disabled = false; }
};

$("ytaSave").onclick = async () => {
  const mode = $("ytaMode").value;
  $("ytaSave").disabled = true;
  $("ytaMsg").innerHTML = "";
  try {
    ytAccess = await api("/api/youtube-access", json("POST", {
      mode,
      browser: $("ytaBrowser").value || "",
      file: $("ytaFile").value.trim(),
    }));
    $("ytaInfo").innerHTML = ytaRows(ytAccess);
    ytaFillBrowsers(ytAccess);
    ytaSyncFields();
    const check = ytAccess.file_check;
    $("ytaMsg").innerHTML = check && check.reason
      ? `<div class="warn-box">Saved. ${esc(check.reason)}</div>`
      : `<div class="ok-box">Saved.</div>`;
  } catch (e) {
    $("ytaMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    $("ytaSave").disabled = false;
  }
};

// ------------------------------------------------------------ post to YouTube
//
// Signing in happens in the user's real browser, on Google's page; this only
// starts it and then watches for it to finish. See webapp/youtube_upload.py.

let ytu = null;
let ytuWatch = null;

function ytuRows(s) {
  const rows = [];
  let who;
  if (s.connected) who = s.channel_title || "Connected";
  else if (s.connect_state === "waiting") who = "Waiting for you to sign in…";
  else who = "Not connected";
  rows.push(`<div><span>Channel</span><b>${esc(I18N.t(who))}</b></div>`);
  const client = s.client === "yours" ? "Your own"
    : s.client === "builtin" ? "ClipMint's" : "None set up yet";
  rows.push(`<div><span>Google client</span><b>${esc(I18N.t(client))}</b></div>`);
  return rows.join("");
}

function paintYtu() {
  const s = ytu;
  if (!s) return;
  $("ytuInfo").innerHTML = ytuRows(s);
  $("ytuConnect").hidden = s.connected;
  $("ytuDisconnect").hidden = !s.connected;
  $("ytuClientClear").hidden = s.client !== "yours";
  if (!s.client) $("ytuOwn").open = true;

  let hint;
  if (!s.client) {
    hint = "This build has no Google client of its own yet. Make a free one in Google Cloud "
         + "and paste it below — docs/youtube-upload.md walks through it in about ten minutes.";
  } else if (s.connected) {
    hint = "Open any clip, press Boost, and the YouTube tab has an Upload button. "
         + "You can withdraw access any time at myaccount.google.com/permissions.";
  } else {
    hint = "Your browser opens Google's sign-in. Pick the channel the clips should go to.";
  }
  $("ytuHint").textContent = I18N.t(hint);

  if (s.pinned) {
    $("ytuMsg").innerHTML = `<div class="warn-box">A YOUTUBE_CLIENT_ID environment variable is `
      + `set, and it wins over a client pasted here.</div>`;
  } else if (s.connect_state === "error" && s.connect_error) {
    $("ytuMsg").innerHTML = `<div class="err">${esc(s.connect_error)}</div>`;
  }
}

async function loadYtu() {
  try {
    ytu = await api("/api/youtube");
    paintYtu();
  } catch (_) { /* the rest of the drawer works without it */ }
  return ytu;
}

// Sign-in finishes in another window, so the only way to know is to ask.
function watchConnect() {
  clearInterval(ytuWatch);
  const until = Date.now() + 5 * 60 * 1000;
  ytuWatch = setInterval(async () => {
    const s = await loadYtu();
    if (!s || s.connect_state !== "waiting" || Date.now() > until) {
      clearInterval(ytuWatch);
      if (s && s.connected) {
        toast(`Connected to ${s.channel_title || "YouTube"}.`);
        if ($("player").classList.contains("seo-on")) renderUploadBox();
      }
    }
  }, 2000);
}

async function connectYouTube() {
  $("ytuMsg").innerHTML = "";
  try {
    ytu = await api("/api/youtube/connect", json("POST", {}));
    paintYtu();
    toast("Finish signing in in your browser.");
    watchConnect();
  } catch (e) {
    $("ytuMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
    toast(e.message, true);
  }
}
$("ytuConnect").onclick = connectYouTube;

$("ytuDisconnect").onclick = () => {
  ask("Disconnect YouTube?",
      "ClipMint gives its upload permission back to Google. Videos already uploaded stay "
      + "exactly where they are.",
      async () => {
        try {
          ytu = await api("/api/youtube/disconnect", json("POST", {}));
          $("ytuMsg").innerHTML = "";
          paintYtu();
          toast("YouTube disconnected.");
        } catch (e) {
          toast(e.message, true);
        }
      });
};

async function saveYtuClient(text) {
  $("ytuMsg").innerHTML = "";
  try {
    ytu = await api("/api/youtube/client", json("POST", { client: text }));
    $("ytuClient").value = "";
    paintYtu();
    $("ytuMsg").innerHTML = `<div class="ok-box">${esc(I18N.t(
      text ? "Saved. Now press Connect YouTube." : "Back to the built-in client."))}</div>`;
  } catch (e) {
    $("ytuMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}
$("ytuClientSave").onclick = () => {
  const text = $("ytuClient").value.trim();
  if (!text) { $("ytuMsg").innerHTML = `<div class="err">Paste the client JSON first.</div>`; return; }
  saveYtuClient(text);
};
$("ytuClientClear").onclick = () => saveYtuClient("");

// ---- the Upload box, under the YouTube boxes in Boost

const YU_PREF = "clipmint.ytupload";
const ytUploads = {};          // "job/file" -> upload id, while one is running

function yuPrefs() {
  try { return JSON.parse(localStorage.getItem(YU_PREF) || "{}") || {}; }
  catch (_) { return {}; }
}
function yuRemember(p) {
  try { localStorage.setItem(YU_PREF, JSON.stringify(p)); } catch (_) { /* a nicety */ }
}

// The next whole hour at least half an hour away, as a datetime-local value.
function yuDefaultWhen() {
  const d = new Date(Date.now() + 30 * 60 * 1000);
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() + 1);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function uploadBox() {
  return `<div class="yu-box" id="yuBox"></div>`;
}

function renderUploadBox() {
  const box = $("yuBox");
  const c = clips[cur];
  if (!box || !c) return;
  const s = ytu;
  const prefs = yuPrefs();
  const key = `${jobOf(c)}/${c.file}`;
  const done = c.youtube;

  const head = `<h4><svg><use href="#i-yt"/></svg>${esc(I18N.t("Post it to your channel"))}</h4>`;

  if (!s || !s.connected) {
    box.innerHTML = head + `
      <p class="seo-why">${esc(I18N.t(
        "Connect your channel once and clips go straight to YouTube with these words, now or on a schedule."))}</p>
      <button class="btn yt" id="yuConnect"><svg><use href="#i-yt"/></svg><span>${esc(I18N.t("Connect YouTube"))}</span></button>
      <div id="yuMsg"></div>`;
    $("yuConnect").onclick = async () => {
      if (!s || !s.client) { openDrawer(); return; }
      await connectYouTube();
    };
    return;
  }

  const when = done && done.publish_at ? new Date(done.publish_at) : null;
  const doneLine = done ? `
    <div class="yu-done">
      <span class="seo-why" style="margin:0">${esc(
        done.kept_private ? I18N.t("Uploaded, but YouTube kept it private.")
        : when ? `${I18N.t("Scheduled for")} ${when.toLocaleString()}.`
        : `${I18N.t("On YouTube")} (${I18N.t(done.privacy || "public")}).`)}</span>
      <button class="qbtn" data-yuopen="studio">${esc(I18N.t("Open in Studio"))}</button>
      <button class="qbtn" data-yuopen="watch">${esc(I18N.t("Watch"))}</button>
    </div>` : "";

  const cats = (s.categories || []).map(([id, name]) =>
    `<option value="${esc(id)}">${esc(I18N.t(name))}</option>`).join("");

  box.innerHTML = head + doneLine + `
    <p class="seo-why">${esc(I18N.t("Goes to"))} <b>${esc(s.channel_title || "your channel")}</b>
      ${esc(I18N.t("with the title, description and tags above, exactly as they are in the boxes."))}</p>
    <label class="fld"><span>${esc(I18N.t("Who can see it"))}</span>
      <select id="yuPrivacy">
        <option value="public">${esc(I18N.t("Public — now"))}</option>
        <option value="schedule">${esc(I18N.t("Scheduled — goes public at a time you pick"))}</option>
        <option value="unlisted">${esc(I18N.t("Unlisted — only people with the link"))}</option>
        <option value="private">${esc(I18N.t("Private — only you"))}</option>
      </select></label>
    <label class="fld" id="yuWhenFld" hidden><span>${esc(I18N.t("Goes public at (your time)"))}</span>
      <input type="datetime-local" id="yuWhen"></label>
    <label class="fld"><span>${esc(I18N.t("Category"))}</span><select id="yuCat">${cats}</select></label>
    <label class="check"><input type="checkbox" id="yuKids"><span>${esc(I18N.t(
      "Made for kids — YouTube's legal question, answer it honestly"))}</span></label>
    <button class="btn yt" id="yuGo"><svg><use href="#i-yt"/></svg><span>${esc(I18N.t(
      done ? "Upload again" : "Upload to YouTube"))}</span></button>
    <div class="yu-prog" id="yuProg" hidden><div class="bar"><i id="yuBar"></i></div><p id="yuProgText"></p></div>
    <div id="yuMsg"></div>`;

  $("yuPrivacy").value = prefs.privacy || "public";
  $("yuCat").value = (s.categories || []).some(([id]) => id === prefs.category)
    ? prefs.category : "22";
  $("yuKids").checked = !!prefs.kids;
  $("yuWhen").value = yuDefaultWhen();
  const syncWhen = () => { $("yuWhenFld").hidden = $("yuPrivacy").value !== "schedule"; };
  $("yuPrivacy").onchange = syncWhen;
  syncWhen();

  box.querySelectorAll("[data-yuopen]").forEach((b) => {
    b.onclick = () => api("/api/youtube/open",
      json("POST", { video_id: done.video_id, where: b.dataset.yuopen })).catch((e) => toast(e.message, true));
  });

  $("yuGo").onclick = () => startYouTubeUpload(c);

  if (ytUploads[key]) followUpload(key, ytUploads[key]);
}

async function startYouTubeUpload(c) {
  const privacy = $("yuPrivacy").value;
  let publishAt = null;
  if (privacy === "schedule") {
    const v = $("yuWhen").value;
    const t = v ? new Date(v) : null;
    if (!t || isNaN(t)) { $("yuMsg").innerHTML = `<div class="err">Pick when it should go public.</div>`; return; }
    publishAt = t.toISOString();
  }
  const prefs = { privacy, category: $("yuCat").value, kids: $("yuKids").checked };
  yuRemember(prefs);

  const key = `${jobOf(c)}/${c.file}`;
  $("yuMsg").innerHTML = "";
  $("yuGo").disabled = true;
  try {
    const st = await api(
      `/api/jobs/${encodeURIComponent(jobOf(c))}/clips/${encodeURIComponent(c.file)}/youtube`,
      json("POST", {
        title: $("sfTitle").value,
        description: $("sfDesc").value,
        tags: $("sfTags").value,
        privacy: privacy === "schedule" ? "private" : privacy,
        publish_at: publishAt,
        made_for_kids: prefs.kids,
        category: prefs.category,
      }));
    ytUploads[key] = st.id;
    followUpload(key, st.id);
  } catch (e) {
    $("yuGo").disabled = false;
    $("yuMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

// One watcher per upload. It keeps going when the panel closes or another clip
// opens, and only paints the box while that box is showing its clip.
const yuFollowing = new Set();
function followUpload(key, id) {
  const showing = () => {
    const c = clips[cur];
    return c && `${jobOf(c)}/${c.file}` === key && $("yuBox") ? c : null;
  };
  const paint = (st) => {
    if (!showing()) return;
    const pct = st.size ? Math.floor((st.sent / st.size) * 100) : 0;
    $("yuGo").disabled = true;
    $("yuProg").hidden = false;
    $("yuBar").style.transform = `scaleX(${st.size ? st.sent / st.size : 0})`;
    const label = st.state === "checking" ? "Checking what YouTube did with it…"
      : st.state === "uploading" ? `${I18N.t("Uploading")} ${pct}%`
      : "Starting the upload…";
    $("yuProgText").textContent = st.note || I18N.t(label);
  };
  if (yuFollowing.has(id)) return;
  yuFollowing.add(id);

  const tick = async () => {
    let st;
    try { st = await api(`/api/youtube/uploads/${encodeURIComponent(id)}`); }
    catch (_) { yuFollowing.delete(id); delete ytUploads[key]; return; }

    if (st.state === "done" || st.state === "error") {
      yuFollowing.delete(id);
      delete ytUploads[key];
      if (st.state === "done") {
        const r = st.result;
        const i = clips.findIndex((x) => `${jobOf(x)}/${x.file}` === key);
        if (i >= 0) clips[i].youtube = r;
        const where = r.channel_title ? ` to ${r.channel_title}` : "";
        toast(r.kept_private ? "Uploaded, but YouTube kept it private."
              : r.publish_at ? `Scheduled${where}.` : `Uploaded${where}.`, r.kept_private);
        if (showing()) {
          renderUploadBox();
          if (r.kept_private) {
            $("yuMsg").innerHTML = `<div class="warn-box">${esc(I18N.t(
              "YouTube locks uploads from a Google project that hasn't passed its API audit to private, and drops any schedule. You can make it public in Studio. The setup guide explains the audit."))}</div>`;
          }
        }
      } else {
        toast(st.error, true);
        if (showing()) {
          $("yuProg").hidden = true;
          $("yuGo").disabled = false;
          $("yuMsg").innerHTML = `<div class="err">${esc(st.error)}</div>`;
        }
        if (st.reconnect) loadYtu();
      }
      return;
    }
    paint(st);
    setTimeout(tick, 1000);
  };
  tick();
}

// ---------------------------------------------------------------- locations

let locations = null;

async function loadLocations() {
  try {
    locations = await api("/api/locations");
    if (!$("locPath").value) $("locPath").value = locations.root;
  } catch (_) { /* the drawer still works without it */ }
}

$("locSave").onclick = async () => {
  const path = $("locPath").value.trim();
  if (!path) { $("locMsg").innerHTML = `<div class="err">Enter a folder first.</div>`; return; }
  $("locSave").disabled = true;
  try {
    locations = await api("/api/locations", json("POST", { path }));
    $("locPath").value = locations.root;
    $("locMsg").innerHTML = `<div class="ok-box">Saved. New clips land in ${esc(locations.shorts)}</div>`;
  } catch (e) {
    $("locMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    $("locSave").disabled = false;
  }
};

let cleanup = null;

function humanBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

async function loadCleanup() {
  const info = $("cleanInfo"), btn = $("cleanBtn");
  try {
    cleanup = await api("/api/cleanup");
  } catch (_) {
    info.innerHTML = `<div><span>Couldn't check the folder</span></div>`;
    btn.disabled = true;
    return;
  }
  if (!cleanup.count) {
    info.innerHTML = `<div><span>Nothing to clear</span><b>0 B</b></div>`;
    btn.disabled = true;
    return;
  }
  const partials = cleanup.items.filter(i => i.kind === "partial").length;
  const sources = cleanup.count - partials;
  const rows = [`<div><span>Source videos</span><b>${sources}</b></div>`];
  if (partials) rows.push(`<div><span>Unfinished downloads</span><b>${partials}</b></div>`);
  rows.push(`<div><span>Frees up</span><b>${humanBytes(cleanup.bytes)}</b></div>`);
  info.innerHTML = rows.join("");
  btn.disabled = false;
}

$("cleanBtn").onclick = () => {
  if (!cleanup || !cleanup.count) return;
  ask(
    "Clear space?",
    `${cleanup.count} file${cleanup.count === 1 ? "" : "s"} will be deleted, freeing ${humanBytes(cleanup.bytes)}. `
      + `Your clips stay where they are. Making more shorts from the same video will download it again.`,
    async () => {
      $("cleanBtn").disabled = true;
      $("cleanMsg").innerHTML = "";
      try {
        const r = await api("/api/cleanup", json("POST", {}));
        toast(`Freed ${humanBytes(r.freed)}`);
        if (r.failed.length) {
          $("cleanMsg").innerHTML = `<div class="err">${esc(r.failed.join("; "))}</div>`;
        }
      } catch (e) {
        $("cleanMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
      } finally {
        await loadCleanup();
      }
    }
  );
};

async function openFolder(path) {
  try {
    if (!locations) await loadLocations();
    await api("/api/reveal",
              json("POST", { path: typeof path === "string" && path ? path : locations.shorts }));
  } catch (e) {
    toast(e.message, true);
  }
}
$("folderBtn").onclick = () => openFolder();
$("folderBtn2").onclick = () => openFolder();
$("gFolder").onclick = () => openFolder();

async function openYouTubeUpload() {
  try {
    await api("/api/open-upload", json("POST", {}));
    toast("YouTube's upload page is open in your browser.");
  } catch (e) {
    toast(e.message, true);
  }
}
$("uploadBtn").onclick = openYouTubeUpload;
$("gUpload").onclick = openYouTubeUpload;

// The author's own links. Opened by name through the same allowlist as the
// upload page -- the page never hands the server a URL to launch.
async function openExternal(what) {
  try {
    await api(`/api/open-upload?what=${encodeURIComponent(what)}`, json("POST", {}));
  } catch (e) {
    toast(e.message, true);
  }
}
$("creditYt").onclick = (e) => { e.preventDefault(); openExternal("author-youtube"); };
$("creditGh").onclick = (e) => { e.preventDefault(); openExternal("author-github"); };
$("creditLi").onclick = (e) => { e.preventDefault(); openExternal("author-linkedin"); };
$("creditDc").onclick = (e) => { e.preventDefault(); openExternal("author-discord"); };
$("creditDonate").onclick = (e) => { e.preventDefault(); openExternal("donate"); };

// ---------------------------------------------------------------- welcome
//
// Shown at every launch. Nothing is remembered about it: the app introduces
// itself and asks once per session, and the sidebar credit opens it again.
// It is a plain overlay rather than a native dialog because the packaged app
// runs inside a WebView2 window -- there is nothing else on screen to be
// modal against, and <dialog>'s own backdrop cannot take the page's blur.

let welcomeReturn = null;   // what to focus once it closes

function openWelcome() {
  const w = $("welcome");
  welcomeReturn = document.activeElement;
  w.hidden = false;
  // Force layout between unhiding and the class, or the transition has no
  // starting point and the card just appears. A rAF would do the same, but a
  // window that is occluded at launch never runs one -- and the failure there
  // is a fully opaque overlay stuck at zero opacity, blocking a page the user
  // can still see through it.
  void w.offsetWidth;
  w.classList.add("on");
  body.classList.add("welcome-on");
  $("wStart").focus();
}

function closeWelcome() {
  const w = $("welcome");
  if (w.hidden) return;
  w.classList.remove("on");
  body.classList.remove("welcome-on");
  // Hide only once it has faded, so the card is not yanked off screen.
  setTimeout(() => { w.hidden = true; }, 280);
  if (welcomeReturn && welcomeReturn.focus) welcomeReturn.focus();
  welcomeReturn = null;
}

$("wClose").onclick = closeWelcome;
$("wStart").onclick = closeWelcome;
$("wBack").onclick = closeWelcome;
$("gAbout").onclick = openWelcome;

// Same allowlist as the masthead icons -- the button carries a name, not a URL.
// Any element carrying data-open, not just the launch card's tiles.
document.querySelectorAll("[data-open]").forEach((b) => {
  b.onclick = () => openExternal(b.dataset.open);
});
$("wDonate").onclick = () => openExternal("donate");

// Copied rather than handed to a mail client: plenty of Windows installs have
// no default mail app, and a mailto: that opens nothing looks like a dead
// button. The clipboard works everywhere and says so.
$("wMail").onclick = () => copy("parthbhadana57@gmail.com", "Email address copied");

// Tab has to come back round inside the card while it is open, or focus walks
// off into a page the user cannot see.
$("welcome").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeWelcome(); return; }
  if (e.key !== "Tab") return;
  const stops = $("welcome").querySelectorAll("button");
  const first = stops[0], last = stops[stops.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

// ---------------------------------------------------------------- updates
//
// The app is a single .exe people download once, so without this a fix ships
// and never reaches them. The check is one request to GitHub at launch; it
// stays silent unless there is something to say, and silent if it fails --
// an app that cannot reach GitHub is still a working app.
//
// The page never handles a download URL. It asks the server to install "the
// update" and the server resolves which file that is from the repository
// compiled into the build.

let updateState = null;
let updatePoll = null;
let announcedVersion = null;   // the version we have already mentioned
let lastCheckAt = 0;

// Shown from a local endpoint rather than waiting on the update check, so the
// build number is there with no network and is the first thing to hand when
// someone reports a bug.
async function showVersion() {
  try {
    const d = await api("/api/version");
    $("wVer").textContent = "v" + d.version;
    $("gVer").textContent = "Version " + d.version;
    $("topVer").textContent = "v" + d.version;
  } catch (e) { /* the number is not worth an error */ }
}
showVersion();

function fmtMB(bytes) {
  return bytes ? Math.round(bytes / 1e6) + " MB" : "";
}

function showUpdate(title, sub, { bar = false, actions = true } = {}) {
  $("wUpdate").classList.remove("hidden");
  $("wuTitle").textContent = title;
  $("wuSub").textContent = sub || "";
  $("wuBar").classList.toggle("hidden", !bar);
  $("wuActions").classList.toggle("hidden", !actions);
}

async function checkUpdate(loud) {
  try {
    const d = await api("/api/update/check");
    updateState = d;
    renderVersionRow(d);

    const waiting = d.status === "update";
    $("updateDot").classList.toggle("hidden", !waiting);
    $("gUpdateBadge").classList.toggle("hidden", !waiting);

    // Someone who leaves the app open all day would otherwise only ever learn
    // about a release by restarting. Announced once per version, so a long
    // session does not get nagged every half hour about the same build.
    if (waiting && d.latest && d.latest !== announcedVersion) {
      announcedVersion = d.latest;
      if (!loud) toast(`Version ${d.latest} is out — click the update button to install it.`);
    }

    if (waiting || d.status === "ahead") {
      const ahead = d.status === "ahead";
      const verb = ahead ? "Install" : "Version";
      showUpdate(ahead ? `${verb} ${d.latest}?`
                       : `${verb} ${d.latest} is available`,
                 ahead ? `You are on ${d.current}, which is newer. ${fmtMB(d.size)} download.`
                       : `You have ${d.current}. ${fmtMB(d.size)} download.`);
      $("wuGo").textContent = ahead ? `Go back to ${d.latest}` : "Update now";
      $("wuGo").disabled = !d.can_install;
      if (!d.can_install && d.reason) $("wuSub").textContent = d.reason;
    } else if (d.status === "unavailable" && d.newer) {
      // Only when there is genuinely something newer. This build being ahead
      // of the last release is not news worth a banner.
      showUpdate(`Version ${d.latest} is available`, d.reason);
      $("wuGo").textContent = "Open the releases page";
      $("wuGo").disabled = false;
      $("wuGo").onclick = () => openExternal("releases");
      $("wuNotes").classList.add("hidden");
      if (loud) openWelcome();
    } else if (loud) {
      toast(d.status === "current"
        ? `You are on the latest version (${d.current}).`
        : (d.reason || "Could not check for updates."));
    }
  } catch (e) {
    if (loud) toast(e.message, true);
  }
}

function renderVersionRow(d) {
  const el = $("verInfo");
  if (!el) return;
  const rows = [["Installed", d.current]];
  if (d.latest) rows.push(["Newest release", d.latest]);
  if (d.reason) rows.push(["Note", d.reason]);
  el.innerHTML = rows.map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");
}

function stopUpdatePoll() {
  if (updatePoll) { clearInterval(updatePoll); updatePoll = null; }
}

async function pollUpdate() {
  let p;
  try {
    p = await api("/api/update/progress");
  } catch (e) {
    return;                     // a dropped poll is not a failed download
  }
  if (p.state === "downloading") {
    showUpdate("Downloading the update…",
               `${fmtMB(p.done)} of ${fmtMB(p.total)}`, { bar: true, actions: false });
    $("wuFill").style.width = p.percent + "%";
  } else if (p.state === "verifying") {
    showUpdate("Checking the download…", "Making sure it arrived intact.",
               { bar: true, actions: false });
    $("wuFill").style.width = "100%";
  } else if (p.state === "ready") {
    stopUpdatePoll();
    showUpdate("Ready to install",
               "The app will close and reopen on the new version.");
    $("wuGo").disabled = false;
    $("wuGo").textContent = "Restart now";
    $("wuGo").onclick = applyUpdate;
  } else if (p.state === "failed") {
    stopUpdatePoll();
    showUpdate("The update could not be downloaded", p.error || "");
    $("wuGo").disabled = false;
    $("wuGo").textContent = "Try again";
    $("wuGo").onclick = startUpdate;
  }
}

async function startUpdate() {
  $("wuGo").disabled = true;
  try {
    await api("/api/update/install", json("POST", {}));
    showUpdate("Starting the download…", "", { bar: true, actions: false });
    stopUpdatePoll();
    updatePoll = setInterval(pollUpdate, 600);
  } catch (e) {
    $("wuGo").disabled = false;
    showUpdate("The update could not be started", e.message);
  }
}

async function applyUpdate() {
  $("wuGo").disabled = true;
  showUpdate("Restarting…", "The window will close and come back.",
             { bar: false, actions: false });
  try {
    await api("/api/update/apply", json("POST", {}));
  } catch (e) {
    showUpdate("Could not install the update", e.message);
    $("wuGo").disabled = false;
  }
}

$("wuGo").onclick = startUpdate;
$("wuNotes").onclick = () => openExternal("releases");

if ($("verCheck")) {
  $("verCheck").onclick = () => checkUpdate(true);
}

// Asking from the masthead or the sidebar re-checks, then puts the answer
// where the answer lives -- on the launch card, next to the button that acts
// on it -- rather than leaving the user to find it.
async function checkUpdateAndShow() {
  toast("Checking for updates…");
  await checkUpdate(true);
  if (updateState && (updateState.status === "update" || updateState.status === "rollback")) {
    openWelcome();
    $("wUpdate").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}
$("updateBtn").onclick = checkUpdateAndShow;
$("gUpdate").onclick = checkUpdateAndShow;

function checkUpdateQuietly() {
  // GitHub allows 60 unauthenticated calls an hour and this is one app on one
  // PC, so half-hourly is nowhere near the limit -- but the guard keeps a
  // window that is focused and blurred repeatedly from turning into a stream
  // of requests.
  const now = Date.now();
  if (now - lastCheckAt < 10 * 60 * 1000) return;
  lastCheckAt = now;
  checkUpdate(false);
}

lastCheckAt = Date.now();
checkUpdate(false);

// While the app is open: every half hour, and whenever the window is looked
// at again after being away. A release published at noon reaches someone who
// opened the app at nine without them restarting it.
setInterval(checkUpdateQuietly, 30 * 60 * 1000);
window.addEventListener("focus", checkUpdateQuietly);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) checkUpdateQuietly();
});

// ------------------------------------------------------------- attention
//
// Tell the server whether anyone is actually looking at this, so a run that
// finishes while nobody is raises a desktop notification and one that
// finishes in front of you does not.
//
// Sent as a heartbeat rather than once per change, because the interesting
// case is the window that went away without saying anything — closed, or a
// browser tab shut mid-render. Silence is the signal there, and only a
// heartbeat has any.
function reportAttention() {
  const watching = !document.hidden && document.hasFocus();
  fetch("/api/attention", json("POST", { watching })).catch(() => {});
}

document.addEventListener("visibilitychange", reportAttention);
window.addEventListener("focus", reportAttention);
window.addEventListener("blur", reportAttention);
setInterval(reportAttention, 20 * 1000);
reportAttention();

openWelcome();

// ---------------------------------------------------------------- source

// ---------------------------------------------------------------- pause
//
// Rendering pins every core it can, and a machine that has gone unusable is
// the most common reason someone kills a run half way. Pausing suspends the
// ffmpeg processes rather than waiting for the current clip to finish, so the
// CPU comes back immediately.

let paused = false;

function renderPauseState(on) {
  paused = !!on;
  body.classList.toggle("job-paused", paused);
  $("pauseLabel").textContent = paused ? "Resume" : "Pause";
  $("pauseBtn").querySelector("use")
    .setAttribute("href", paused ? "#i-play" : "#i-pause");
  $("pauseHint").textContent = paused
    ? "Stopped. Your CPU is free — nothing is lost, the run picks up where it left off."
    : "Rendering uses every core it can. Pause if you need the machine.";
}

$("pauseBtn").onclick = async () => {
  if (!jobId) return;
  const btn = $("pauseBtn");
  btn.disabled = true;
  try {
    const d = await api(`/api/jobs/${jobId}/${paused ? "resume" : "pause"}`, json("POST", {}));
    renderPauseState(d.paused);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

function setSource(src, name) {
  source = { source: src, name: name || src };
  $("srcName").textContent = source.name;
  $("srcTag").classList.remove("hidden");
  $("go").disabled = false;
  $("go").textContent = "Generate shorts";
}

function clearSource() {
  source = null;
  $("srcTag").classList.add("hidden");
  $("chanWrap").classList.add("hidden");
  $("go").disabled = true;
  $("go").textContent = "Add a source first";
  $("url").value = "";
  $("urlClear").classList.add("hidden");
}

function showErr(msg) {
  $("srcErr").innerHTML = msg ? `<div class="err">${esc(msg)}</div>` : "";
}

async function loadUrl() {
  const url = $("url").value.trim();
  if (!url) return;
  showErr("");
  $("load").disabled = true;
  loadbar(0.4);
  try {
    const d = await api("/api/resolve", json("POST", { url }));
    if (d.type === "video") {
      $("chanWrap").classList.add("hidden");
      setSource(d.source, url);
    } else {
      renderChannel(d.videos);
    }
    $("secCreate").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    showErr(e.message);
  } finally {
    $("load").disabled = false;
    loadbar(1);
    setTimeout(() => loadbar(null), 400);
  }
}

function renderChannel(videos) {
  $("chanWrap").classList.remove("hidden");
  $("vids").innerHTML = videos.map((v, i) => `
    <div class="vid" data-i="${i}">
      ${v.thumbnail
        ? `<img src="${esc(v.thumbnail)}" alt="" loading="lazy">`
        : `<div class="vthumb-none"><svg><use href="#i-film"/></svg></div>`}
      <div class="vt">${esc(v.title)}</div>
      <div class="vd">${fmtDur(v.duration)}</div>
    </div>`).join("");

  [...$("vids").children].forEach((el) => {
    el.onclick = () => {
      [...$("vids").children].forEach((o) => o.classList.remove("sel"));
      el.classList.add("sel");
      const v = videos[+el.dataset.i];
      setSource(v.url, v.title);
    };
  });
}

async function uploadFile(file) {
  showErr("");
  const drop = $("drop");
  const big = drop.querySelector(".big");
  drop.classList.add("busy");
  big.textContent = `Copying ${file.name}…`;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const d = await api("/api/upload", { method: "POST", body: fd });
    setSource(d.source, `${d.name} · ${fmtSize(d.size)}`);
    $("chanWrap").classList.add("hidden");
  } catch (e) {
    showErr(e.message);
  } finally {
    drop.classList.remove("busy");
    big.textContent = "Drag a video here";
  }
}

$("drop").onclick = () => $("file").click();
$("pickFile").onclick = () => $("file").click();
$("file").onchange = (e) => e.target.files[0] && uploadFile(e.target.files[0]);

["dragenter", "dragover"].forEach((ev) =>
  $("drop").addEventListener(ev, (e) => { e.preventDefault(); $("drop").classList.add("over"); }));
["dragleave", "drop"].forEach((ev) =>
  $("drop").addEventListener(ev, (e) => { e.preventDefault(); $("drop").classList.remove("over"); }));
$("drop").addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) uploadFile(f);
});

$("load").onclick = loadUrl;
$("url").addEventListener("keydown", (e) => { if (e.key === "Enter") loadUrl(); });
$("url").addEventListener("input", () =>
  $("urlClear").classList.toggle("hidden", !$("url").value));
$("urlClear").onclick = () => {
  $("url").value = "";
  $("urlClear").classList.add("hidden");
  $("url").focus();
};
$("srcClear").onclick = clearSource;

// ---------------------------------------------------------------- layout

function drawPreview(spec, summary, notes, warning) {
  const maxH = 300, maxW = 250;
  const ratio = spec.width / spec.height;
  let h = maxH, w = h * ratio;
  if (w > maxW) { w = maxW; h = w / ratio; }

  const frame = $("frame");
  frame.style.width = Math.round(w) + "px";
  frame.style.height = Math.round(h) + "px";

  const stacked = spec.layout === "stacked";
  const cam = $("pvCam");
  cam.style.display = stacked ? "flex" : "none";
  if (stacked) cam.style.height = Math.round(h * spec.cam_panel_fraction) + "px";

  $("pvGame").textContent =
    spec.layout === "facetrack" ? "face-tracked crop"
    : spec.layout === "center" ? "centre crop"
    : "gameplay";

  $("dims").textContent = `${spec.width} × ${spec.height}`;
  $("summary").textContent = summary;
  $("notes").innerHTML = (notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
  $("warn").innerHTML = warning ? `<div class="warn-box">${esc(warning)}</div>` : "";

  const ranges = spec.time_ranges || [];
  $("exact").innerHTML = ranges.length
    ? `<div class="exact-box"><b>Exact cuts — no AI ranking</b>${
        ranges.map((r) => `<div>${clock(r[0])} → ${clock(r[1])} <span>(${Math.round(r[1] - r[0])}s)</span></div>`).join("")
      }</div>`
    : "";
}

// "" means the prompt decides, which is how this behaved before the toggle.
let aspectChoice = "";
// Same for the kind of video: "" leaves it to the words, then to the ranker.
let kindChoice = "";

async function refreshPreview() {
  try {
    const d = await api("/api/layout/preview", json("POST", {
      prompt: $("prompt").value, use_llm: true, aspect_ratio: aspectChoice || null,
      content_kind: kindChoice || null,
    }));
    drawPreview(d.spec, d.summary, d.notes, d.warning);
  } catch (_) { /* the preview is cosmetic — never block on it */ }
}

$("arBar").onclick = (e) => {
  const btn = e.target.closest("[data-ar]");
  if (!btn) return;
  aspectChoice = btn.dataset.ar;
  for (const b of $("arBar").querySelectorAll(".chip")) b.classList.toggle("on", b === btn);
  refreshPreview();   // the preview is the only proof the pick landed
};

// A vlog or a podcast moves the preview onto the face, so this redraws too.
$("kindBar").onclick = (e) => {
  const btn = e.target.closest("[data-kind]");
  if (!btn) return;
  kindChoice = btn.dataset.kind;
  for (const b of $("kindBar").querySelectorAll(".chip")) b.classList.toggle("on", b === btn);
  refreshPreview();
};

$("chips").innerHTML = EXAMPLES.map((e) =>
  `<button class="chip" data-t="${esc(e)}">${esc(e)}</button>`).join("");

function syncChips() {
  const text = $("prompt").value.toLowerCase();
  [...$("chips").children].forEach((c) =>
    c.classList.toggle("on", text.includes(c.dataset.t.toLowerCase())));
}

[...$("chips").children].forEach((c) => {
  c.onclick = () => {
    const t = $("prompt");
    const phrase = c.dataset.t;
    const has = t.value.toLowerCase().includes(phrase.toLowerCase());
    if (has) {
      // Toggling off should not leave a stray comma behind.
      t.value = t.value.replace(new RegExp(`\\s*,?\\s*${phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`, "i"), "")
                       .replace(/^\s*,\s*/, "").trim();
    } else {
      t.value = t.value.trim() ? `${t.value.trim()}, ${phrase}` : phrase;
    }
    syncChips();
    refreshPreview();
  };
});

$("prompt").addEventListener("input", () => {
  syncChips();
  clearTimeout(specTimer);
  specTimer = setTimeout(refreshPreview, 450);
});

// ---------------------------------------------------------------- run

async function run() {
  if (!source) return;
  $("go").disabled = true;
  $("go").textContent = "Working…";
  // The clips already on disk stay on screen while the new run works.
  $("log").textContent = "";
  $("progress").classList.remove("hidden");
  $("progress").scrollIntoView({ behavior: "smooth", block: "center" });
  showErr("");
  closeMini();

  let job;
  try {
    job = await api("/api/jobs", json("POST", {
      source: source.source,
      prompt: $("prompt").value,
      download_format: $("format").value,
      language: $("spokenLang").value,
      aspect_ratio: aspectChoice || null,
      content_kind: kindChoice || null,
      hook_replay: $("hookReplay").checked,
    }));
  } catch (e) {
    showErr(e.message);
    $("go").disabled = false;
    $("go").textContent = "Generate shorts";
    return;
  }

  follow(job.id);
}
$("go").onclick = run;

// Watch a job to the end: a new run, or an old one sent round again.
function follow(id) {
  jobId = id;
  finished = null;
  $("retryRow").classList.add("hidden");
  if (es) es.close();
  es = new EventSource(`/api/jobs/${id}/stream`);
  es.onmessage = (ev) => onUpdate(JSON.parse(ev.data));
  es.onerror = () => {
    // The stream drops when the job ends; fall back to one direct read.
    es.close();
    fetch(`/api/jobs/${id}`).then((r) => r.json()).then(onUpdate).catch(() => {});
  };
}

// A run that failed, or finished with clips missing, says so and offers to go
// again. Every stage has already retried itself by the time this shows, so
// what is left is usually something that needed fixing first -- and the
// caches mean the second go starts where the first stopped.
function offerRetry(s, made) {
  const failed = s.failed || 0;
  let mode = null;
  if (s.status === "error" && !made) mode = "run";
  else if (failed && s.source_on_disk) mode = "clips";
  $("retryRow").classList.toggle("hidden", !mode);
  if (!mode) return;
  $("retryLabel").textContent = mode === "run" ? "Try again" : "Retry failed clips";
  $("retryHint").textContent = mode === "run"
    ? "Starts where it stopped: nothing already downloaded, transcribed or ranked is done twice."
    : `${failed} clip${failed > 1 ? "s" : ""} did not render. Only ${
        failed > 1 ? "those are" : "that one is"} rendered again.`;
  $("retryBtn").onclick = () => retryRun(s.id, mode === "clips");
}

async function retryRun(id, clipsOnly) {
  $("retryBtn").disabled = true;
  showErr("");
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/retry${clipsOnly ? "?clips_only=true" : ""}`,
              { method: "POST" });
    $("go").disabled = true;
    $("go").textContent = "Working…";
    $("progress").classList.remove("hidden");
    follow(id);
  } catch (e) {
    showErr(e.message);
  } finally {
    $("retryBtn").disabled = false;
  }
}

function onUpdate(s) {
  // The server is the authority on this: a pause survives a page reload, and
  // the button has to agree with what the worker is actually doing.
  if (s.paused !== undefined && s.paused !== paused) renderPauseState(s.paused);
  $("pauseBtn").classList.toggle("hidden", s.status !== "running");
  $("stageLabel").textContent = s.message || s.stage_label;
  $("pct").textContent = Math.round(s.progress * 100) + "%";
  $("barFill").style.transform = `scaleX(${Math.max(0, Math.min(1, s.progress))})`;
  loadbar(s.progress);

  const at = STEPS.findIndex(([k]) => k === s.stage);
  $("steps").innerHTML = STEPS.map(([k, label], i) => {
    const cls = s.status === "done" || (at > -1 && i < at) ? "done" : (i === at ? "now" : "");
    // The stage name carries its own animation while it is the live one —
    // four long stages with one identical dot between them is what made a
    // run feel stuck even when it was moving.
    return `<span class="st st-${k} ${cls}">${label}</span>`;
  }).join("");

  // A clock and a guess, so a quiet stage still shows something changing.
  clockFrom(s);

  if (s.log && s.log.length) {
    const el = $("log");
    const stuck = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = s.log.join("\n");
    if (stuck) el.scrollTop = el.scrollHeight;
  }
  if (s.status === "done" || s.status === "error") { renderPauseState(false); finish(s); }
}

// ---------------------------------------------------------------- the clock

// Elapsed time and a guess at what is left. The server sends both, but only
// when something changes — which during transcription can be seconds apart —
// so the clock ticks locally from the last figure it was given.
let clockAt = 0, clockElapsed = 0, clockEta = null, clockRunning = false, clockTimer = null;

function humanTime(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

// Round the estimate to something honest: "about 7 min", not "6m 51s", which
// claims a precision a guess does not have.
function humanEta(sec) {
  if (sec < 45) return "under a minute left";
  if (sec < 90) return "about a minute left";
  if (sec < 3600) return `about ${Math.round(sec / 60)} min left`;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return `about ${h}h ${m}m left`;
}

function clockFrom(s) {
  clockAt = Date.now();
  clockElapsed = s.elapsed || 0;
  clockEta = s.eta_seconds != null ? s.eta_seconds : null;
  clockRunning = s.status === "running";
  paintClock();
  if (!clockTimer) clockTimer = setInterval(paintClock, 1000);
  if (!clockRunning) { clearInterval(clockTimer); clockTimer = null; }
}

function paintClock() {
  const since = (Date.now() - clockAt) / 1000;
  const elapsed = clockElapsed + (clockRunning ? since : 0);
  const eta = clockEta == null ? null : Math.max(0, clockEta - since);
  const parts = [];
  if (elapsed > 0) parts.push(`${humanTime(elapsed)} so far`);
  if (clockRunning && eta != null) parts.push(humanEta(eta));
  $("pTime").textContent = parts.join(" · ");
}

// ---------------------------------------------------------------- carrying on

// A run that was going when the app closed. Everything it had already done is
// on disk, so it is offered back rather than lost.
async function checkInterrupted() {
  try {
    const d = await api("/api/jobs");
    const stuck = (d.jobs || []).filter((j) => j.resumable);
    const bar = $("resumeBar");
    if (!stuck.length) { bar.classList.add("hidden"); return; }
    const j = stuck[0];
    // A local run has no video title, and a full path is unreadable in a
    // sentence — the file's own name is what someone recognises.
    const what = j.source_title || (j.source || "").split(/[\\/]/).pop() || "your video";
    bar.innerHTML = `
      <div class="rb-text">
        <b>A run didn't finish.</b>
        ${esc(what)} — it stopped while ${esc((j.stage_label || "working").toLowerCase())}.
        Carrying on skips everything it already did.
      </div>
      <div class="rb-act">
        <button class="btn primary sm" id="rbGo">Resume</button>
        <button class="btn ghost sm" id="rbNo">Dismiss</button>
      </div>`;
    bar.classList.remove("hidden");
    $("rbNo").onclick = () => bar.classList.add("hidden");
    $("rbGo").onclick = async () => {
      $("rbGo").disabled = true;
      try {
        await api(`/api/jobs/${encodeURIComponent(j.id)}/continue`, { method: "POST" });
        bar.classList.add("hidden");
        $("go").disabled = true;
        $("go").textContent = "Working…";
        $("progress").classList.remove("hidden");
        $("progress").scrollIntoView({ behavior: "smooth", block: "center" });
        follow(j.id);
      } catch (e) {
        showErr(e.message);
        $("rbGo").disabled = false;
      }
    };
  } catch (_) { /* an offline check is not worth blocking the page for */ }
}

let finished = null;

async function finish(s) {
  // The stream's last message and the fallback read can both land here.
  if (finished === s.id) return;
  finished = s.id;
  if (es) { es.close(); es = null; }
  $("go").disabled = false;
  $("go").textContent = "Generate shorts";
  setTimeout(() => loadbar(null), 600);
  // The run just spent requests — including the failed ones, which is exactly
  // when knowing what is left matters most.
  refreshUsage();
  // And it may have changed what the heavy work runs on: an encoder that
  // failed mid-run is dropped for that run, and the panel should say so.
  loadProcessor();

  const made = (s.clips || []).filter((c) => c.url).length;
  offerRetry(s, made);
  if (s.error && !made) {
    showErr(s.error);
    return;
  }

  // Reload the whole library rather than showing this run alone, so the new
  // clips land at the top of everything already made.
  await loadLibrary();
  if (!made) return;

  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
  // The whole point of the wait: show the best clip playing, straight away.
  const first = clips.findIndex((c) => c.job_id === s.id);
  setTimeout(() => openMini(first > -1 ? first : 0), 700);
  toast(`${made} clip${made > 1 ? "s" : ""} ready, ranked best first — open one for its title and tags.`);
}

// ---------------------------------------------------------------- results

// Everything ever rendered that is still on disk — not just this session's run.
async function loadLibrary() {
  try {
    const d = await api("/api/library");
    runs = d.runs || [];
  } catch (_) {
    runs = [];
  }
  clips = [];
  runs.forEach((r) => r.clips.forEach((c) => {
    c.job_id = c.job_id || r.id;
    // The full path on disk, for the "Show file" tooltip.
    if (r.shorts_dir && c.file) {
      c.path = r.shorts_dir + (r.shorts_dir.includes("\\") ? "\\" : "/") + c.file;
    }
    c._hay = haystack(c, r);
    clips.push(c);
  }));
  reindex();
  renderClips();
  return clips.length;
}

// Every card carries its own position in `clips`, so filtering the grid can
// never make a card open the wrong clip. Anything that reorders or removes
// from `clips` has to call this.
function reindex() {
  clips.forEach((c, i) => { c._i = i; });
}

// Everything a clip could be remembered by. You rarely recall which field the
// phrase you are searching for actually lives in, so they all match.
function haystack(c, r) {
  const seo = c.seo || {};
  return [
    c.title, seo.title, seo.description, seo.hook_text,
    c.hook_sentence, c.first_line, c.virality_reason,
    (seo.about || {}).subject, (seo.about || {}).genre, c.subject_override,
    (c.scene || {}).scene,
    (seo.tags || []).join(" "), (seo.hashtags || []).join(" "),
    c.file, r && r.source_title,
  ].filter(Boolean).join(" ").toLowerCase();
}

// Was this run made inside the selected window? "Today" is the calendar day,
// not the last 24 hours — a clip made last night is not one you made today.
function inWindow(ts) {
  if (libWhen === "all") return true;
  if (!ts) return false;
  const made = new Date(ts * 1000);
  const days = Number(libWhen);
  if (days === 1) return made.toDateString() === new Date().toDateString();
  return Date.now() - made.getTime() <= days * 86400000;
}

function whenMade(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  return d.toLocaleDateString();
}

function clipCard(c) {
  const i = c._i;
  const seo = c.seo || null;
  const title = (seo && seo.title) || c.title || "Untitled";
  const sub = (seo && seo.hook_text) || c.hook_sentence
    || (c.start_time != null ? `${clock(c.start_time)} → ${clock(c.end_time)}` : "");
  const rank = c.rank || c.index;
  // What the clip is filed under, on the card itself, so a clip labelled with
  // the wrong game is visible at a glance instead of only after upload.
  const filed = c.subject_override || (seo && seo.about && seo.about.subject) || "";
  return `
    <div class="clip" data-i="${i}" style="animation-delay:${Math.min(i, 12) * 45}ms">
      <div class="thumb">
        <video src="${esc(c.url)}#t=0.5" preload="metadata" muted playsinline data-i="${i}"></video>
        <div class="veil"><span class="pbtn"><svg><use href="#i-play"/></svg></span></div>
        <span class="rank">${rank ? `#${esc(rank)}` : ""}${
          c.score != null ? `<i>★ ${esc(c.score)}</i>` : ""}</span>
        <span class="dur">${c.duration != null ? clock(c.duration) : ""}</span>
        <div class="flag">
          ${c.edited ? `<span>trimmed</span>` : ""}
          ${c.muted ? `<span>muted</span>` : ""}
          ${c.saved_to ? `<span>saved</span>` : ""}
          ${!seo ? `<span class="need">no title yet</span>` : ""}
          ${filed ? `<span class="subj">${esc(filed)}</span>` : ""}
        </div>
      </div>
      <div class="ct">${esc(title)}</div>
      <div class="cm">${esc(sub)}</div>
      <div class="cq">
        ${seo ? `<button class="qbtn" data-copy="title" data-i="${i}">Copy title</button>
        <button class="qbtn" data-copy="tags" data-i="${i}">Copy tags</button>` : ""}
        <button class="qbtn" data-show="${i}"
          title="${esc(c.path || c.file || "")}">Show file</button>
      </div>
    </div>`;
}

function renderClips() {
  const count = clips.length;
  $("gCount").textContent = count;
  $("gCount").classList.toggle("hidden", !count);
  $("results").classList.toggle("hidden", !count);

  const terms = libQ.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const filtering = terms.length > 0 || libWhen !== "all";
  shown = [];

  $("clips").innerHTML = runs.map((r) => {
    // The date belongs to the run — every clip in it was made at once — while
    // the search is per clip, so a run can survive on just one of its clips.
    const keep = inWindow(r.created_at)
      ? r.clips.filter((c) => terms.every((t) => c._hay.includes(t)))
      : [];
    if (!keep.length) return "";

    const cards = keep.map((c) => { shown.push(c._i); return clipCard(c); }).join("");
    // Counted across the whole run, not the filtered view: the button rewrites
    // every clip in the run, so it must not promise less than it does.
    const missing = r.clips.filter((c) => !c.seo).length;
    return `
      <section class="run">
        <div class="run-head">
          <div class="run-id">
            <h3>${esc(r.source_title || "Clips from an earlier run")}</h3>
            <span>${keep.length < r.clips.length
                ? `${keep.length} of ${r.clips.length} clips`
                : `${r.clips.length} clip${r.clips.length > 1 ? "s" : ""}`}
              · ${esc(whenMade(r.created_at))}</span>
          </div>
          <div class="run-actions">
            <button class="btn ghost sm" data-seorun="${esc(r.id)}"
              data-missing="${missing}">
              <svg><use href="#i-spark"/></svg>${missing ? "Write titles &amp; tags" : "Rewrite titles"}</button>
            <button class="btn ghost sm" data-openrun="${esc(r.id)}"
              title="${esc(r.shorts_dir || "")}">
              <svg><use href="#i-folder"/></svg>Folder</button>
          </div>
        </div>
        <div class="clips">${cards}</div>
      </section>`;
  }).join("");

  $("libEmpty").classList.toggle("hidden", !count || shown.length > 0);
  $("libCount").classList.toggle("hidden", !filtering || !shown.length);
  $("libCount").textContent =
    `${shown.length} of ${count} clip${count === 1 ? "" : "s"}`;
  $("libQClear").classList.toggle("hidden", !libQ);

  $("clips").querySelectorAll(".clip").forEach((el) => {
    el.onclick = () => openPlayer(+el.dataset.i);
  });
  // Clips restored from an older folder have no recorded length; the file
  // itself knows, so fill it in as soon as the browser reads the header.
  $("clips").querySelectorAll(".thumb video").forEach((v) => {
    v.addEventListener("loadedmetadata", () => {
      const c = clips[+v.dataset.i];
      if (!c || c.duration != null || !isFinite(v.duration)) return;
      c.duration = Math.round(v.duration * 10) / 10;
      const tag = v.closest(".thumb").querySelector(".dur");
      if (tag) tag.textContent = clock(c.duration);
    }, { once: true });
  });
  $("clips").querySelectorAll("[data-copy]").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      const c = clips[+b.dataset.i];
      const seo = (c && c.seo) || {};
      const what = b.dataset.copy;
      copy(what === "title" ? seo.title : (seo.tags || []).join(", "),
           what === "title" ? "Title copied" : "Tags copied");
    };
  });
  $("clips").querySelectorAll("[data-show]").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); showClipFile(clips[+b.dataset.show]); };
  });
  $("clips").querySelectorAll("[data-seorun]").forEach((b) => {
    b.onclick = () => writeSeoForRun(b.dataset.seorun, b, +b.dataset.missing === 0);
  });
  $("clips").querySelectorAll("[data-openrun]").forEach((b) => {
    b.onclick = async () => {
      try {
        await api(`/api/jobs/${encodeURIComponent(b.dataset.openrun)}/reveal`,
                  json("POST", {}));
      } catch (e) {
        toast(e.message, true);
      }
    };
  });
}

// Open the file manager with the clip's own file selected, so "where is this
// on my PC" is one click rather than a hunt through folders.
async function showClipFile(c) {
  if (!c) return;
  try {
    const d = await api(`/api/jobs/${encodeURIComponent(jobOf(c))}/reveal`,
                        json("POST", { file: c.file }));
    toast(d.opened || "Opened in your files.");
  } catch (e) {
    toast(e.message, true);
  }
}

async function copy(text, okMsg) {
  const value = String(text ?? "").trim();
  if (!value) { toast("Nothing to copy yet."); return false; }
  try {
    await navigator.clipboard.writeText(value);
  } catch (_) {
    // Clipboard API needs a secure context; a hidden textarea always works.
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (_) { /* nothing left to try */ }
    ta.remove();
  }
  toast(okMsg || "Copied");
  return true;
}

// Writing metadata costs an LLM call, and for clips with no transcript on
// record a short transcription too — so it runs on request, not on load.
async function writeSeoForRun(runId, btn, force) {
  const label = btn ? btn.innerHTML : "";
  if (btn) { btn.disabled = true; btn.textContent = "Writing…"; }
  try {
    await api(`/api/jobs/${encodeURIComponent(runId)}/seo${force ? "?force=true" : ""}`,
              { method: "POST" });
    const at = cur;
    await loadLibrary();
    if (at > -1 && clips[at]) renderSeo();
    toast("Titles, descriptions and tags are ready — copy them from any clip.");
  } catch (e) {
    // Flagged as an error so it reads as one and stays up longer — this is
    // the message that explains why the titles did not change.
    toast(e.message || "Could not write the metadata.", true);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = label; }
  }
}

// ---------------------------------------------------------------- player

const vid = $("pVideo");

function openPlayer(i) {
  const c = clips[i];
  if (!c) return;
  cur = i;
  closeMini();

  vid.src = c.url;
  vid.muted = false;
  vid.currentTime = 0;
  $("pTitle").textContent = (c.seo && c.seo.title) || c.title || "Untitled";
  $("pMeta").textContent = [
    c.rank ? `#${c.rank} of this run` : "",
    c.start_time != null ? `${clock(c.start_time)} → ${clock(c.end_time)}` : "",
    c.duration != null ? `${c.duration}s` : "",
    c.score != null ? `score ${c.score}` : "",
    c.muted ? "muted" : "",
  ].filter(Boolean).join(" · ");
  $("pDownload").href = c.url;
  $("pDownload").setAttribute("download", c.file || "short.mp4");

  body.classList.add("player-on");
  $("player").classList.remove("trim-on");
  renderSeo();
  setMuteIcon();
  vid.play().catch(() => {});
}

function closePlayer() {
  body.classList.remove("player-on");
  $("player").classList.remove("trim-on", "seo-on");
  vid.pause();
}
$("pClose").onclick = closePlayer;
$("player").addEventListener("click", (e) => { if (e.target === $("player")) closePlayer(); });

function setPlayIcon() {
  const on = !vid.paused;
  $("pPlay").querySelector("use").setAttribute("href", on ? "#i-pause" : "#i-play");
  $("pPlay").querySelector("span").textContent = on ? "Pause" : "Play";
  $("player").classList.toggle("paused", vid.paused);
}

function togglePlay() {
  if (vid.paused) vid.play().catch(() => {}); else vid.pause();
}
$("pPlay").onclick = togglePlay;
$("pTap").onclick = togglePlay;
vid.addEventListener("play", setPlayIcon);
vid.addEventListener("pause", setPlayIcon);

function setMuteIcon() {
  $("pMute").querySelector("use").setAttribute("href", vid.muted ? "#i-mute" : "#i-vol");
  $("pMute").querySelector("span").textContent = vid.muted ? "Muted" : "Sound";
  $("pMute").classList.toggle("on", vid.muted);
}
$("pMute").onclick = () => { vid.muted = !vid.muted; setMuteIcon(); };
$("pShow").onclick = () => showClipFile(clips[cur]);

vid.addEventListener("timeupdate", () => {
  const f = vid.duration ? vid.currentTime / vid.duration : 0;
  $("pScrubFill").style.width = `${f * 100}%`;
  $("pScrubKnob").style.left = `${f * 100}%`;
  $("pNow").textContent = clock(vid.currentTime);
});
vid.addEventListener("loadedmetadata", () => { $("pDur").textContent = clock(vid.duration); });
vid.addEventListener("ended", () => { vid.currentTime = 0; vid.play().catch(() => {}); });

function scrubTo(e) {
  const r = $("pScrub").getBoundingClientRect();
  const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  if (vid.duration) vid.currentTime = f * vid.duration;
}
$("pScrub").addEventListener("pointerdown", (e) => {
  scrubTo(e);
  const move = (ev) => scrubTo(ev);
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
});

document.addEventListener("keydown", (e) => {
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
  if (body.classList.contains("welcome-on")) {
    if (e.key === "Escape") closeWelcome();
    return;
  }
  if (!body.classList.contains("player-on")) {
    if (e.key === "Escape" && body.classList.contains("drawer-on")) body.classList.remove("drawer-on");
    return;
  }
  const k = e.key.toLowerCase();
  if (e.key === "Escape") { closePlayer(); }
  else if (e.key === " ") { e.preventDefault(); togglePlay(); }
  else if (k === "m") { vid.muted = !vid.muted; setMuteIcon(); }
  else if (k === "t") { toggleTrim(); }
  else if (k === "b") { toggleSeo(); }
  else if (k === "f") { showClipFile(clips[cur]); }
  else if (e.key === "ArrowRight") { vid.currentTime = Math.min(vid.duration || 0, vid.currentTime + 2); }
  else if (e.key === "ArrowLeft") { vid.currentTime = Math.max(0, vid.currentTime - 2); }
  // Step through what is actually on screen. With a filter applied, the clip
  // after this one is the next visible card, not the next array entry.
  else if (e.key === "ArrowDown") { stepClip(1); }
  else if (e.key === "ArrowUp") { stepClip(-1); }
});

// ---------------------------------------------------------------- upload metadata

function seoField(label, id, value, opts = {}) {
  const tag = opts.multiline ? "textarea" : "input";
  const attrs = opts.multiline ? `rows="${opts.rows || 7}"` : `type="text"`;
  return `
    <div class="sf">
      <div class="sf-top">
        <span>${esc(label)}</span>
        ${opts.count ? `<i class="sf-count">${String(value || "").length}/${opts.count}</i>` : ""}
        <button class="qbtn" data-seocopy="${id}">Copy</button>
      </div>
      <${tag} id="${id}" ${attrs} spellcheck="false">${
        opts.multiline ? esc(value || "") : ""}</${tag}>
    </div>`;
}

// Which app the Boost panel is packaging for. Remembered across clips, since
// someone posting a batch to Instagram posts all of it to Instagram.
let seoPlatform = "yt";
const SEO_PLATFORMS = [["yt", "YouTube Shorts"], ["ig", "Instagram Reels"], ["tt", "TikTok"]];

// A ranked list of options -- titles, descriptions, captions -- that fill a
// box when tapped. The score leads, so the order reads at a glance.
function optionList(label, key, options, current, field) {
  if (options.length < 2) return "";
  return `
    <div class="sf">
      <div class="sf-top"><span>${esc(label)}</span></div>
      <div class="topts">${options.map((o, k) => `
        <button class="topt${o[field] === current ? " on" : ""}" data-${key}="${k}"
          title="${esc(scoreNote(o))}">
          <b>${esc(o.score)}</b><span class="topt-text">${esc(o[field])}</span><i>${esc(o.angle || "")}</i>
        </button>`).join("")}</div>
    </div>`;
}

function renderSeo() {
  const c = clips[cur];
  const box = $("seoBody");
  if (!c) { box.innerHTML = ""; return; }

  const seo = c.seo;
  $("seoRedo").textContent = seo ? "Rewrite" : "Write it now";
  $("seoCopyAll").disabled = !seo;
  $("seoMsg").innerHTML = "";

  if (!seo) {
    box.innerHTML = `
      <p class="seo-empty">This clip has no upload metadata yet — it was made before
        the app started writing it, or the write failed.<br><br>
        <b>Write it now</b> listens to this clip and writes a title, a description,
        tags, an on-screen hook, and captions for Instagram Reels and TikTok, from
        what is actually said in it.</p>`;
    return;
  }

  const options = seo.title_options || [];
  const descOptions = seo.description_options || [];
  const tagOptions = seo.tag_options || [];
  const reels = seo.reels || null;
  const tiktok = seo.tiktok || null;
  const capOptions = (reels && reels.caption_options) || [];
  const madeFromYoutube = (reels && reels.generated === false) || (tiktok && tiktok.generated === false);
  const socialNote = !reels && !tiktok
    ? `<p class="seo-why warnish">This clip was packaged before the app wrote Instagram and TikTok
         captions. Hit Rewrite to have them written for each app.</p>`
    : madeFromYoutube && seo.generated !== false
      ? `<p class="seo-why warnish">Made from the YouTube text, because writing these
           didn't work this time. Hit Rewrite to have them written for this app.</p>`
      : "";

  box.innerHTML =
    aboutField(c, seo)
    + `<div class="ptabs" role="tablist">${SEO_PLATFORMS.map(([k, name]) => `
        <button class="ptab${k === seoPlatform ? " on" : ""}" role="tab" data-plat="${k}"
          aria-selected="${k === seoPlatform}">${esc(name)}</button>`).join("")}</div>`

    // ---- YouTube Shorts
    + `<div class="pbody" data-pbody="yt"${seoPlatform === "yt" ? "" : " hidden"}>`
    + seoField("Title — paste into YouTube's title box", "sfTitle", seo.title, { count: 100 })
    + optionList("Title options, best first — tap one to use it", "topt", options, seo.title, "title")
    + seoField("Description", "sfDesc", seo.description, { multiline: true, rows: 8 })
    + optionList("Description options, best first — tap one to use it", "dopt", descOptions,
                 seo.description, "description")
    + seoField("Tags — paste into the tags box", "sfTags", (seo.tags || []).join(", "),
               { multiline: true, rows: 3 })
    + (tagOptions.length ? `
      <div class="sf">
        <div class="sf-top"><span>More tags, best first — tap to add or remove</span>
          <i class="sf-count" id="sfTagCount"></i></div>
        <div class="tchips">${tagOptions.map((o, k) => `
          <button class="tchip" data-tchip="${k}"
            title="${esc([o.kind, o.score != null ? `score ${o.score}` : ""].filter(Boolean).join(" · "))}">${
            esc(o.tag)}${o.score != null ? `<small>${esc(o.score)}</small>` : ""}</button>`
        ).join("")}</div>
      </div>` : "")
    + seoField("On-screen hook for the first 2 seconds", "sfHook", seo.hook_text)
    + (seo.search_phrase
        ? `<p class="seo-why"><b>Ranks for:</b> ${esc(seo.search_phrase)}</p>` : "")
    + (seo.why_it_works
        ? `<p class="seo-why"><b>Why this one travels:</b> ${esc(seo.why_it_works)}</p>` : "")
    + uploadBox()
    + `</div>`

    // ---- Instagram Reels
    + `<div class="pbody" data-pbody="ig"${seoPlatform === "ig" ? "" : " hidden"}>`
    + seoField("Caption — paste into Instagram", "sfIgCaption", reels ? reels.caption : "",
               { multiline: true, rows: 6, count: 2200 })
    + `<p class="sf-hint">The first 125 characters show before “more”, so the hook and the search words go there.</p>`
    + optionList("Caption options, best first — tap one to use it", "copt", capOptions,
                 reels ? reels.caption : "", "caption")
    + seoField("Hashtags", "sfIgTags", reels ? (reels.hashtags || []).join(" ") : "")
    + seoField("Cover text — 2 to 5 words, kept in the middle for the 3:4 grid", "sfIgCover",
               reels ? reels.cover_text : "", { count: 40 })
    + seoField("Alt text — Advanced settings, Accessibility", "sfIgAlt",
               reels ? reels.alt_text : "", { count: 100 })
    + `</div>`

    // ---- TikTok
    + `<div class="pbody" data-pbody="tt"${seoPlatform === "tt" ? "" : " hidden"}>`
    + seoField("Caption — paste into TikTok", "sfTtCaption", tiktok ? tiktok.caption : "",
               { multiline: true, rows: 6, count: 4000 })
    + seoField("Hashtags", "sfTtTags", tiktok ? (tiktok.hashtags || []).join(" ") : "")
    + `</div>`

    + socialNote
    + (seo.generated === false
        ? `<p class="seo-why warnish">Written from the clip's own hook line — the model
             wasn't reachable. Hit Rewrite to have it written properly.</p>` : "");

  // Single-line boxes are filled here rather than in the markup, so a quote
  // or an angle bracket in a title can never break out of an attribute.
  $("sfTitle").value = seo.title || "";
  $("sfHook").value = seo.hook_text || "";
  $("sfIgTags").value = reels ? (reels.hashtags || []).join(" ") : "";
  $("sfIgCover").value = reels ? reels.cover_text || "" : "";
  $("sfIgAlt").value = reels ? reels.alt_text || "" : "";
  $("sfTtTags").value = tiktok ? (tiktok.hashtags || []).join(" ") : "";
  renderUploadBox();
  if (!ytu) loadYtu().then(renderUploadBox);

  box.querySelectorAll("[data-plat]").forEach((b) => {
    b.onclick = () => {
      seoPlatform = b.dataset.plat;
      box.querySelectorAll("[data-plat]").forEach((x) => {
        x.classList.toggle("on", x === b);
        x.setAttribute("aria-selected", String(x === b));
      });
      box.querySelectorAll("[data-pbody]").forEach((p) => { p.hidden = p.dataset.pbody !== seoPlatform; });
    };
  });

  box.querySelectorAll("[data-seocopy]").forEach((b) => {
    b.onclick = () => {
      const el = $(b.dataset.seocopy);
      copy(el && el.value, "Copied");
    };
  });

  // Every box with a limit keeps its count live: each app rejects or cuts
  // text past its own limit, whoever typed it.
  box.querySelectorAll(".sf").forEach((sf) => {
    const count = sf.querySelector(".sf-count");
    const input = sf.querySelector("input, textarea");
    const limit = count && Number((count.textContent.split("/")[1] || "").trim());
    if (!count || !input || !limit) return;
    const paint = () => {
      count.textContent = `${input.value.length}/${limit}`;
      count.classList.toggle("over", input.value.length > limit);
    };
    input.addEventListener("input", paint);
    paint();
  });

  // Editing was already possible; keeping the edit was not. Every box now
  // arms the Save button, so a title you rewrote survives closing the panel
  // instead of being thrown away the next time this renders.
  seoDirty = false;
  $("seoSave").disabled = true;
  $("seoSave").textContent = "Save changes";
  ["sfSubject", "sfTitle", "sfDesc", "sfTags", "sfHook", "sfIgCaption", "sfIgTags",
   "sfIgCover", "sfIgAlt", "sfTtCaption", "sfTtTags"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", markSeoDirty);
  });

  // A ranked option goes into its box rather than straight to disk, so it can
  // be tweaked first and is kept by the same Save as typed text.
  const wireOptions = (key, list, field, target) => {
    box.querySelectorAll(`[data-${key}]`).forEach((b) => {
      b.onclick = () => {
        const o = list[+b.dataset[key]];
        if (!o) return;
        $(target).value = o[field];
        $(target).dispatchEvent(new Event("input"));
        box.querySelectorAll(`[data-${key}]`).forEach((x) => x.classList.toggle("on", x === b));
      };
    });
  };
  wireOptions("topt", options, "title", "sfTitle");
  wireOptions("dopt", descOptions, "description", "sfDesc");
  wireOptions("copt", capOptions, "caption", "sfIgCaption");

  // The chips and the tag box are one list seen two ways: toggling a chip
  // edits the box, and typing in the box relights the chips.
  const tagsBox = $("sfTags");
  const current = () => tagsBox.value.split(",").map((t) => t.trim().toLowerCase()).filter(Boolean);
  const paintChips = () => {
    const have = new Set(current());
    box.querySelectorAll("[data-tchip]").forEach((b) => {
      const o = tagOptions[+b.dataset.tchip];
      b.classList.toggle("on", !!o && have.has(o.tag));
    });
    const count = $("sfTagCount");
    if (count) {
      // YouTube counts the commas between tags against its 500 too.
      const used = current().join(",").length;
      count.textContent = `${used}/500`;
      count.classList.toggle("over", used > 500);
    }
  };
  box.querySelectorAll("[data-tchip]").forEach((b) => {
    b.onclick = () => {
      const o = tagOptions[+b.dataset.tchip];
      if (!o) return;
      const have = current();
      tagsBox.value = (have.includes(o.tag) ? have.filter((t) => t !== o.tag) : [...have, o.tag])
        .join(", ");
      paintChips();
      markSeoDirty();
    };
  });
  tagsBox.addEventListener("input", paintChips);
  paintChips();
}

// What the clip was filed under, and a box to put it right. The app names a
// game only when something on screen or in the clip confirms it; a person
// who knows better types the real name here and every rewrite after uses it.
function aboutField(c, seo) {
  const a = seo.about || {};
  const scene = c.scene || {};
  const kind = (a.content_type || scene.content_type || "").replace(/_/g, " ");
  const genre = a.genre || scene.genre || "";
  const by = {
    you: "you set this", on_screen_text: "named on screen", speech: "said in the clip",
    source_listing: "from the video's title", unmistakable: "recognised on screen",
    mentioned_in_video: "matches a name said in the video", frames: "seen on screen",
  }[a.by] || "";
  let hint;
  if (c.subject_override) {
    hint = `Filed under ${esc(c.subject_override)} because you said so.`;
  } else if (a.confirmed && a.subject) {
    hint = `Filed under <b>${esc(a.subject)}</b>${by ? ` — ${esc(by)}` : ""}. Wrong? Type the real name and hit Rewrite.`;
  } else if (a.guess) {
    hint = `Looks like it could be ${esc(a.guess)}, but nothing on screen confirms it, so the titles
      say ${genre ? `“${esc(genre)}”` : "what happens"} instead. Type the real name and hit Rewrite.`;
  } else {
    hint = "Not named. Type what this is — a game, a show, a topic — and hit Rewrite.";
  }
  return `
    <div class="sf">
      <div class="sf-top"><span>What's in this clip</span>
        ${kind ? `<i class="sf-count">${esc(kind)}${genre ? ` · ${esc(genre)}` : ""}</i>` : ""}</div>
      <input id="sfSubject" type="text" spellcheck="false" maxlength="80"
        value="${esc(c.subject_override || "")}"
        placeholder="${esc(a.subject || a.guess || "e.g. Fear to Fathom")}">
      <p class="sf-hint">${hint}</p>
    </div>`;
}

function scoreNote(o) {
  const parts = [`Score ${o.score}/100`];
  if (o.model_score != null) parts.push(`editor rubric ${o.model_score}`);
  if (o.check_score != null) parts.push(`the app's own check ${o.check_score}`);
  return parts.join(" · ");
}

let seoDirty = false;

function markSeoDirty() {
  seoDirty = true;
  $("seoSave").disabled = false;
}

// Save the words in the boxes, whoever wrote them. The server renames the mp4
// to match a new title, so the open player has to be pointed at the new name.
async function saveSeo() {
  const c = clips[cur];
  if (!c || !c.file) return;
  const btn = $("seoSave");
  btn.disabled = true;
  btn.textContent = "Saving…";
  $("seoMsg").innerHTML = "";
  try {
    const body = {
      title: $("sfTitle").value,
      description: $("sfDesc").value,
      tags: $("sfTags").value,
      hook_text: $("sfHook").value,
      subject: $("sfSubject") ? $("sfSubject").value : undefined,
      reels_caption: $("sfIgCaption").value,
      reels_hashtags: $("sfIgTags").value,
      reels_cover_text: $("sfIgCover").value,
      reels_alt_text: $("sfIgAlt").value,
      tiktok_caption: $("sfTtCaption").value,
      tiktok_hashtags: $("sfTtTags").value,
    };
    const d = await api(
      `/api/jobs/${encodeURIComponent(jobOf(c))}/clips/${encodeURIComponent(c.file)}/seo`,
      json("PUT", body));
    const renamed = d.file && d.file !== c.file;
    patchClip(cur, { seo: d.seo, file: d.file, url: d.url, subject_override: d.subject_override });
    if (renamed) {
      const at = vid.currentTime, playing = !vid.paused;
      vid.src = `${d.url}?v=${Date.now()}`;
      vid.currentTime = at;
      if (playing) vid.play().catch(() => {});
      $("pDownload").href = d.url;
      $("pDownload").setAttribute("download", d.file);
    }
    $("pTitle").textContent = (d.seo && d.seo.title) || d.title || "Untitled";
    seoDirty = false;
    btn.textContent = "Saved";
    toast(renamed ? "Saved — the file was renamed to match." : "Saved.");
  } catch (e) {
    $("seoMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
    btn.disabled = false;
    btn.textContent = "Save changes";
  }
}
$("seoSave").onclick = saveSeo;

function toggleSeo() {
  const p = $("player");
  if (p.classList.contains("seo-on")) { p.classList.remove("seo-on"); return; }
  if (!clips[cur]) return;
  p.classList.remove("trim-on");
  renderSeo();
  p.classList.add("seo-on");
}
$("pSeoBtn").onclick = toggleSeo;
$("seoClose").onclick = () => $("player").classList.remove("seo-on");

$("seoCopyAll").onclick = () => {
  const c = clips[cur];
  if (!c || !c.seo) return;
  // Everything for the app whose tab is open: that is the one being posted to.
  if (seoPlatform === "ig") {
    copy([
      `${$("sfIgCaption").value}\n\n${$("sfIgTags").value}`.trim(),
      `COVER TEXT\n${$("sfIgCover").value}`,
      `ALT TEXT\n${$("sfIgAlt").value}`,
    ].join("\n\n"), "Caption, hashtags, cover and alt text copied.");
    return;
  }
  if (seoPlatform === "tt") {
    copy(`${$("sfTtCaption").value}\n\n${$("sfTtTags").value}`.trim(),
         "Caption and hashtags copied.");
    return;
  }
  copy([
    `TITLE\n${$("sfTitle").value}`,
    `DESCRIPTION\n${$("sfDesc").value}`,
    `TAGS\n${$("sfTags").value}`,
    `ON-SCREEN HOOK\n${$("sfHook").value}`,
  ].join("\n\n"), "Title, description, tags and hook copied.");
};

$("seoRedo").onclick = () => {
  const c = clips[cur];
  if (!c) return;
  // A rewrite replaces every box, including ones somebody typed by hand.
  // Losing your own title to a button you meant to press once is the kind of
  // thing you only forgive an app once.
  if (seoDirty || (c.seo && c.seo.edited)) {
    ask("Rewrite over your own words?",
        "This clip has a title you edited. Rewriting replaces it, along with the "
        + "description, tags, hook and the Instagram and TikTok captions.", rewriteSeo);
    return;
  }
  rewriteSeo();
};

async function rewriteSeo() {
  const c = clips[cur];
  if (!c) return;
  const btn = $("seoRedo");
  btn.disabled = true;
  busy(true, "Writing the title and tags…");
  $("seoMsg").innerHTML = "";
  try {
    // A name typed into "What's in this clip" is the reason to rewrite, so it
    // is saved first -- otherwise the rewrite would file the clip under the
    // very guess it was meant to correct.
    const typed = $("sfSubject") ? $("sfSubject").value.trim() : null;
    if (typed !== null && typed !== (c.subject_override || "")) {
      const s = await api(
        `/api/jobs/${encodeURIComponent(jobOf(c))}/clips/${encodeURIComponent(c.file)}/seo`,
        json("PUT", { subject: typed }));
      patchClip(cur, { subject_override: s.subject_override });
    }
    // Just this clip. The rest of the run keeps its titles, and is sent along
    // so the new one does not open the same way as any of them.
    const d = await api(
      `/api/jobs/${encodeURIComponent(jobOf(c))}/seo?force=true&only=${encodeURIComponent(c.file)}`,
      { method: "POST" });
    // Match on index, not on filename. A rewrite renames the mp4 to its new
    // title, so every x.file coming back is the *new* name -- matching the
    // old one finds nothing, silently, and leaves the card holding a url
    // whose file no longer exists. That is a 404, which the <video> reports
    // as a black frame and MEDIA_ERR_SRC_NOT_SUPPORTED.
    const fresh = (x) => ({ seo: x.seo, file: x.file, url: x.url, title: x.title,
                            scene: x.scene, subject_override: x.subject_override });
    const mine = (d.clips || []).find((x) => x.index === c.index);
    if (mine) patchClip(cur, fresh(mine));
    // Everything else in that run was rewritten too — take the new copy.
    (d.clips || []).forEach((x) => {
      const local = clips.find((y) => y.index === x.index && jobOf(y) === jobOf(c));
      if (local && local !== c) Object.assign(local, fresh(x));
    });
    renderClips();
    renderSeo();
    // The open <video> keeps whatever src it was rendered with, so a rename
    // has to be pushed onto the element by hand -- same as the trim path.
    if (mine) {
      const at = vid.currentTime, playing = !vid.paused;
      vid.src = `${mine.url}?v=${Date.now()}`;
      vid.currentTime = at;
      if (playing) vid.play().catch(() => {});
      $("pDownload").href = mine.url;
      $("pDownload").setAttribute("download", mine.file);
    }
    $("pTitle").textContent = (clips[cur].seo && clips[cur].seo.title) || clips[cur].title;
    toast("Rewritten.");
  } catch (e) {
    $("seoMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    busy(false);
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- trim

function toggleTrim() {
  const p = $("player");
  if (p.classList.contains("trim-on")) { p.classList.remove("trim-on"); return; }
  if (!clips[cur]) return;
  resetTrim();
  p.classList.remove("seo-on");
  p.classList.add("trim-on");
}
$("pTrimBtn").onclick = toggleTrim;
$("trimClose").onclick = () => $("player").classList.remove("trim-on");

function resetTrim() {
  const c = clips[cur];
  if (!c) return;
  const start = Number(c.start_time) || 0;
  const end = Number(c.end_time) || start + 30;
  const pad = Math.max(20, (end - start) * 0.8);
  trim = { lo: Math.max(0, start - pad), hi: end + pad, start, end };
  $("tMute").checked = !!c.muted;
  $("tMsg").innerHTML = "";
  drawTrim();
}
$("tReset").onclick = resetTrim;

function drawTrim() {
  if (!trim) return;
  const span = trim.hi - trim.lo || 1;
  const a = ((trim.start - trim.lo) / span) * 100;
  const b = ((trim.end - trim.lo) / span) * 100;
  $("tFill").style.left = `${a}%`;
  $("tFill").style.width = `${Math.max(0, b - a)}%`;
  $("tStartH").style.left = `${a}%`;
  $("tEndH").style.left = `${b}%`;
  $("tStart").value = clock(trim.start);
  $("tEnd").value = clock(trim.end);
  $("tLen").textContent = `${(trim.end - trim.start).toFixed(1)}s`;
  $("tScaleA").textContent = clock(trim.lo);
  $("tScaleB").textContent = clock(trim.hi);
}

function dragHandle(which, e) {
  const track = $("tTrack");
  const h = which === "start" ? $("tStartH") : $("tEndH");
  h.classList.add("drag");

  const move = (ev) => {
    const r = track.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
    const t = trim.lo + f * (trim.hi - trim.lo);
    if (which === "start") trim.start = Math.min(t, trim.end - 1);
    else trim.end = Math.max(t, trim.start + 1);
    drawTrim();
  };
  const up = () => {
    h.classList.remove("drag");
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  move(e);
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}
$("tStartH").addEventListener("pointerdown", (e) => { e.preventDefault(); dragHandle("start", e); });
$("tEndH").addEventListener("pointerdown", (e) => { e.preventDefault(); dragHandle("end", e); });

function commitField(which) {
  const v = parseClock(which === "start" ? $("tStart").value : $("tEnd").value);
  if (!isFinite(v)) { drawTrim(); return; }
  if (which === "start") trim.start = Math.max(0, Math.min(v, trim.end - 1));
  else trim.end = Math.max(trim.start + 1, v);
  trim.lo = Math.min(trim.lo, Math.max(0, trim.start - 5));
  trim.hi = Math.max(trim.hi, trim.end + 5);
  drawTrim();
}
["tStart", "tEnd"].forEach((id) => {
  const which = id === "tStart" ? "start" : "end";
  $(id).addEventListener("change", () => commitField(which));
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") commitField(which); });
});

document.querySelectorAll("[data-nudge]").forEach((b) => {
  b.onclick = () => {
    const [which, step] = b.dataset.nudge.split(":");
    const d = parseFloat(step);
    if (which === "start") trim.start = Math.max(0, Math.min(trim.start + d, trim.end - 1));
    else trim.end = Math.max(trim.start + 1, trim.end + d);
    trim.lo = Math.min(trim.lo, Math.max(0, trim.start - 5));
    trim.hi = Math.max(trim.hi, trim.end + 5);
    drawTrim();
  };
});

function busy(on, text) {
  $("pBusy").classList.toggle("hidden", !on);
  if (text) $("pBusyText").textContent = text;
}

// The flat list and the per-run lists hold the same clip objects, so patching
// one in place keeps the grid and the player telling the same story.
function patchClip(i, updates) {
  const c = clips[i];
  if (!c) return null;
  Object.assign(c, updates);
  renderClips();
  return c;
}

function dropClip(i) {
  const c = clips[i];
  if (!c) return;
  const run = runs.find((r) => r.id === jobOf(c));
  if (run) run.clips = run.clips.filter((x) => x !== c);
  runs = runs.filter((r) => r.clips.length);
  clips.splice(i, 1);
  // Splicing shifts every clip after this one, so the cards' stored positions
  // are stale until they are handed out again.
  reindex();
  renderClips();
}


// Move the player `delta` cards along the visible grid. Falls back to the flat
// list when the clip in the player is not on screen — which is what happens if
// you filter it out while it is playing.
function stepClip(delta) {
  const order = shown.length ? shown : clips.map((_, i) => i);
  const at = order.indexOf(cur);
  const next = at === -1 ? cur + delta : order[at + delta];
  if (next != null && next >= 0 && next < clips.length) openPlayer(next);
}


// ---------------------------------------------------------------- library filters

function applyLibFilters() {
  renderClips();
}

$("libQ").addEventListener("input", (e) => {
  libQ = e.target.value;
  $("libQClear").classList.toggle("hidden", !libQ);
  clearTimeout(libTimer);
  libTimer = setTimeout(applyLibFilters, 120);
});

$("libQClear").onclick = () => {
  libQ = "";
  $("libQ").value = "";
  $("libQ").focus();
  applyLibFilters();
};

$("libWhen").onclick = (e) => {
  const btn = e.target.closest("[data-when]");
  if (!btn) return;
  libWhen = btn.dataset.when;
  for (const b of $("libWhen").querySelectorAll(".chip")) b.classList.toggle("on", b === btn);
  applyLibFilters();
};

$("libReset").onclick = () => {
  libQ = "";
  libWhen = "all";
  $("libQ").value = "";
  for (const b of $("libWhen").querySelectorAll(".chip")) {
    b.classList.toggle("on", b.dataset.when === "all");
  }
  applyLibFilters();
};

$("tApply").onclick = async () => {
  const c = clips[cur];
  if (!c || !trim) return;
  $("tApply").disabled = true;
  busy(true, "Re-cutting from the source…");
  $("tMsg").innerHTML = "";
  vid.pause();
  try {
    const updated = await api(
      `/api/jobs/${encodeURIComponent(jobOf(c))}/clips/${encodeURIComponent(c.file)}/trim`,
      json("POST", { start: trim.start, end: trim.end, mute: $("tMute").checked }));
    patchClip(cur, updated);
    // Cache-bust: the new render can reuse a name the browser already holds.
    vid.src = `${updated.url}?v=${Date.now()}`;
    $("pMeta").textContent =
      `${clock(updated.start_time)} → ${clock(updated.end_time)} · ${updated.duration}s`
      + (updated.muted ? " · muted" : "");
    $("pDownload").href = updated.url;
    $("pDownload").setAttribute("download", updated.file);
    vid.play().catch(() => {});
    toast("Clip re-cut.");
    $("player").classList.remove("trim-on");
  } catch (e) {
    $("tMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    busy(false);
    $("tApply").disabled = false;
  }
};

// ---------------------------------------------------------------- save / delete

$("pSave").onclick = async () => {
  const c = clips[cur];
  if (!c) return;
  $("pSave").disabled = true;
  try {
    const d = await api(
      `/api/jobs/${encodeURIComponent(jobOf(c))}/clips/${encodeURIComponent(c.file)}/save`,
      json("POST", { name: ((c.seo && c.seo.title) || c.title || "short").slice(0, 60) }));
    patchClip(cur, { saved_to: d.path });
    toast(`Saved to ${d.path}`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    $("pSave").disabled = false;
  }
};

$("pDelete").onclick = () => {
  const c = clips[cur];
  if (!c) return;
  ask("Delete this clip?", `"${c.title || "Untitled"}" is removed from your PC. This cannot be undone.`,
    async () => {
      try {
        await api(`/api/jobs/${encodeURIComponent(jobOf(c))}/clips/${encodeURIComponent(c.file)}`,
                  { method: "DELETE" });
        dropClip(cur);
        toast("Clip deleted.");
        if (!clips.length) { closePlayer(); closeMini(); return; }
        openPlayer(Math.min(cur, clips.length - 1));
      } catch (e) {
        toast(e.message, true);
      }
    });
};

// ---------------------------------------------------------------- mini player

const mvid = $("mVideo");

function openMini(i) {
  const c = clips[i];
  if (!c) return;
  cur = i;
  mvid.src = c.url;
  mvid.muted = true;          // autoplay only survives if it starts silent
  $("mTitle").textContent = (c.seo && c.seo.title) || c.title || "Untitled";
  $("mini").classList.remove("hidden");
  setMiniIcons();
  mvid.play().catch(() => {});
}

function closeMini() {
  $("mini").classList.add("hidden");
  mvid.pause();
  mvid.removeAttribute("src");
  mvid.load();
}

function setMiniIcons() {
  $("mPlay").querySelector("use").setAttribute("href", mvid.paused ? "#i-play" : "#i-pause");
  $("mMute").querySelector("use").setAttribute("href", mvid.muted ? "#i-mute" : "#i-vol");
}

$("mPlay").onclick = () => { if (mvid.paused) mvid.play().catch(() => {}); else mvid.pause(); };
$("mMute").onclick = () => { mvid.muted = !mvid.muted; setMiniIcons(); };
$("mClose").onclick = () => closeMini();
$("mExpand").onclick = () => openPlayer(cur);
mvid.addEventListener("play", setMiniIcons);
mvid.addEventListener("pause", setMiniIcons);
mvid.addEventListener("ended", () => { mvid.currentTime = 0; mvid.play().catch(() => {}); });
mvid.addEventListener("timeupdate", () => {
  $("mFill").style.width = `${(mvid.duration ? mvid.currentTime / mvid.duration : 0) * 100}%`;
});
$("mini").querySelector("video").onclick = () => openPlayer(cur);

$("pMini").onclick = () => {
  const t = vid.currentTime;
  closePlayer();
  openMini(cur);
  mvid.addEventListener("loadedmetadata", () => { mvid.currentTime = t; }, { once: true });
};

// ---------------------------------------------------------------- boot

keyLinkFor($("setProvider").value);
checkSetup();
refreshUsage();
loadProcessor();
checkInterrupted();
loadLocations();
refreshPreview();

// Deliberately not saveKey(): that switches the active provider, and this key
// is meant to sit behind Gemini rather than replace it.
$("capSave").onclick = async () => {
  const msg = $("capMsg"), btn = $("capSave");
  btn.disabled = true;
  try {
    // as_fallback keeps this from switching the active provider to OpenAI.
    await api("/api/settings", json("POST", {
      provider: "openai", daily_limit: $("capOpenai").value.trim(), as_fallback: true,
    }));
    msg.innerHTML = `<div class="ok-box">Saved. The OpenAI meter will fill against it.</div>`;
    refreshUsage();
  } catch (e) {
    msg.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
};

$("fbSave").onclick = async () => {
  const key = $("fbKey").value.trim();
  const msg = $("fbMsg"), btn = $("fbSave");
  if (!key) { msg.innerHTML = `<div class="err">Paste an OpenAI key first.</div>`; return; }
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "Saving…";
  try {
    await api("/api/settings", json("POST", {
      provider: "openai", api_key: key, as_fallback: true,
    }));
    msg.innerHTML = `<div class="ok-box">Saved. Gemini stays your main provider; `
      + `OpenAI takes over if a run hits the daily cap.</div>`;
    $("fbKey").value = "";
    await checkSetup();     // the switcher can now offer OpenAI
    refreshUsage();
  } catch (e) {
    msg.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
};
// Clips made in earlier sessions are still on disk — show them straight away.
loadLibrary();

// ---------------------------------------------------------------- language
//
// English is what a fresh install shows. I18N only departs from it once the
// user has picked something, and it remembers that pick per browser profile.

(function initLanguage() {
  const sel = $("uiLang");
  if (!sel || !window.I18N) return;

  sel.innerHTML = I18N.codes
    .map((c) => `<option value="${c}">${esc(I18N.name(c))}</option>`)
    .join("");
  sel.value = I18N.lang;
  sel.onchange = () => I18N.set(sel.value);

  // Most of this UI is drawn after load -- clip cards, the library, the
  // upload panel. Rather than teaching every render function to translate,
  // watch for what they insert and translate that. The guard matters: apply()
  // rewrites text nodes, which the observer would otherwise see as more work
  // and hand straight back to apply().
  let inside = false;
  const observer = new MutationObserver((records) => {
    if (inside || I18N.lang === "en") return;
    inside = true;
    try {
      records.forEach((r) => {
        r.addedNodes.forEach((n) => {
          if (n.nodeType === 1) I18N.apply(n);
        });
      });
    } finally {
      // Let this batch's own mutations settle before listening again.
      setTimeout(() => { inside = false; }, 0);
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });

  I18N.apply(document.body);
})();
