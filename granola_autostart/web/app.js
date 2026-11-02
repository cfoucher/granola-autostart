// Settings and first-run setup. Data, and every change, go through window.pywebview.api (settings_app.Api).
"use strict";

const ICONS = {
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  timer: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2.5M9 2h6"/>',
  sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  power: '<path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/>',
  contrast: '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" style="fill: currentColor"/>',
  logout: '<path d="M9 4H5.5A1.5 1.5 0 0 0 4 5.5v13A1.5 1.5 0 0 0 5.5 20H9M16 8l4 4-4 4M20 12H9"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
  minus: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
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
  people: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.3a6.5 6.5 0 0 1 3.5 5.7"/>',
  browser: '<rect x="2.5" y="4" width="19" height="16" rx="2"/><path d="M2.5 9h19M6 6.5h.01M9 6.5h.01"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>',
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
// A row's address editor saves as you add or remove an address. Add calendar's (add*) and setup's (found*) editors keep
// their list in the page and save it once, on Add calendar or Finish. Their *Suggested list is what Test suggested, so
// an editor left as it is sends null and the calendar stays automatic. Setup's call switches (calls, by provider) are kept
// the same way and saved with Finish or Calls only for now.
const ui = {
  section: "calendars",
  adding: false, addUrl: "", addResult: null, addBusy: false, saving: false,
  addList: [], addSuggested: [], addDraft: "", addError: null,
  open: null, newAddress: "", addressError: null,
  tests: {}, confirmRemove: null, showHelp: false,
  newWord: "",
  chosen: {}, choosing: {},
  pauseMenu: false, busy: false, error: null, actions: 0, checking: false, installing: false, installTimer: null,
  step: "welcome", tab: "google", setupUrl: "", found: null, setupBusy: false, setupError: null, callsOnly: false, calls: null,
  foundList: [], foundSuggested: [], foundEditing: false, foundDraft: "", foundError: null,
};
const api = () => window.pywebview.api;

window.addEventListener("pywebviewready", async () => {
  data = await api().load();
  ui.section = data.section;
  // Setup's switches start from what is saved: Zoom on and the others off on a first run, and the last choices on a rerun.
  if (data.settings) ui.calls = Object.fromEntries(data.settings.calls.map((c) => [c.provider, c.on]));
  render();
  setInterval(poll, 2000);
});

async function poll() {
  if (!data || data.page !== "settings" || document.hidden) return;
  // An action that ran meanwhile already loaded newer data than this poll may bring back.
  const actionsBefore = ui.actions;
  const fresh = await api().load().catch(() => null);
  if (!fresh || ui.actions !== actionsBefore) return;
  const changed = JSON.stringify([fresh.status, fresh.settings, fresh.update, fresh.config_error, fresh.theme, fresh.appearance, fresh.start_at_sign_in]) !==
    JSON.stringify([data.status, data.settings, data.update, data.config_error, data.theme, data.appearance, data.start_at_sign_in]);
  data = fresh;
  if (changed) render();
}

// What scrolls: the page, or with many calendars the list under the fixed header.
const scrollers = () => [document.querySelector("main"), document.querySelector(".scroll")];
let lastView = "";

function render() {
  document.documentElement.dataset.theme = data.theme;
  const active = document.activeElement;
  const keep = active && active.id ? { id: active.id, start: safe(() => active.selectionStart), end: safe(() => active.selectionEnd) } : null;
  const offsets = scrollers().map((element) => (element ? element.scrollTop : 0));
  document.getElementById("app").innerHTML = data.page === "setup" ? setupView() : settingsView();
  // A redraw keeps the page where it was, unless it is another page.
  const view = `${data.page}/${ui.section}/${ui.step}`;
  if (view === lastView) scrollers().forEach((element, i) => { if (element) element.scrollTop = offsets[i]; });
  lastView = view;
  if (keep) {
    const element = document.getElementById(keep.id);
    if (element) {
      element.focus({ preventScroll: true });
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

// Calendar addresses, checked as settings_app._EMAIL does, so a typo shows before anything is saved.
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const NOT_AN_ADDRESS = "That doesn't look like an email address.";
const sameAddress = (a, b) => a.toLowerCase() === b.toLowerCase();
const sameList = (a, b) => a.length === b.length && a.every((x) => b.some((y) => sameAddress(x, y)));
// What add_calendar and finish_setup are sent: null while the editor holds what Test suggested (or nothing), so the
// calendar stays automatic and nothing more is downloaded.
const chosenList = (list, suggested) => (!list.length || sameList(list, suggested) ? null : list);

function resetAdd() {
  ui.adding = false; ui.addUrl = ""; ui.addResult = null; ui.addList = []; ui.addSuggested = []; ui.addDraft = ""; ui.addError = null; ui.showHelp = false;
}

// Add calendar's and setup's editors hold their list in the page: this adds the address typed in the field, or says why not.
function addTyped(listKey, draftKey, errorKey, inputId) {
  const text = ui[draftKey].trim();
  if (!text) { focusSoon(inputId); return; }
  if (!EMAIL.test(text)) { ui[errorKey] = NOT_AN_ADDRESS; render(); return; }
  if (!ui[listKey].some((a) => sameAddress(a, text))) ui[listKey] = [...ui[listKey], text];
  ui[draftKey] = ""; ui[errorKey] = null;
  render(); focusSoon(inputId);
}

function removeTyped(listKey, address, chipPrefix, inputId) {
  const at = ui[listKey].indexOf(address);
  ui[listKey] = ui[listKey].filter((a) => a !== address);
  render();
  // Focus moves to the chip that took its place, or else to the field.
  (document.getElementById(`${chipPrefix}${at}`) || document.getElementById(inputId))?.focus();
}

// The editor's list with the address typed but not yet added counted in, or why it can't be: the person who types
// an address and goes straight to Add calendar or Finish means to have it.
function withTyped(list, draft) {
  const text = draft.trim();
  if (!text) return { list };
  if (!EMAIL.test(text)) return { error: NOT_AN_ADDRESS };
  return { list: list.some((a) => sameAddress(a, text)) ? list : [...list, text] };
}

// Settings

function settingsView() {
  // With a config that can't be read the page says why in the section's banner, so the error would only repeat it.
  const err = errorBlock(data.config_error ? null : ui.error);
  const body = ui.section === "calendars" ? calendarsSection(err) : err + { calls: callsSection, notes: notesSection, general: generalSection }[ui.section]();
  const nav = (id, glyph, text) =>
    `<button class="nav" data-action="section" data-section="${id}" ${ui.section === id ? 'aria-current="page"' : ""}>${icon(glyph)}${text}</button>`;
  return `<div class="window">
    <nav class="rail" aria-label="Settings sections">
      ${nav("calendars", "calendar", "Calendars")}
      ${nav("calls", "video", "Calls")}
      ${nav("notes", "timer", "Starting notes")}
      ${nav("general", "sliders", "General")}
      ${statusBox()}
    </nav>
    <main class="${ui.section}${manyCalendars() && ui.section === "calendars" ? " many" : ""}">${body}</main>
  </div>`;
}

// A switch, as in the design: the button is the track and its knob is the span inside it.
// ``label`` is its aria-labelledby or aria-label attribute.
const toggle = (id, label, on, action, attrs = "") =>
  `<button id="${id}" class="switch" role="switch" aria-checked="${on}" ${label} data-action="${action}" ${attrs}><span class="knob"></span></button>`;

// The status box at the foot of the side panel: the Autostart notes switch (which can't change while config.json can't
// be read), then the state. Its buttons are the tray menu's: Pause, Resume, Start, and Fix. Off has none while the app
// runs (a stopped app shows Start), and neither does Starting.
function statusBox() {
  const s = data.status;
  const label = {
    watching: "Watching", notes: "Taking notes", paused: "Paused", attention: "Needs attention", stopped: "Not running", starting: "Starting…", off: "Off",
  }[s.state];
  const busy = ui.busy ? "disabled" : "";
  let controls = "";
  if (s.state === "stopped") {
    controls = `<button class="btn mini primary" data-action="startWatcher" ${busy}>Start</button>`;
  } else if (s.state !== "starting" && s.state !== "off") {
    const fix = s.state === "attention" ? `<button class="btn mini primary" data-action="fix">Fix</button>` : "";
    const resume = `<button class="btn mini ${fix ? "" : "primary"}" data-action="resume" ${busy}>Resume</button>`;
    const restOfToday = !fix && !s.paused_today ? `<button class="link small" data-action="pause" data-length="today" ${busy}>Rest of today</button>` : "";
    controls = fix + (s.paused ? resume + restOfToday : pauseMenu(busy));
  }
  return `<div class="rail-status">
    <div class="power"><span id="power-label">Autostart notes</span>${toggle("power-switch", 'aria-labelledby="power-label"', s.autostart_notes, "setNotes", data.config_error ? "disabled" : "")}</div>
    <hr class="divider">
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

// ``effect``: what can't change meanwhile, in the section the banner is in.
function configBanner(effect = "nothing here can change") {
  if (!data.config_error) return "";
  return `<div class="banner" role="alert">${icon("warn")}<div class="stack" style="gap:8px">
    <span>${esc(data.config_error)}. The app keeps the last settings it could read, but ${effect} until config.json is fixed.</span>
    <div class="row"><button class="btn small" data-action="openConfig">Open config.json</button></div></div></div>`;
}

const INTRO = "Meetings on these calendars start notes when they begin.";
const ORDER_NOTE = "Ones that need attention come first, and ones that are off come last.";
// From this many calendars the header holds still and the list scrolls under it.
const MANY = 5;
// Where a calendar's addresses come from, as the page says it, for the editors in a row, Add calendar, and setup.
// Python works out which one applies (settings_app.address_note), with "pending" for a calendar that hasn't downloaded yet and
// "off" for one that is off and hasn't downloaded since the watcher started. {older} and {calendars} are filled in by noteText.
const NOTES = {
  own: "Read from this calendar{older}. Add any other addresses your invites go to, such as aliases.",
  list: "Add any other addresses your invites on this calendar go to, such as aliases.",
  copied: "Copied from your other {calendars}{older}, as this one doesn't name an address.",
  kept: "Saved by an older version of this app, as this calendar doesn't name an address. Add any others your invites go to.",
  none: "This calendar doesn't name an address. Add the ones your invites go to, so meetings you've declined are skipped.",
  pending: "This calendar hasn't downloaded yet. Its addresses appear here once it has.",
  off: "This calendar is off. Its addresses appear here once it's switched on.",
};
const OLDER_ADDRESS = "the address an older version of this app saved";

// "calendar" or "calendars" for a "copied" note, by how many calendars lent an address (``lent``, which isn't the chips).
const calendarsLent = (r) => (r.lent > 1 ? "calendars" : "calendar");

// The copy for a calendar's note, from a calendar row or a Test result (note, older, lent).
function noteText(r) {
  return NOTES[r.note].replace("{calendars}", calendarsLent(r)).replace("{older}", r.older ? `, plus ${OLDER_ADDRESS}` : "");
}

const manyCalendars = () => Boolean(data.settings) && data.settings.calendars.length >= MANY;

function calendarsSection(err) {
  if (!data.settings) return `${err}<div class="intro"><h1>Calendars</h1><p>${INTRO}</p></div>${configBanner()}`;
  const calendars = data.settings.calendars;
  const many = manyCalendars();
  const summary = many ? `${calendars.length} calendars, ${calendars.filter((c) => c.enabled).length} on. ${ORDER_NOTE}` : INTRO;
  const head = `<div class="page-head"><div class="intro"><h1>Calendars</h1><p>${esc(summary)}</p></div>
    <button id="add-calendar" class="btn" data-action="startAdd" ${ui.adding ? "disabled" : ""}>${icon("plus", "md")}Add calendar</button></div>`;
  const list = calendars.length
    ? `<div class="list" role="list" aria-label="Calendars">${calendars.map(calendarRow).join("")}</div>`
    : `<div class="list"><p class="empty">No calendars yet. Add one so in-person and other meetings start notes too.</p></div>`;
  const body = `${ui.adding ? addPanel() : ""}${list}
    <p class="footnote">${icon("lock", "sm")}Calendar addresses are stored encrypted on this PC. Changes save as you make them.</p>`;
  return many ? `${head}<div class="scroll">${err}${body}</div>` : `${err}${head}${body}`;
}

function helpText() {
  return `Google Calendar: open Settings, choose the calendar under “Settings for my calendars”, then Integrate calendar, and copy
    “Secret address in iCal format”. Outlook: Settings, Shared calendars, Publish a calendar, then copy the ICS link.`;
}

function calendarRow(c) {
  const open = ui.open === c.id;
  const lineIcon = { ok: icon("check", "sm"), warn: icon("warn", "sm"), pending: icon("clock", "sm"), off: icon("minus", "sm") }[c.state];
  return `<div class="cal ${c.state}" role="listitem">
    <div class="item cal-main">
      <span class="lead">${icon("calendar", "lg")}</span>
      <div class="body">
        <div class="title"><span class="name" title="${esc(c.name)}">${esc(c.name)}</span><span class="provider">${esc(c.provider)}</span></div>
        <div class="line ${c.state}">${lineIcon}<span>${esc(c.line)}</span></div>
      </div>
      <div class="actions">
        ${toggle(`cal-switch-${c.id}`, `aria-label="Start notes for ${esc(c.name)}"`, c.enabled, "toggleCalendar", `data-id="${c.id}"`)}
        <button id="cal-open-${c.id}" class="icon-btn" data-action="toggleRow" data-id="${c.id}" aria-expanded="${open}"
          ${open ? `aria-controls="cal-detail-${c.id}"` : ""} aria-label="Settings for ${esc(c.name)}">${icon("down", "md")}</button>
      </div>
    </div>
    ${open ? rowDetail(c) : ""}
  </div>`;
}

// An open row: who it skips declines for, then Test and Remove calendar.
function rowDetail(c) {
  const confirming = ui.confirmRemove === c.id;
  const buttons = confirming
    ? `<span class="small muted">Remove?</span>
       <button id="remove-${c.id}" class="btn small" data-action="removeCalendar" data-id="${c.id}">Remove</button>
       <button id="keep-${c.id}" class="btn small" data-action="keepCalendar" data-id="${c.id}">Keep</button>`
    : `<button id="test-${c.id}" class="btn small" data-action="testCalendar" data-id="${c.id}">Test</button>
       <button id="askremove-${c.id}" class="quiet" data-action="askRemove" data-id="${c.id}">${icon("trash", "sm")}Remove calendar</button>`;
  // Until the calendar has downloaded nobody knows whether it names an address, and a list typed now would replace it.
  const addresses = c.note === "pending" || c.note === "off"
    ? `<div class="stack"><div class="label sm">Skip meetings declined by</div><p class="small muted">${esc(noteText(c))}</p></div>`
    : addressEditor({
      key: c.id, chips: c.declined,
      // An automatic calendar's only address would come straight back, so it can't be taken away.
      removable: !(c.automatic && c.declined.length === 1),
      removeAction: "removeDeclined", addAction: "addDeclined", bind: "newAddress", draft: ui.newAddress, error: ui.addressError,
      label: `Add an address for ${c.name}`, note: noteText(c),
    });
  return `<div id="cal-detail-${c.id}" class="detail" role="group" aria-label="Settings for ${esc(c.name)}">
    ${addresses}
    <div class="row">${buttons}</div>
    ${testOutcome(ui.tests[c.id])}
  </div>`;
}

// "Skip meetings declined by", the addresses as removable chips, and a field to add another. A calendar's row, Add
// calendar, and setup each pass their own ids (key) and the actions that change the list they hold.
function addressEditor({ key, chips, removable, removeAction, addAction, bind, draft, error, label, note }) {
  const chip = (address, i) => `<span class="chip ${removable ? "" : "fixed"}" role="listitem"><span class="text" title="${esc(address)}">${esc(address)}</span>${
    removable
      ? `<button id="chip-${key}-${i}" data-action="${removeAction}" data-id="${key}" data-address="${esc(address)}" aria-label="Remove ${esc(address)}">${icon("x", "sm")}</button>`
      : ""}</span>`;
  return `<div class="stack">
    <div id="declined-${key}" class="label sm">Skip meetings declined by</div>
    <div class="editor">
      ${chips.length ? `<span class="chips" role="list" aria-labelledby="declined-${key}">${chips.map(chip).join("")}</span>` : ""}
      <span class="adder">
        <input id="address-${key}" class="field address" type="text" inputmode="email" spellcheck="false" autocomplete="off"
          placeholder="Add an address, like an alias" aria-label="${esc(label)}" ${error ? `aria-invalid="true" aria-describedby="address-error-${key}"` : ""}
          value="${esc(draft)}" data-bind="${bind}" data-enter="${addAction}" data-id="${key}">
        <button id="address-add-${key}" class="btn" data-action="${addAction}" data-id="${key}">Add</button>
      </span>
    </div>
    ${error ? `<p id="address-error-${key}" class="small warn-text" role="alert">${esc(error)}</p>` : ""}
    <p class="small muted">${esc(note)}</p>
  </div>`;
}

function testOutcome(result) {
  if (!result) return "";
  if (result.busy) return `<div class="line">${icon("clock", "sm")}<span>Testing…</span></div>`;
  return `<div>${resultBlock(result)}</div>`;
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

// Add calendar's panel, at the top of the list. After a good Test it holds the addresses declines will use, as
// the page's own copy, so nothing is saved until Add calendar.
function addPanel() {
  const result = ui.addResult;
  const ok = Boolean(result && result.ok);
  // The suggestion's only address would come straight back, as it does for an automatic calendar.
  const removable = !(ui.addSuggested.length === 1 && sameList(ui.addList, ui.addSuggested));
  return `<div class="panel">
    <div class="stack">
      <div class="row between"><label class="label" for="add-url">Secret address in iCal format</label>
        <button id="help-toggle" class="link small" data-action="toggleHelp" aria-expanded="${ui.showHelp}">Where do I find it?</button></div>
      <div class="row" style="flex-wrap:nowrap">
        <input id="add-url" class="field" style="flex:1" type="text" spellcheck="false" autocomplete="off"
          placeholder="https://calendar.google.com/calendar/ical/…/basic.ics" value="${esc(ui.addUrl)}" data-bind="addUrl" data-enter="testNew">
        <button id="add-test" class="btn large" data-action="testNew" ${ui.addUrl.trim() ? "" : "disabled"}>${ui.addBusy ? "Testing…" : "Test"}</button>
      </div>
      ${ui.showHelp ? `<p class="help">${helpText()}</p>` : ""}
    </div>
    ${result ? resultBlock(result) : ""}
    ${ok ? addressEditor({
      key: "new", chips: ui.addList, removable, removeAction: "removeNewAddress", addAction: "addNewAddress", bind: "addDraft", draft: ui.addDraft,
      error: ui.addError, label: "Add an address for this calendar", note: noteText(result),
    }) : ""}
    <div class="row" style="justify-content:flex-end">
      <button id="add-cancel" class="btn" data-action="cancelAdd">Cancel</button>
      <button id="add-confirm" class="btn primary" data-action="confirmAdd" ${ok ? "" : "disabled"}>Add calendar</button>
    </div>
  </div>`;
}

// Calls: the settings page and setup's second step share the rows and the warning.
const CALL_ICONS = { zoom: "video", teams: "people", meet: "browser" };
const callFound = (c) => (c.found === null
  ? "In Chrome or Edge. Join with the Meet tab in front, so the call is recognised."
  : `Desktop app, ${c.found ? "found" : "not found"} on this PC`);

// A switch for each call, with whether its app was found on this PC. ``action`` changes a switch by its data-provider.
function callList(calls, action) {
  const rows = calls.map((c) => `<div class="item call ${c.on ? "on" : "off"}" role="listitem">
      <span class="lead">${icon(CALL_ICONS[c.provider], "lg")}</span>
      <div class="body"><div id="call-name-${c.provider}" class="name">${esc(c.name)}</div><div class="small muted">${esc(callFound(c))}</div></div>
      ${toggle(`call-switch-${c.provider}`, `aria-labelledby="call-name-${c.provider}"`, c.on, action, `data-provider="${c.provider}"`)}
    </div>`);
  return `<div class="list" role="list" aria-label="Calls">${rows.join("")}</div>`;
}

const caution = (text) => `<p class="caution" role="status">${icon("warn", "sm")}${text}</p>`;

function callsSection() {
  const intro = `<div class="intro"><h1>Calls</h1>
    <p>Joining one of these calls starts notes, with the same countdown card. Meetings on your calendars start notes at their start time either way.</p></div>`;
  if (!data.settings) return intro + configBanner();
  const calls = data.settings.calls;
  return `${intro}${callList(calls, "toggleCall")}
    ${calls.some((c) => c.on) ? "" : caution("Calls won't start notes. Meetings on your calendars still will.")}
    <p class="footnote top">${icon("mic", "sm")}Zoom calls are noticed from Zoom's meeting window, and Teams and Meet calls from which app is using the microphone. Granola Autostart never listens to or records audio.</p>`;
}

function notesSection() {
  const intro = `<div class="intro"><h1>Starting notes</h1>
    <p>When a meeting begins or you join a call, a card counts down in the top-right corner. Granola opens that meeting's note when it reaches zero, unless you cancel.</p></div>`;
  if (!data.settings) return intro + configBanner();
  const st = data.settings;
  const length = "countdown" in ui.chosen ? ui.chosen.countdown : st.countdown_seconds;
  const lengths = radioButtons(st.countdown_choices.map((n) => [n, `${n} s`]), length, "setCountdown", (n) => `countdown-${n}`);
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
      <p class="small muted">Granola opens ${length} seconds after the card appears. Preview shows the card without opening Granola.</p>
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
      <div class="stack" style="gap:4px"><div id="skip-label" class="label">Skip meetings and calls with these words in the title</div>
        <p class="small muted">Checked against the titles of meetings on your calendars. A call during a meeting follows that meeting, so it is skipped when the meeting is. A call that matches no meeting is checked by its own name.</p></div>
      <div class="chips" role="list">${chips || '<span class="small muted">None</span>'}</div>
      <div class="row" style="flex-wrap:nowrap">
        <input id="new-word" class="field" style="width:240px" type="text" placeholder="Add a word, like “standup”" aria-label="Add a word to skip"
          value="${esc(ui.newWord)}" data-bind="newWord" data-enter="addWord">
        <button class="btn" data-action="addWord">Add</button>
      </div>
      <p class="small muted">Not case-sensitive. All-day events are always skipped.</p>
    </section>`;
}

const APPEARANCES = [["auto", "Auto"], ["light", "Light"], ["dark", "Dark"]];

// The buttons of a role="radiogroup", from [value, text] pairs. Tab stops once on the group, at the chosen one (the first when
// none is), and the arrow keys move between them and choose (see moveRadio). ``idFor`` names a button from its value.
function radioButtons(items, current, action, idFor, disabled = "") {
  const stop = Math.max(0, items.findIndex(([value]) => value === current));
  return items
    .map(([value, text], i) => `<button id="${idFor(value)}" role="radio" aria-checked="${value === current}" tabindex="${i === stop ? 0 : -1}"
      data-action="${action}" data-value="${value}" ${disabled}>${text}</button>`)
    .join("");
}

function generalSection() {
  const s = data.status;
  const last = s.last_start;
  let lastRow;
  if (!last) {
    lastRow = row("play", "No notes started yet", "The first meeting you start notes for shows up here.");
  } else if (last.opened === false) {
    lastRow = row("warn", `Last start: ${last.title}, ${last.when}`, "Granola didn't open. A Granola update may have changed how notes open.", "warn");
  } else {
    lastRow = row("play", `Last start: ${last.title}, ${last.when}`, last.opened ? "Granola opened and started transcribing." : "Waiting to see Granola open…");
  }
  // Run from source there is no installed exe to start at sign-in, so there is no switch.
  const signIn = !data.sign_in_available ? "" : `<div class="item"><span class="lead">${icon("power")}</span>
    <div class="body"><div id="signin-name" class="name">Start when you sign in to Windows</div>
      <div class="small muted">Puts Granola Autostart in the tray at sign-in. Whether it starts notes is up to the Autostart notes switch.</div></div>
    ${toggle("signin-switch", 'aria-labelledby="signin-name"', data.start_at_sign_in, "setSignIn")}</div>`;
  // Nor has a checkout a Start menu shortcut to start it again from.
  const quitNote = data.sign_in_available
    ? "It starts again from the Start menu, or at sign-in if that switch is on."
    : "It stays off until you start it again.";
  // Appearance is saved in config.json, which can't be changed while it can't be read.
  const appearanceNow = "appearance" in ui.chosen ? ui.chosen.appearance : data.appearance;
  const choices = radioButtons(APPEARANCES, appearanceNow, "setAppearance", (id) => `appearance-${id}`, data.config_error ? "disabled" : "");
  const appearance = `<div class="item"><span class="lead">${icon("contrast")}</span>
    <div class="body" style="gap:8px">
      <div class="stack" style="gap:2px"><div id="appearance-name" class="name">Appearance</div>
        <div class="small muted">For Settings, setup, the countdown card, and the tray menu. Auto follows your Windows setting.</div></div>
      <div class="segmented" role="radiogroup" aria-labelledby="appearance-name" style="align-self:flex-start">${choices}</div>
    </div></div>`;
  // Quit only means something while the app runs, and not in the moment it is starting.
  const quittable = s.running && s.state !== "starting" && !ui.busy;
  return `<h1>General</h1>
    ${configBanner("Appearance and the Autostart notes switch can't change")}
    <div class="list tight">${signIn}${appearance}</div>
    <div class="list tight">${updateRow()}${lastRow}${row("folder", "Logs", "Logs record meeting titles. Calendar addresses are never written to them.", "",
      `<button class="btn small" data-action="openLogs">Open log folder</button>`)}${row("logout", "Quit the app", quitNote, "",
      `<button class="btn small" data-action="quitApp" ${quittable ? "" : "disabled"}>Quit</button>`)}</div>
    <div class="row" style="margin-top:auto;justify-content:space-between;gap:6px 20px">
      <button class="link" data-action="runSetup">Run setup again</button>
      <p class="small muted">Granola Autostart is an independent tool and is not made by Granola.</p>
    </div>`;
}

// This version, and whether a newer one is ready to install. Checking and installing wait for the watcher's
// answer in Python, so ui.checking and ui.installing keep the row as it is meanwhile.
function updateRow() {
  const u = data.update;
  const warn = (text) => `<div class="line warn">${icon("warn", "sm")}<span>${esc(text)}</span></div>`;
  let lines = "";
  let buttons = "";
  if (u.source) {
    lines = `<div class="small muted">Running from source, so updates come from git.</div>`;
  } else if (ui.installing || u.installing) {
    lines = `<div class="small muted">Installing… Granola Autostart restarts in a moment.</div>`;
  } else {
    if (u.available) {
      lines = `<div class="small">Version ${esc(u.available)} is available</div>`;
      if (u.card_showing) lines += `<div class="small muted">You can install it once the countdown card closes.</div>`;
      buttons = (u.page ? `<button class="link" data-action="openLink" data-url="${esc(u.page)}">What's new</button>` : "") +
        `<button class="btn small primary" data-action="installUpdate" ${u.card_showing ? "disabled" : ""}>Install update</button>`;
    } else {
      if (!u.error) lines = `<div class="small muted">${u.checked ? `You're up to date. Checked ${esc(u.checked)}; updates are` : "Updates are"} checked once a day.</div>`;
      buttons = `<button class="btn small" data-action="checkUpdates" ${ui.checking ? "disabled" : ""}>${ui.checking ? "Checking…" : "Check now"}</button>`;
    }
    lines += (u.error ? warn(u.error) : "") + (u.install_error ? warn(u.install_error) : "");
  }
  return `<div class="item" style="align-items:center"><span class="lead">${appMark(32)}</span>
    <div class="body"><div class="name">Granola Autostart ${esc(data.version)}</div>${lines}</div>
    ${buttons ? `<div class="actions" style="gap:12px">${buttons}</div>` : ""}</div>`;
}

function row(glyph, title, detail, tone = "", action = "") {
  return `<div class="item ${tone}"><span class="lead" ${tone ? "" : 'style="color:var(--accent-ink)"'}>${icon(glyph)}</span>
    <div class="body"><div class="name">${esc(title)}</div><div class="small muted">${esc(detail)}</div></div>
    ${action ? `<div class="actions">${action}</div>` : ""}</div>`;
}

// First-run setup

function setupView() {
  const step = { welcome: welcomeStep, calls: callsStep, calendar: calendarStep, found: foundStep, done: doneStep }[ui.step]();
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
        <li>${icon("video")}Calls on Zoom, Teams or Google Meet can start notes when you join.</li>
        <li>${icon("calendar")}In-person and other meetings need your calendar. It takes about a minute.</li>
      </ul>`,
    footer: `<button class="btn large primary" data-action="goStep" data-step="calls">Get started</button>`,
  };
}

// Step 1 of 3: which calls start notes. The switches are kept here, and saved with Finish or Calls only for now.
function callsStep() {
  // Without a readable config.json there are no switches to show, and the page says why.
  const calls = data.settings ? data.settings.calls.map((c) => ({ ...c, on: Boolean(ui.calls && ui.calls[c.provider]) })) : null;
  const none = calls && !calls.some((c) => c.on);
  return {
    body: `<div class="stack" style="gap:8px"><div class="eyebrow">Step 1 of 3</div><h1>Which calls should start notes?</h1>
        <p class="muted">When you join one, the countdown card appears and Granola starts notes for the call. Meetings on your calendar start notes at their start time either way.</p></div>
      ${calls ? callList(calls, "toggleSetupCall") : configBanner()}
      ${none ? caution("With every call off, only meetings on your calendar will start notes.") : ""}
      <p class="footnote top">${icon("mic", "sm")}Teams and Meet calls are noticed by which app is using your microphone. Granola Autostart never listens to or records audio.</p>`,
    footer: `<button class="btn large" data-action="goStep" data-step="welcome">Back</button>
      ${calls && !none ? `<button id="calls-only" class="btn large" data-action="callsOnly" ${ui.setupBusy ? "disabled" : ""}>Calls only for now</button>` : ""}
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
    body: `<div class="stack" style="gap:8px"><div class="eyebrow">Step 2 of 3</div><h1>Copy your calendar's secret address</h1></div>
      <div class="segmented" role="tablist" aria-label="Calendar app" style="align-self:flex-start">${tab("google", "Google Calendar")}${tab("outlook", "Outlook")}</div>
      <ol class="steps" role="tabpanel">${steps.map((text, i) => `<li><span class="num">${i + 1}</span><span>${text}</span></li>`).join("")}</ol>
      <div class="stack">
        <label class="label" for="setup-url">Paste the address</label>
        <input id="setup-url" class="field" type="text" spellcheck="false" autocomplete="off" placeholder="https://calendar.google.com/calendar/ical/…/basic.ics"
          value="${esc(ui.setupUrl)}" data-bind="setupUrl" data-enter="testSetup">
        ${errorBlock(ui.setupError)}
        <p class="footnote" style="margin-top:2px">${icon("lock", "sm")}Anyone with this address can read your calendar, so it's stored encrypted on this PC and never written to logs.</p>
      </div>`,
    footer: `<button class="btn large" data-action="goStep" data-step="calls">Back</button>
      <button class="btn large primary" data-action="testSetup" ${ui.setupBusy || !ui.setupUrl.trim() ? "disabled" : ""}>${ui.setupBusy ? "Testing…" : "Test address"}</button>`,
  };
}

const names = (list) => (list.length < 2 ? list.map(esc).join("") : `${list.slice(0, -1).map(esc).join(", ")} and ${esc(list[list.length - 1])}`);

// Setup's one-line version of where the addresses come from, to go between commas in its sentence, as the notes say it.
function setupOrigin(f) {
  const older = f.older ? `, plus ${OLDER_ADDRESS}` : "";
  if (f.note === "own") return `, the address on this calendar${older},`;
  if (f.note === "copied") return `, copied from your other ${calendarsLent(f)}${older}, as this one doesn't name an address,`;
  if (f.note === "kept") return ", saved by an older version of this app, as this calendar doesn't name an address,";
  return "";
}

function foundStep() {
  const f = ui.found;
  const rows = f.meetings
    .map((m) => `<div role="listitem" class="row" style="gap:16px;padding:4px 0;flex-wrap:nowrap;${m.skipped ? "color:var(--muted)" : ""}">
        <span style="width:68px;flex-shrink:0;color:var(--muted)">${esc(m.time)}</span><span style="flex:1">${esc(m.title)}</span>
        ${m.skipped ? `<span class="small">${esc(m.skipped)}</span>` : ""}</div>`)
    .join("");
  const suggested = ui.foundSuggested;
  // The editor opens at once when there is nothing to start from, and on "Add another address" otherwise.
  const declined = ui.foundEditing
    ? addressEditor({
      key: "setup", chips: ui.foundList, removable: !(suggested.length === 1 && sameList(ui.foundList, suggested)),
      removeAction: "removeFoundAddress", addAction: "addFoundAddress", bind: "foundDraft", draft: ui.foundDraft, error: ui.foundError,
      label: "Add an address for this calendar", note: noteText(f),
    })
    : `<p class="row muted" style="flex-wrap:nowrap;align-items:flex-start"><span style="color:var(--accent-ink);display:flex;padding-top:2px">${icon("check", "sm")}</span>
        <span>Meetings declined by ${names(suggested)}${setupOrigin(f)} are skipped.
          <button id="edit-found" class="link" style="font:inherit" data-action="editFound">Add another address</button></span></p>`;
  return {
    body: `<div class="stack" style="gap:8px"><div class="eyebrow">Step 3 of 3</div><h1>Found your calendar</h1></div>
      <div class="list">
        <div class="item" style="align-items:center;gap:10px"><span class="lead">${icon("check")}</span>
          <span class="name">${esc(f.name)}</span><span class="muted" style="font-size:13px">${plural(f.count, "meeting")} in the next day</span></div>
        ${rows ? `<div role="list" aria-label="Next meetings" style="padding:8px 16px 12px 46px;border-top:1px solid var(--border);font-variant-numeric:tabular-nums">${rows}</div>` : ""}
      </div>
      ${declined}`,
    footer: `<span class="note">Add more calendars later in Settings.</span>
      <button class="btn large" data-action="goStep" data-step="calendar">Back</button>
      <button id="finish" class="btn large primary" data-action="finish" ${ui.setupBusy ? "disabled" : ""}>Finish</button>`,
  };
}

function doneStep() {
  const next = ui.found && ui.found.meetings.find((m) => !m.skipped);
  // Only what is true: it starts at sign-in when that is on, and the switch for it is only in the installed app.
  const signIn = data.start_at_sign_in
    ? " and starts when you sign in to Windows"
    : data.sign_in_available ? ", but won't start when you sign in to Windows until you switch that on in Settings" : "";
  const text = ui.callsOnly
    ? "Calls you join will start notes from now on. Add a calendar any time from Settings, so other meetings do too."
    : `Granola Autostart runs in the background${signIn}.${next ? ` Your next meeting, ${esc(next.title)} at ${esc(next.time)}, will show the countdown card.` : ""}`;
  return {
    body: `<span class="big-check">${icon("tick")}</span>
      <div class="stack" style="gap:8px"><h1>You're all set</h1><p class="muted">${text}</p></div>
      <div class="stack" style="gap:10px;margin-top:8px"><div class="label">Find it in the system tray</div>
        <div class="tray" aria-hidden="true"><span>${icon("up", "sm")}</span><span class="ours">${appMark(16)}</span><span>${icon("wifi")}</span><span>${icon("speaker")}</span>
          <span class="clock">${clock()}</span></div>
        <div class="row small muted" style="align-self:flex-end;margin-right:152px">${icon("arrowUp", "sm")}Right-click it to pause, turn off, or open Settings</div></div>`,
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
  startAdd: () => {
    ui.adding = true; ui.addResult = null; render();
    // The panel opens at the top of the list, so the list goes back to its top too.
    scrollers().forEach((element) => { if (element) element.scrollTop = 0; });
    focusSoon("add-url");
  },
  cancelAdd: () => { resetAdd(); render(); focusSoon("add-calendar"); },
  testNew: async () => {
    if (!ui.addUrl.trim() || ui.addBusy) return;
    ui.addBusy = true; render();
    const result = await api().test_address(ui.addUrl);
    ui.addBusy = false; ui.addResult = result;
    if (result.ok) {
      // The editor starts from the suggestion, and what is changed in it stays here until Add calendar.
      ui.addSuggested = [...(result.declined || [])]; ui.addList = [...ui.addSuggested]; ui.addDraft = ""; ui.addError = null;
    }
    render();
  },
  addNewAddress: () => addTyped("addList", "addDraft", "addError", "address-new"),
  removeNewAddress: (d) => removeTyped("addList", d.address, "chip-new-", "address-new"),
  confirmAdd: async () => {
    if (ui.saving || !ui.addResult || !ui.addResult.ok) return;
    const typed = withTyped(ui.addList, ui.addDraft);
    if (typed.error) { ui.addError = typed.error; render(); focusSoon("address-new"); return; }
    ui.saving = true;
    const result = await api().add_calendar(ui.addUrl, chosenList(typed.list, ui.addSuggested));
    ui.saving = false;
    if (result.error) { ui.addError = result.error; render(); return; }
    data = result; resetAdd(); render(); focusSoon("add-calendar");
  },
  testCalendar: async (d) => {
    if (ui.tests[d.id] && ui.tests[d.id].busy) return;
    ui.tests[d.id] = { busy: true }; render();
    ui.tests[d.id] = await api().test_calendar(d.id); render();
  },
  // One row is open at a time, and what was half typed in another is dropped with it.
  toggleRow: (d) => {
    ui.open = ui.open === d.id ? null : d.id;
    ui.newAddress = ""; ui.addressError = null; ui.confirmRemove = null;
    render();
    // An open row near the foot of a long list comes into view.
    document.getElementById(`cal-detail-${d.id}`)?.scrollIntoView({ block: "nearest" });
  },
  toggleCalendar: async (d) => {
    const calendar = data.settings && data.settings.calendars.find((c) => c.id === d.id);
    if (!calendar) return;
    data = await api().set_calendar_enabled(d.id, !calendar.enabled);
    render();
    // An off calendar sorts to the end (and an on one back up), so the page follows its switch, which keeps the focus.
    document.getElementById(`cal-switch-${d.id}`)?.scrollIntoView({ block: "nearest" });
  },
  askRemove: (d) => { ui.confirmRemove = d.id; render(); focusSoon(`keep-${d.id}`); },
  keepCalendar: (d) => { ui.confirmRemove = null; render(); focusSoon(`askremove-${d.id}`); },
  removeCalendar: async (d) => {
    data = await api().remove_calendar(d.id);
    ui.confirmRemove = null; delete ui.tests[d.id];
    if (ui.open === d.id) { ui.open = null; ui.newAddress = ""; ui.addressError = null; }
    render(); focusSoon("add-calendar");
  },
  addDeclined: async (d) => {
    const text = ui.newAddress.trim();
    if (!text) { focusSoon(`address-${d.id}`); return; }
    if (!EMAIL.test(text)) { ui.addressError = NOT_AN_ADDRESS; render(); return; }
    const result = await api().add_declined(d.id, text);
    if (result.error) { ui.addressError = result.error; render(); return; }
    // The Test result shown was for the old list, so it goes.
    data = result; delete ui.tests[d.id]; ui.newAddress = ""; ui.addressError = null; render(); focusSoon(`address-${d.id}`);
  },
  removeDeclined: async (d) => {
    const calendar = data.settings.calendars.find((c) => c.id === d.id);
    const at = calendar ? calendar.declined.indexOf(d.address) : -1;
    data = await api().remove_declined(d.id, d.address);
    delete ui.tests[d.id];
    render();
    // Focus moves to the chip that took its place, or else to the field.
    (document.getElementById(`chip-${d.id}-${at}`) || document.getElementById(`address-${d.id}`))?.focus();
  },
  setCountdown: (d) => choose("countdown", Number(d.value), () => data.settings.countdown_seconds, (value) => api().set_countdown(value)),
  setIdle: async (_d, element) => { data = await api().set_idle(Number(element.value)); render(); },
  addWord: async () => { if (!ui.newWord.trim()) return; data = await api().add_word(ui.newWord); ui.newWord = ""; render(); },
  removeWord: async (d) => { data = await api().remove_word(d.word); render(); },
  preview: () => api().preview(),
  openLogs: () => api().open_logs(),
  openConfig: () => api().open_config(),
  openLink: (d) => api().open_link(d.url),
  checkUpdates: async () => {
    if (ui.checking) return;
    ui.checking = true; render();
    try { data = await api().check_updates(); } finally { ui.checking = false; render(); }
  },
  installUpdate: async () => {
    if (ui.installing) return;
    ui.installing = true; render();
    data = await api().install_update();
    // Once the installer has started it stops this window in a moment, so the row keeps saying so, until it has
    // waited longer than that (the wait Python says) and offers the update again. When nothing started (a failure,
    // or a countdown card that came up) the row is just shown as it is.
    const u = data.update;
    ui.installing = u.installing;
    clearTimeout(ui.installTimer);
    if (ui.installing) ui.installTimer = setTimeout(() => { ui.installing = false; render(); }, u.install_wait_seconds * 1000);
    render();
  },
  setNotes: async () => { data = await api().set_autostart_notes(!data.status.autostart_notes); render(); },
  setSignIn: async () => { data = await api().set_start_at_sign_in(!data.start_at_sign_in); render(); },
  setAppearance: (d) => choose("appearance", d.value, () => data.appearance, (value) => api().set_appearance(value)),
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
  toggleCall: async (d) => {
    const call = data.settings && data.settings.calls.find((c) => c.provider === d.provider);
    if (!call) return;
    data = await api().set_call(d.provider, !call.on);
    render();
  },
  toggleSetupCall: (d) => { ui.calls = { ...ui.calls, [d.provider]: !(ui.calls && ui.calls[d.provider]) }; render(); },
  // Finish with no calendar. The button is only there while some call is on.
  callsOnly: async () => {
    if (ui.setupBusy || !ui.calls || !Object.values(ui.calls).some(Boolean)) return;
    ui.setupBusy = true; render();
    const result = await api().finish_setup(null, null, ui.calls);
    ui.setupBusy = false;
    data = result; ui.callsOnly = true; ui.step = "done"; render();
  },
  testSetup: async () => {
    if (!ui.setupUrl.trim() || ui.setupBusy) return;
    ui.setupBusy = true; ui.setupError = null; render();
    const result = await api().test_address(ui.setupUrl);
    ui.setupBusy = false;
    if (result.ok) {
      ui.found = result; ui.step = "found";
      ui.foundSuggested = [...(result.declined || [])]; ui.foundList = [...ui.foundSuggested];
      ui.foundEditing = !ui.foundSuggested.length; ui.foundDraft = ""; ui.foundError = null;
    } else {
      ui.setupError = result.error;
    }
    render();
    // The address field this was started from has gone, so focus goes on to the next thing to do.
    if (result.ok) focusSoon(ui.foundEditing ? "address-setup" : "finish");
  },
  editFound: () => { ui.foundEditing = true; render(); focusSoon("address-setup"); },
  addFoundAddress: () => addTyped("foundList", "foundDraft", "foundError", "address-setup"),
  removeFoundAddress: (d) => removeTyped("foundList", d.address, "chip-setup-", "address-setup"),
  finish: async () => {
    if (ui.setupBusy) return;
    const typed = withTyped(ui.foundList, ui.foundDraft);
    if (typed.error) { ui.foundEditing = true; ui.foundError = typed.error; render(); focusSoon("address-setup"); return; }
    ui.setupBusy = true; render();
    const result = await api().finish_setup(ui.setupUrl, chosenList(typed.list, ui.foundSuggested), ui.calls);
    ui.setupBusy = false;
    if (result.error) { ui.error = result.error; render(); return; }
    data = result; ui.callsOnly = false; ui.step = "done"; render();
  },
};

// A radio group saves what is chosen in it one choice at a time, in order. The arrow keys choose as they move, so several
// choices can come in quick succession, and each would otherwise be saved on its own and arrive out of order. ``ui.chosen``
// holds the latest one not yet saved, which the page shows meanwhile, and the running save goes on to it when it is done.
async function choose(setting, value, saved, save) {
  if (value === (setting in ui.chosen ? ui.chosen[setting] : saved())) return;
  ui.chosen[setting] = value;
  render();
  if (ui.choosing[setting]) return;
  ui.choosing[setting] = true;
  try {
    while (setting in ui.chosen) {
      const wanted = ui.chosen[setting];
      data = await save(wanted);
      if (ui.chosen[setting] === wanted) delete ui.chosen[setting];
    }
  } finally {
    // After a failure too, the page goes back to what is saved.
    delete ui.chosen[setting];
    ui.choosing[setting] = false;
    render();
  }
}

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
    ui.busy = ui.addBusy = ui.saving = ui.setupBusy = ui.checking = ui.installing = false;
    for (const id of Object.keys(ui.tests)) if (ui.tests[id].busy) delete ui.tests[id];
    try { data = await api().load(); } catch { /* keep the page as it was */ }
    ui.error = data.config_error ? `${data.config_error}.` : "That didn't work. Try again, or open the log folder from General.";
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
  // Typing after an address was turned back clears the message.
  const error = { newAddress: "addressError", addDraft: "addError", foundDraft: "foundError" }[bind];
  if (error && ui[error]) { ui[error] = null; render(); }
});
app.addEventListener("keydown", (event) => {
  const action = event.target.dataset.enter;
  if (event.key === "Enter" && action) { event.preventDefault(); perform(action, event.target.dataset, event.target); }
  else if (event.target.getAttribute("role") === "radio") moveRadio(event);
});

// A radio group works as radios do: the arrow keys go to the next or previous one, round the ends, and choose it.
const RADIO_STEPS = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 };
function moveRadio(event) {
  const step = RADIO_STEPS[event.key];
  const group = event.target.closest('[role="radiogroup"]');
  if (!step || !group) return;
  const radios = [...group.querySelectorAll('[role="radio"]:not(:disabled)')];
  const next = radios[(radios.indexOf(event.target) + step + radios.length) % radios.length];
  event.preventDefault();
  next.focus();
  next.click();
}
app.addEventListener("change", (event) => {
  const action = event.target.dataset.change;
  if (action) perform(action, event.target.dataset, event.target);
});
