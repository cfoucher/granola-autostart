// Settings and first-run setup. Data, and every change, go through window.pywebview.api (settings_app.Api).
"use strict";

const ICONS = {
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  timer: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2.5M9 2h6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
  tick: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  warn: '<path d="M12 3.5L21.5 20h-19z"/><path d="M12 10v4.5M12 17.2h.01"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  video: '<rect x="2.5" y="6" width="13" height="12" rx="2"/><path d="M15.5 10.5l6-3.5v10l-6-3.5"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  up: '<path d="M6 15l6-6 6 6"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  arrowUp: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  wifi: '<path d="M2 9a15 15 0 0 1 20 0M5.5 12.5a10 10 0 0 1 13 0M9 16a5 5 0 0 1 6 0M12 19.5h.01"/>',
  speaker: '<path d="M4 9.5h4L13 5v14l-5-4.5H4zM16.5 9a4 4 0 0 1 0 6M19 6.5a8 8 0 0 1 0 11"/>',
};

const icon = (name, cls = "") => `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;
const esc = (text) =>
  String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
// The Labyrinth app icon (icon.py draws the same thing for the exe, tray, and notifications).
const appMark = (size) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 64 64" aria-hidden="true">` +
  `<circle cx="32" cy="32" r="31" fill="#fbf8f1" stroke="#e2d9c6" stroke-width="1"/>` +
  `<circle cx="32" cy="32" r="27" fill="none" stroke="#e9e2d2" stroke-width="5"/>` +
  `<circle cx="32" cy="32" r="27" transform="rotate(-90 32 32)" fill="none" stroke="#5a8a47" stroke-width="5" stroke-linecap="round" stroke-dasharray="127.2 169.6"/>` +
  `<g fill="none" stroke="#42703a" stroke-width="4" stroke-linecap="round"><path d="M36.6 15.63A17 17 0 1 1 27.4 15.63"/>` +
  `<path d="M27.8 41.08A10 10 0 1 1 36.2 41.08"/><path d="M42 32H49"/></g><circle cx="32" cy="32" r="4.5" fill="#5a8a47"/></svg>`;
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

let data = null;
const ui = {
  section: "calendars",
  adding: false, addUrl: "", addResult: null, addBusy: false,
  tests: {}, confirmRemove: null, showHelp: false,
  addingAddress: false, newAddress: "", emailError: null,
  newWord: "",
  pauseMenu: false, busy: false, error: null, actions: 0,
  step: "welcome", tab: "google", setupUrl: "", found: null, setupBusy: false, setupError: null, setupEmail: "", zoomOnly: false,
};
const api = () => window.pywebview.api;

window.addEventListener("pywebviewready", async () => {
  data = await api().load();
  ui.section = data.section;
  render();
  setInterval(poll, 2000);
});

async function poll() {
  if (!data || data.page !== "settings" || document.hidden) return;
  // An action that ran meanwhile already loaded newer data than this poll may bring back.
  const actionsBefore = ui.actions;
  const fresh = await api().load().catch(() => null);
  if (!fresh || ui.actions !== actionsBefore) return;
  const changed = JSON.stringify([fresh.status, fresh.settings, fresh.config_error, fresh.theme]) !==
    JSON.stringify([data.status, data.settings, data.config_error, data.theme]);
  data = fresh;
  if (changed) render();
}

function render() {
  document.documentElement.dataset.theme = data.theme;
  const active = document.activeElement;
  const keep = active && active.id ? { id: active.id, start: safe(() => active.selectionStart), end: safe(() => active.selectionEnd) } : null;
  document.getElementById("app").innerHTML = data.page === "setup" ? setupView() : settingsView();
  if (keep) {
    const element = document.getElementById(keep.id);
    if (element) {
      element.focus();
      if (keep.start != null) safe(() => element.setSelectionRange(keep.start, keep.end));
    }
  }
}

function safe(read) {
  try { return read(); } catch { return null; }
}

function focusSoon(id) {
  setTimeout(() => document.getElementById(id)?.focus(), 0);
}

// Settings

function settingsView() {
  const nav = (id, glyph, text) =>
    `<button class="nav" data-action="section" data-section="${id}" ${ui.section === id ? 'aria-current="page"' : ""}>${icon(glyph)}${text}</button>`;
  return `<div class="window">
    <nav class="rail" aria-label="Settings sections">
      ${nav("calendars", "calendar", "Calendars")}
      ${nav("notes", "timer", "Starting notes")}
      ${nav("about", "info", "About")}
      ${statusBox()}
    </nav>
    <main>${errorBlock(data.config_error && ui.section !== "about" ? null : ui.error)}${{ calendars: calendarsSection, notes: notesSection, about: aboutSection }[ui.section]()}</main>
  </div>`;
}

// The status box at the foot of the side panel. Its buttons are the tray menu's: Pause, Resume, Start, and Fix.
function statusBox() {
  const s = data.status;
  const label = {
    watching: "Watching", notes: "Taking notes", paused: "Paused", attention: "Needs attention", stopped: "Not running", starting: "Starting…",
  }[s.state];
  const busy = ui.busy ? "disabled" : "";
  let controls = "";
  if (s.state === "stopped") {
    controls = `<button class="btn mini primary" data-action="startWatcher" ${busy}>Start</button>`;
  } else if (s.state !== "starting") {
    const fix = s.state === "attention" ? `<button class="btn mini primary" data-action="fix">Fix</button>` : "";
    const resume = `<button class="btn mini ${fix ? "" : "primary"}" data-action="resume" ${busy}>Resume</button>`;
    const restOfToday = !fix && !s.paused_today ? `<button class="link small" data-action="pause" data-length="today" ${busy}>Rest of today</button>` : "";
    controls = fix + (s.paused ? resume + restOfToday : pauseMenu(busy));
  }
  return `<div class="rail-status">
    <div class="stack" style="gap:4px" aria-live="polite">
      <div class="state"><span class="dot ${s.state}"></span>${label}</div>
      <div class="small ${s.state === "attention" ? "warn" : "muted"}">${esc(s.detail)}</div>
    </div>
    ${controls ? `<div class="row controls">${controls}</div>` : ""}
  </div>`;
}

function pauseMenu(busy) {
  const open = ui.pauseMenu;
  return `<div class="menu-anchor">
    <button id="pause-toggle" class="btn mini dropdown" data-action="togglePause" aria-haspopup="menu" aria-expanded="${open}" ${busy}>Pause${icon(open ? "up" : "down", "xs")}</button>
    ${open ? `<div class="menu" role="menu" aria-label="Pause">
      <button id="pause-hour" role="menuitem" data-action="pause" data-length="hour">For 1 hour</button>
      <button id="pause-today" role="menuitem" data-action="pause" data-length="today">For the rest of today</button>
    </div>` : ""}
  </div>`;
}

function configBanner() {
  if (!data.config_error) return "";
  return `<div class="banner" role="alert">${icon("warn")}<div class="stack" style="gap:8px">
    <span>${esc(data.config_error)}. Zoom joins still start notes, but nothing here can change until config.json is fixed.</span>
    <div class="row"><button class="btn small" data-action="openConfig">Open config.json</button></div></div></div>`;
}

function calendarsSection() {
  const intro = `<div class="intro"><h1>Calendars</h1>
    <p>Meetings on these calendars start notes when they begin. Zoom joins start notes even without a calendar.</p></div>`;
  if (!data.settings) return intro + configBanner();
  const st = data.settings;
  const rows = st.calendars.length
    ? st.calendars.map(calendarRow).join("")
    : `<div class="empty">No calendars yet. Add one so in-person and other meetings start notes too.</div>`;
  return `${intro}
    <div class="list" role="list" aria-label="Calendars">${rows}</div>
    ${ui.adding ? addPanel() : `<div class="row" style="gap:16px">
      <button class="btn" data-action="startAdd">${icon("plus", "sm")}Add calendar</button>
      <button class="link" data-action="toggleHelp" aria-expanded="${ui.showHelp}">Where do I find a calendar's address?</button></div>`}
    ${ui.showHelp && !ui.adding ? `<p class="help">${helpText()}</p>` : ""}
    ${declinedSection(st)}
    <p class="footnote">${icon("lock", "sm")}Calendar addresses are stored encrypted on this PC. Changes save as you make them.</p>`;
}

function helpText() {
  return `Google Calendar: open Settings, choose the calendar under “Settings for my calendars”, then Integrate calendar, and copy
    “Secret address in iCal format”. Outlook: Settings, Shared calendars, Publish a calendar, then copy the ICS link.`;
}

function calendarRow(c) {
  const lineIcon = { ok: icon("check", "sm"), warn: icon("warn", "sm"), pending: icon("clock", "sm") }[c.state];
  const actions = ui.confirmRemove === c.id
    ? `<span class="small muted">Remove?</span><button class="btn small" data-action="removeCalendar" data-id="${c.id}">Remove</button>
       <button class="btn small" data-action="keepCalendar">Keep</button>`
    : `<button class="btn small" data-action="testCalendar" data-id="${c.id}">Test</button>
       <button class="icon-btn" data-action="askRemove" data-id="${c.id}" aria-label="Remove ${esc(c.name)}">${icon("trash")}</button>`;
  return `<div class="item ${c.state === "warn" ? "warn" : ""}" role="listitem">
    <span class="lead">${icon("calendar")}</span>
    <div class="body">
      <div class="name">${esc(c.name)}</div>
      <div class="small muted">${esc(c.provider)}</div>
      <div class="line ${c.state}">${lineIcon}<span>${esc(c.line)}</span></div>
      ${testOutcome(ui.tests[c.id])}
    </div>
    <div class="actions">${actions}</div>
  </div>`;
}

function testOutcome(result) {
  if (!result) return "";
  if (result.busy) return `<div class="line">${icon("clock", "sm")}<span>Testing…</span></div>`;
  return `<div style="margin-top:8px">${resultBlock(result)}</div>`;
}

function resultBlock(result) {
  if (!result.ok) return errorBlock(result.error);
  const rows = result.meetings
    .map((m) => `<span class="time">${esc(m.time)}</span><span class="${m.skipped ? "skipped" : ""}">${esc(m.title)}${m.skipped ? ` · ${esc(m.skipped)}` : ""}</span>`)
    .join("");
  return `<div class="result" role="status">
    <div class="headline">${icon("check", "sm")}<span>Found “${esc(result.name)}” with ${plural(result.count, "meeting")} in the next day</span></div>
    ${rows ? `<div class="meetings">${rows}</div>` : ""}
  </div>`;
}

function addPanel() {
  return `<div class="panel">
    <div class="stack">
      <label class="label" for="add-url">Secret address in iCal format</label>
      <div class="row" style="flex-wrap:nowrap">
        <input id="add-url" class="field" style="flex:1" type="text" spellcheck="false" autocomplete="off"
          placeholder="https://calendar.google.com/calendar/ical/…/basic.ics" value="${esc(ui.addUrl)}" data-bind="addUrl" data-enter="testNew">
        <button class="btn" data-action="testNew" ${ui.addBusy || !ui.addUrl.trim() ? "disabled" : ""}>${ui.addBusy ? "Testing…" : "Test"}</button>
      </div>
      <p class="help">${helpText()}</p>
    </div>
    ${ui.addResult ? resultBlock(ui.addResult) : ""}
    <div class="row" style="justify-content:flex-end">
      <button class="btn" data-action="cancelAdd">Cancel</button>
      <button class="btn primary" data-action="confirmAdd" ${ui.addResult && ui.addResult.ok ? "" : "disabled"}>Add calendar</button>
    </div>
  </div>`;
}

function declinedSection(st) {
  const detected = st.detected_addresses;
  const email = st.user_email;
  const extra = email && !detected.includes(email.toLowerCase());
  let body;
  if (detected.length) {
    body = `<p class="muted" style="font-size:13px">Skipped automatically, using ${detected.map((a) => `<b>${esc(a)}</b>`).join(", ")} from your calendar${detected.length > 1 ? "s" : ""}.</p>`;
    if (extra) body += `<div class="row small">Also using <b>${esc(email)}</b><button class="link" data-action="clearEmail">Remove</button></div>`;
  } else if (email && !ui.addingAddress) {
    body = `<p class="muted" style="font-size:13px">Skipped using <b>${esc(email)}</b>.</p>`;
  } else {
    body = `<p class="muted" style="font-size:13px">Add the address your invites go to, so meetings you've declined are skipped.</p>`;
  }
  const showInput = ui.addingAddress || (!detected.length && !email);
  const input = `<div class="row" style="flex-wrap:nowrap">
      <input id="new-address" class="field" style="width:280px" type="text" inputmode="email" spellcheck="false" placeholder="you@example.com" aria-label="Email address"
        value="${esc(ui.newAddress)}" data-bind="newAddress" data-enter="saveEmail">
      <button class="btn" data-action="saveEmail">Save</button>
      ${ui.addingAddress ? `<button class="link" data-action="cancelEmail">Cancel</button>` : ""}
    </div>${ui.emailError ? `<p class="small" style="color:var(--warn)" role="alert">${esc(ui.emailError)}</p>` : ""}`;
  const offer = !showInput
    ? `<button class="link" style="align-self:flex-start" data-action="addAddress">${email && !extra ? "Change address" : "Add another address"}</button>`
    : "";
  return `<div class="stack" style="gap:6px;padding-top:4px"><div class="label">Declined meetings</div>${body}${showInput ? input : offer}</div>`;
}

function notesSection() {
  const intro = `<div class="intro"><h1>Starting notes</h1>
    <p>When a meeting begins, a card counts down in the top-right corner. Granola opens that meeting's note when it reaches zero, unless you cancel.</p></div>`;
  if (!data.settings) return intro + configBanner();
  const st = data.settings;
  const lengths = st.countdown_choices
    .map((n) => `<button role="radio" aria-checked="${n === st.countdown_seconds}" data-action="setCountdown" data-value="${n}">${n} s</button>`)
    .join("");
  const idle = st.idle_choices
    .map((n) => `<option value="${n}" ${n === st.idle_minutes ? "selected" : ""}>${plural(n, "minute")}</option>`)
    .join("");
  const chips = st.skip_words
    .map((w) => `<span class="chip" role="listitem">${esc(w)}<button data-action="removeWord" data-word="${esc(w)}" aria-label="Remove ${esc(w)}">${icon("x", "sm")}</button></span>`)
    .join("");
  return `${intro}
    <section class="stack" style="gap:8px" aria-labelledby="len-label">
      <div id="len-label" class="label">Countdown length</div>
      <div class="row" style="gap:14px"><div class="segmented" role="radiogroup" aria-labelledby="len-label">${lengths}</div>
        <button class="btn" data-action="preview">${icon("play", "sm")}Preview</button></div>
      <p class="small muted">Granola opens ${st.countdown_seconds} seconds after the card appears. Preview shows the card without opening Granola.</p>
    </section>
    <hr class="divider">
    <section class="stack" style="gap:8px" aria-labelledby="idle-label">
      <div id="idle-label" class="label">Only when you're at this PC</div>
      <div class="row" style="gap:10px"><label for="idle">Start notes only if the mouse or keyboard was used in the last</label>
        <select id="idle" class="field" data-change="setIdle">${idle}</select></div>
      <p class="small muted">A locked PC never starts notes. If you sit down after a meeting has begun, the card appears then, until the meeting's end time.</p>
    </section>
    <hr class="divider">
    <section class="stack" style="gap:10px" aria-labelledby="skip-label">
      <div id="skip-label" class="label">Skip meetings with these words in the title</div>
      <div class="chips" role="list">${chips || '<span class="small muted">None</span>'}</div>
      <div class="row" style="flex-wrap:nowrap">
        <input id="new-word" class="field" style="width:240px" type="text" placeholder="Add a word, like “standup”" aria-label="Add a word to skip"
          value="${esc(ui.newWord)}" data-bind="newWord" data-enter="addWord">
        <button class="btn" data-action="addWord">Add</button>
      </div>
      <p class="small muted">Not case-sensitive. All-day events are always skipped.</p>
    </section>`;
}

function aboutSection() {
  const s = data.status;
  const last = s.last_start;
  let lastRow;
  if (!last) {
    lastRow = row("play", "No notes started yet", "The first meeting you start notes for shows up here.");
  } else if (last.opened === false) {
    lastRow = row("warn", `Last start: ${last.title}, ${last.when}`, "Granola didn't open. A Granola update may have changed how notes open.", "warn");
  } else {
    lastRow = row("play", `Last start: ${last.title}, ${last.when}`,
      last.opened ? "Granola opened and started transcribing. If an update ever stops notes from opening, you'll get a notification." : "Waiting to see Granola open…");
  }
  const busy = ui.busy ? "disabled" : "";
  let watcher;
  if (s.state === "starting") {
    watcher = row("clock", "Starting…", s.detail);
  } else if (s.running) {
    watcher = row("check", "Running in the background", "It starts when you sign in to Windows. If you quit, it starts again at sign-in, or from the Start menu.", "",
      `<button class="btn small" data-action="quitApp" ${busy}>Quit</button>`);
  } else {
    watcher = row("warn", "Not running", "Meetings won't start notes until it runs again.", "warn", `<button class="btn small" data-action="startWatcher" ${busy}>Start</button>`);
  }
  return `<h1>About</h1>
    <div class="identity">${appMark(52)}<div class="stack" style="gap:2px"><div class="title">Granola Autostart</div><div class="muted" style="font-size:13px">Version ${esc(data.version)}</div></div></div>
    <div class="list">${watcher}${lastRow}${row("folder", "Logs", "Logs record meeting titles. Calendar addresses are never written to them.", "",
      `<button class="btn small" data-action="openLogs">Open log folder</button>`)}</div>
    <div class="row" style="gap:20px;font-size:13px"><button class="link" data-action="runSetup">Run setup again</button></div>
    <p class="footnote">Granola Autostart is an independent tool and is not made by Granola.</p>`;
}

function row(glyph, title, detail, tone = "", action = "") {
  return `<div class="item ${tone}"><span class="lead" ${tone ? "" : 'style="color:var(--accent-ink)"'}>${icon(glyph)}</span>
    <div class="body"><div class="name">${esc(title)}</div><div class="small muted">${esc(detail)}</div></div>
    ${action ? `<div class="actions">${action}</div>` : ""}</div>`;
}

// First-run setup

function setupView() {
  const step = { welcome: welcomeStep, calendar: calendarStep, found: foundStep, done: doneStep }[ui.step]();
  return `<div class="setup"><main>${errorBlock(ui.error)}${step.body}</main><div class="footer">${step.footer}</div></div>`;
}

function card() {
  return `<div class="card" aria-hidden="true">
    <div class="top">
      <div class="ring"><svg width="68" height="68" viewBox="0 0 68 68">
        <circle cx="34" cy="34" r="31.5" style="fill:none;stroke:var(--track);stroke-width:5"/>
        <circle cx="34" cy="34" r="31.5" style="fill:none;stroke:var(--accent);stroke-width:5;stroke-linecap:round;stroke-dasharray:158.34 197.92;stroke-dashoffset:-39.58"/></svg>
        <div class="count"><b>8</b><span>SEC</span></div></div>
      <div class="text"><div class="eyebrow">Granola · Starting notes</div><div class="meeting">Weekly product sync</div>
        <div class="small muted">Granola opens when the timer runs out.</div></div>
    </div>
    <hr class="divider">
    <div class="buttons"><span class="btn primary">Start now</span><span class="btn">Cancel</span></div>
  </div>`;
}

function welcomeStep() {
  return {
    body: `<div class="stack" style="gap:8px"><div class="eyebrow">Welcome</div>
        <h1>Start Granola notes when meetings begin</h1>
        <p class="muted">When a meeting starts, this card appears in the corner of your screen. When the ring runs out, Granola opens that meeting's note and starts transcribing. Cancel if you don't want notes.</p></div>
      ${card()}
      <ul class="points">
        <li>${icon("video")}Zoom calls work right away.</li>
        <li>${icon("calendar")}In-person and other meetings need your calendar. It takes about a minute.</li>
      </ul>`,
    footer: `<button class="btn large" data-action="zoomOnly">Zoom only for now</button>
      <button class="btn large primary" data-action="goStep" data-step="calendar">Connect a calendar</button>`,
  };
}

function calendarStep() {
  const google = ui.tab === "google";
  const steps = google
    ? [
        `Open <a href="#" data-action="openLink" data-url="https://calendar.google.com/calendar/r/settings">Google Calendar settings</a> in your browser.`,
        `Under <b>Settings for my calendars</b>, choose your calendar, then <b>Integrate calendar</b>.`,
        `Copy <b>Secret address in iCal format</b>. <span class="muted">Not there? Your Google Workspace admin may have turned it off.</span>`,
      ]
    : [
        `Open <a href="#" data-action="openLink" data-url="https://outlook.office.com/calendar/options/calendar/SharedCalendars">Outlook shared calendar settings</a> in your browser.`,
        `Under <b>Publish a calendar</b>, choose your calendar and <b>Can view all details</b>, then <b>Publish</b>.`,
        `Copy the <b>ICS</b> link. <span class="muted">Not there? Your Microsoft 365 admin may have turned publishing off.</span>`,
      ];
  const tab = (id, text) => `<button role="tab" aria-selected="${ui.tab === id}" data-action="tab" data-tab="${id}">${text}</button>`;
  return {
    body: `<div class="stack" style="gap:8px"><div class="eyebrow">Step 1 of 2</div><h1>Copy your calendar's secret address</h1></div>
      <div class="segmented" role="tablist" aria-label="Calendar app" style="align-self:flex-start">${tab("google", "Google Calendar")}${tab("outlook", "Outlook")}</div>
      <ol class="steps" role="tabpanel">${steps.map((text, i) => `<li><span class="num">${i + 1}</span><span>${text}</span></li>`).join("")}</ol>
      <div class="stack">
        <label class="label" for="setup-url">Paste the address</label>
        <input id="setup-url" class="field" type="text" spellcheck="false" autocomplete="off" placeholder="https://calendar.google.com/calendar/ical/…/basic.ics"
          value="${esc(ui.setupUrl)}" data-bind="setupUrl" data-enter="testSetup">
        ${errorBlock(ui.setupError)}
        <p class="footnote" style="margin-top:2px">${icon("lock", "sm")}Anyone with this address can read your calendar, so it's stored encrypted on this PC and never written to logs.</p>
      </div>`,
    footer: `<button class="btn large" data-action="goStep" data-step="welcome">Back</button>
      <button class="btn large primary" data-action="testSetup" ${ui.setupBusy || !ui.setupUrl.trim() ? "disabled" : ""}>${ui.setupBusy ? "Testing…" : "Test address"}</button>`,
  };
}

function foundStep() {
  const f = ui.found;
  const rows = f.meetings
    .map((m) => `<div role="listitem" class="row" style="gap:16px;padding:4px 0;flex-wrap:nowrap;${m.skipped ? "color:var(--muted)" : ""}">
        <span style="width:68px;flex-shrink:0;color:var(--muted)">${esc(m.time)}</span><span style="flex:1">${esc(m.title)}</span>
        ${m.skipped ? `<span class="small">${esc(m.skipped)}</span>` : ""}</div>`)
    .join("");
  const declined = f.owner
    ? `<p class="row muted" style="flex-wrap:nowrap;align-items:flex-start"><span style="color:var(--accent-ink);display:flex;padding-top:2px">${icon("check", "sm")}</span>
        <span>Meetings you decline are skipped, using <b>${esc(f.owner)}</b> from this calendar.</span></p>`
    : `<div class="stack"><label class="label" for="setup-email">Your email address</label>
        <input id="setup-email" class="field" style="width:320px" type="text" inputmode="email" spellcheck="false" placeholder="you@example.com"
          value="${esc(ui.setupEmail)}" data-bind="setupEmail">
        <p class="small muted">This calendar isn't named after your address, so add it to skip meetings you've declined.</p></div>`;
  return {
    body: `<div class="stack" style="gap:8px"><div class="eyebrow">Step 2 of 2</div><h1>Found your calendar</h1></div>
      <div class="list">
        <div class="item" style="align-items:center;gap:10px"><span class="lead">${icon("check")}</span>
          <span class="name">${esc(f.name)}</span><span class="muted" style="font-size:13px">${plural(f.count, "meeting")} in the next day</span></div>
        ${rows ? `<div role="list" aria-label="Next meetings" style="padding:8px 16px 12px 46px;border-top:1px solid var(--border);font-variant-numeric:tabular-nums">${rows}</div>` : ""}
      </div>
      ${declined}`,
    footer: `<span class="note">Add more calendars later in Settings.</span>
      <button class="btn large" data-action="goStep" data-step="calendar">Back</button>
      <button class="btn large primary" data-action="finish" ${ui.setupBusy ? "disabled" : ""}>Finish</button>`,
  };
}

function doneStep() {
  const next = ui.found && ui.found.meetings.find((m) => !m.skipped);
  const text = ui.zoomOnly
    ? "Zoom calls will start notes from now on. Add a calendar any time from Settings, so other meetings do too."
    : `Granola Autostart runs in the background and starts when you sign in to Windows.${next ? ` Your next meeting, ${esc(next.title)} at ${esc(next.time)}, will show the countdown card.` : ""}`;
  return {
    body: `<span class="big-check">${icon("tick")}</span>
      <div class="stack" style="gap:8px"><h1>You're all set</h1><p class="muted">${text}</p></div>
      <div class="stack" style="gap:10px;margin-top:8px"><div class="label">Find it in the system tray</div>
        <div class="tray" aria-hidden="true"><span>${icon("up", "sm")}</span><span class="ours">${appMark(16)}</span><span>${icon("wifi")}</span><span>${icon("speaker")}</span>
          <span class="clock">${clock()}</span></div>
        <div class="row small muted" style="align-self:flex-end;margin-right:152px">${icon("arrowUp", "sm")}Right-click it to pause or open Settings</div></div>`,
    footer: `<button class="btn large" data-action="preview">${icon("play", "sm")}Show me the card</button>
      <button class="btn large primary" data-action="toSettings">Done</button>`,
  };
}

function clock() {
  const now = new Date();
  return `${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}<br>${now.toLocaleDateString()}`;
}

// Actions

const actions = {
  section: (d) => { ui.section = d.section; render(); },
  toggleHelp: () => { ui.showHelp = !ui.showHelp; render(); },
  startAdd: () => { ui.adding = true; ui.addResult = null; render(); focusSoon("add-url"); },
  cancelAdd: () => { ui.adding = false; ui.addUrl = ""; ui.addResult = null; render(); },
  testNew: async () => {
    if (!ui.addUrl.trim() || ui.addBusy) return;
    ui.addBusy = true; render();
    ui.addResult = await api().test_address(ui.addUrl);
    ui.addBusy = false; render();
  },
  confirmAdd: async () => {
    data = await api().add_calendar(ui.addUrl);
    ui.adding = false; ui.addUrl = ""; ui.addResult = null; render();
  },
  testCalendar: async (d) => {
    ui.tests[d.id] = { busy: true }; render();
    ui.tests[d.id] = await api().test_calendar(d.id); render();
  },
  askRemove: (d) => { ui.confirmRemove = d.id; render(); },
  keepCalendar: () => { ui.confirmRemove = null; render(); },
  removeCalendar: async (d) => { data = await api().remove_calendar(d.id); ui.confirmRemove = null; delete ui.tests[d.id]; render(); },
  addAddress: () => { ui.addingAddress = true; ui.emailError = null; render(); focusSoon("new-address"); },
  cancelEmail: () => { ui.addingAddress = false; ui.newAddress = ""; ui.emailError = null; render(); },
  saveEmail: async () => {
    const result = await api().set_email(ui.newAddress);
    if (result.error) { ui.emailError = result.error; render(); return; }
    data = result; ui.addingAddress = false; ui.newAddress = ""; ui.emailError = null; render();
  },
  clearEmail: async () => { data = await api().set_email(""); render(); },
  setCountdown: async (d) => { data = await api().set_countdown(Number(d.value)); render(); },
  setIdle: async (_d, element) => { data = await api().set_idle(Number(element.value)); render(); },
  addWord: async () => { if (!ui.newWord.trim()) return; data = await api().add_word(ui.newWord); ui.newWord = ""; render(); },
  removeWord: async (d) => { data = await api().remove_word(d.word); render(); },
  preview: () => api().preview(),
  openLogs: () => api().open_logs(),
  openConfig: () => api().open_config(),
  openLink: (d) => api().open_link(d.url),
  runSetup: () => api().run_setup(),
  startWatcher: () => command(() => api().start_watcher()),
  togglePause: () => { ui.pauseMenu = !ui.pauseMenu; render(); if (ui.pauseMenu) focusSoon("pause-hour"); },
  pause: (d) => command(() => api().pause(d.length)),
  resume: () => command(() => api().resume()),
  quitApp: () => command(() => api().quit_app()),
  fix: () => { ui.section = data.status.fix_section || "calendars"; render(); },
  // Setup's Done turns this window into Settings, so another calendar can be added right away.
  toSettings: async () => { data = await api().open_settings(); ui.section = "calendars"; render(); },
  goStep: (d) => { ui.step = d.step; ui.setupError = null; render(); if (d.step === "calendar") focusSoon("setup-url"); },
  tab: (d) => { ui.tab = d.tab; render(); },
  testSetup: async () => {
    if (!ui.setupUrl.trim() || ui.setupBusy) return;
    ui.setupBusy = true; ui.setupError = null; render();
    const result = await api().test_address(ui.setupUrl);
    ui.setupBusy = false;
    if (result.ok) { ui.found = result; ui.step = "found"; } else { ui.setupError = result.error; }
    render();
  },
  finish: async () => {
    ui.setupBusy = true; render();
    data = await api().finish_setup(ui.setupUrl, ui.found && !ui.found.owner ? ui.setupEmail : null);
    ui.setupBusy = false; ui.zoomOnly = false; ui.step = "done"; render();
  },
  zoomOnly: async () => { data = await api().finish_setup(null, null); ui.zoomOnly = true; ui.step = "done"; render(); },
};

// A Python call can fail, most often on a config.json that can't be read. Every action recovers the
// same way, so no button stays stuck on "Testing…" and the page says why.
async function perform(name, ...args) {
  const action = actions[name];
  if (!action) return;
  ui.actions += 1;
  ui.error = null;
  try {
    await action(...args);
  } catch {
    ui.busy = ui.addBusy = ui.setupBusy = false;
    for (const id of Object.keys(ui.tests)) if (ui.tests[id].busy) delete ui.tests[id];
    try { data = await api().load(); } catch { /* keep the page as it was */ }
    ui.error = data.config_error ? `${data.config_error}.` : "That didn't work. Try again, or open the log folder from About.";
    render();
  } finally {
    // A poll that began while this ran may bring back data from before it, so it is dropped too.
    ui.actions += 1;
  }
}

function errorBlock(message) {
  return message ? `<div class="result error" role="alert"><div class="headline">${icon("warn", "sm")}<span>${esc(message)}</span></div></div>` : "";
}

// Pause, Resume, Start, and Quit wait for the watcher to act, so their buttons stay disabled meanwhile.
async function command(call) {
  if (ui.busy) return;
  ui.busy = true; ui.pauseMenu = false; render();
  try { data = await call(); } finally { ui.busy = false; render(); }
}

function closePauseMenu(returnFocus) {
  ui.pauseMenu = false; render();
  if (returnFocus) focusSoon("pause-toggle");
}

document.addEventListener("keydown", (event) => {
  if (!ui.pauseMenu) return;
  const items = [...document.querySelectorAll('.menu [role="menuitem"]')];
  const at = items.indexOf(document.activeElement);
  if (event.key === "Escape") { event.preventDefault(); closePauseMenu(true); }
  else if (event.key === "Tab") closePauseMenu(false);
  else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    items[(at + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
  }
});

// WebView2's own right-click menu gives text fields Cut, Copy, and Paste. Anywhere else it would offer Back and Reload.
document.addEventListener("contextmenu", (event) => {
  if (!event.target.closest("input, textarea")) event.preventDefault();
});

const app = document.getElementById("app");
app.addEventListener("click", (event) => {
  // A click anywhere outside the open Pause menu closes it.
  if (ui.pauseMenu && !event.target.closest(".menu-anchor")) { ui.pauseMenu = false; render(); }
  const target = event.target.closest("[data-action]");
  if (!target || target.disabled) return;
  event.preventDefault();
  perform(target.dataset.action, target.dataset, target);
});
app.addEventListener("input", (event) => {
  const bind = event.target.dataset.bind;
  if (!bind) return;
  ui[bind] = event.target.value;
  // A changed address needs testing again, and the Test buttons enable once something is typed.
  if (bind === "addUrl") { ui.addResult = null; render(); }
  if (bind === "setupUrl") render();
});
app.addEventListener("keydown", (event) => {
  const action = event.target.dataset.enter;
  if (event.key === "Enter" && action) { event.preventDefault(); perform(action, event.target.dataset, event.target); }
});
app.addEventListener("change", (event) => {
  const action = event.target.dataset.change;
  if (action) perform(action, event.target.dataset, event.target);
});
