"use strict";

const POLL_MS = 2000;

const VM_STATES = {
  running: "Läuft",
  shutoff: "Aus",
  paused: "Angehalten",
  shutting_down: "Fährt herunter",
  crashed: "Abgestürzt",
  suspended: "Ruhezustand",
  unknown: "Unbekannt",
};

const JOB_STATES = {
  queued: "Wartet",
  starting: "VM wird gestartet",
  waiting_prompt: "Warte auf LUKS-Prompt",
  unlocking: "Entsperre …",
  unlocked: "Entsperrt",
  wrong_key: "Passphrase falsch",
  running: "Läuft",
  done: "Fertig",
  failed: "Fehlgeschlagen",
};

const STEP_ICONS = { pending: "○", running: "↻", done: "✓", failed: "✗", skipped: "–" };

const state = {
  info: null,
  vms: [],
  lastJson: "",
  retryShownFor: new Set(), // "<job-id>:<anfrage>", für die der Dialog schon aufging
};

// ------------------------------------------------------------------ Helfer

const $ = (sel, root = document) => root.querySelector(sel);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function store(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : JSON.parse(v);
  } catch {
    return fallback;
  }
}

function save(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* ohne Speicher geht es auch */
  }
}

const dismissed = new Set(store("vmdash.dismissed", []));
const checked = store("vmdash.checklist", {});

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body ?? {});
  }
  const res = await fetch(path, opts);
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* keine JSON-Antwort */
  }
  if (!res.ok) {
    throw new Error((data && data.error) || `HTTP ${res.status}`);
  }
  return data;
}

function showError(msg) {
  const el = $("#error");
  el.textContent = msg;
  el.hidden = !msg;
}

// ------------------------------------------------------------------ Rendern

function render() {
  const grid = $("#vms");
  if (!state.vms.length) {
    grid.innerHTML = '<p class="muted">Keine VMs gefunden.</p>';
    return;
  }
  const names = new Set(state.vms.map((v) => v.name));
  grid.innerHTML = state.vms.map((vm) => renderVm(vm, names)).join("");
}

function renderVm(vm, names) {
  const off = vm.state === "shutoff";
  const running = vm.state === "running";
  const busy = vm.busy;
  const btns = [];
  if (off) {
    btns.push(`<button class="btn primary" data-act="start" ${busy ? "disabled" : ""}>Starten</button>`);
    btns.push(`<button class="btn" data-act="clone" ${busy ? "disabled" : ""}>Klonen</button>`);
  }
  if (running && vm.rdp_url) {
    // Normaler Link: rdpgw liefert die .rdp-Datei direkt an den Browser
    btns.push(`<a class="btn primary" href="${esc(vm.rdp_url)}" target="_blank" rel="noopener noreferrer">Verbinden</a>`);
  }
  if (running) {
    btns.push(`<button class="btn" data-act="shutdown">Herunterfahren</button>`);
  }
  if (!off) {
    btns.push(`<button class="btn danger" data-act="destroy">Hart ausschalten</button>`);
  }

  let net = "";
  if (running && vm.ip) {
    net = `<div class="net"><span class="label">IP:</span> ${esc(vm.ip)}</div>`;
  } else if (running && state.info && state.info.rdpgw) {
    net = '<div class="net muted">Keine IP bekannt (keine DHCP-Reservierung oder Lease) – RDP nicht verfügbar.</div>';
  }

  const panels = [];
  if (vm.job && !dismissed.has(vm.job.id)) panels.push(renderJob(vm.job, vm.name));
  if (vm.clone_job && !dismissed.has(vm.clone_job.id) && !names.has(vm.clone_job.vm)) {
    panels.push(renderJob(vm.clone_job, vm.name));
  }

  return `<article class="vm" data-vm="${esc(vm.name)}">
    <div class="vm-head">
      <h2>${esc(vm.name)}</h2>
      ${vm.is_template ? '<span class="tag">Template</span>' : ""}
      <span class="badge ${esc(vm.state)}">${esc(VM_STATES[vm.state] || vm.state)}</span>
    </div>
    ${btns.length ? `<div class="actions">${btns.join("")}</div>` : ""}
    ${net}
    ${panels.join("")}
  </article>`;
}

function renderJob(job, cardName) {
  const title = job.kind === "clone"
    ? `Klon ${esc(job.source)} → ${esc(job.vm)}`
    : "Start und Entsperren";
  const steps = job.steps.map((s) => `
    <li class="${esc(s.status)}">
      <span class="icon ${s.status === "running" ? "spin" : ""}">${STEP_ICONS[s.status] || "?"}</span>
      <span><span class="label">${esc(s.label)}</span>
      ${s.detail ? `<span class="detail">${esc(s.detail)}</span>` : ""}</span>
    </li>`).join("");

  const extra = [];
  if (job.awaiting_passphrase) {
    extra.push(`<div class="msg warn">Passphrase falsch – die VM wartet am LUKS-Prompt.
      <button class="btn small" data-act="retry" data-target="${esc(job.vm)}">Erneut eingeben</button></div>`);
  }
  if (job.state === "failed") {
    extra.push(`<div class="msg bad">${esc(job.error || "Fehlgeschlagen")}${job.hint ? `<br>${esc(job.hint)}` : ""}</div>`);
    if (job.kind === "clone" && job.result.deletable) {
      extra.push(`<div class="actions"><button class="btn danger" data-act="delete" data-target="${esc(job.vm)}">Klon löschen</button></div>`);
    }
  }
  if (job.kind === "clone" && job.result.deleted) {
    extra.push('<div class="msg ok">Fehlgeschlagener Klon wurde gelöscht.</div>');
  }
  if (job.kind === "clone" && job.state === "done") {
    extra.push('<div class="msg ok">Klon fertig und mit der neuen Passphrase entsperrt.</div>');
    extra.push(renderChecklist(job));
  }
  if (job.kind === "start" && job.state === "unlocked") {
    extra.push('<div class="msg ok">Entsperrt, die VM bootet weiter.</div>');
  }

  return `<section class="job" data-job="${esc(job.id)}">
    <div class="job-head">
      <h3>${title}</h3>
      <span class="badge ${job.state === "failed" ? "crashed" : job.finished ? "running" : "paused"}">${esc(JOB_STATES[job.state] || job.state)}</span>
      ${job.finished ? `<button class="btn small" data-act="dismiss" data-job="${esc(job.id)}" title="Ausblenden">×</button>` : ""}
    </div>
    <ol class="steps">${steps}</ol>
    ${extra.join("")}
  </section>`;
}

function renderChecklist(job) {
  const port = job.result.vnc_port;
  const items = [
    "Neue Passphrase im Passwortmanager ablegen.",
    `RAC-Endpunkt in Authentik für den neuen VNC-Port anlegen: <code>127.0.0.1:${esc(port)}</code>`,
    "Kunden-VPN im Gast installieren.",
    "SAP-GUI-Verbindung und ABAP-Projekt in Eclipse einrichten.",
  ];
  const done = checked[job.id] || [];
  return `<ul class="checklist">${items.map((t, i) => `
    <li><label><input type="checkbox" data-act="check" data-job="${esc(job.id)}" data-idx="${i}" ${done.includes(i) ? "checked" : ""}>
    <span>${t}</span></label></li>`).join("")}</ul>
    ${job.result.ip ? `<p class="hint">RDP: feste IP ${esc(job.result.ip)}, „Verbinden“ funktioniert ohne weitere Einrichtung.</p>` : ""}`;
}

// ------------------------------------------------------------------ Laden

async function refresh() {
  try {
    const vms = await api("GET", "/api/vms");
    $("#conn").classList.remove("bad");
    showError("");
    state.vms = vms;
    const json = JSON.stringify(vms) + JSON.stringify([...dismissed]);
    if (json !== state.lastJson) {
      state.lastJson = json;
      render();
    }
    maybeAskRetry();
  } catch (e) {
    $("#conn").classList.add("bad");
    showError(`Dashboard nicht erreichbar: ${e.message}`);
  }
}

function maybeAskRetry() {
  for (const vm of state.vms) {
    const job = vm.job;
    if (!job || !job.awaiting_passphrase || job.vm !== vm.name) continue;
    // Schlüssel mit Zähler: auch nach erneuter Fehleingabe wieder fragen
    const key = `${job.id}:${job.passphrase_requests}`;
    if (!state.retryShownFor.has(key) && !anyDialogOpen()) {
      state.retryShownFor.add(key);
      openRetry(vm.name);
    }
  }
}

function anyDialogOpen() {
  return [...document.querySelectorAll("dialog")].some((d) => d.open);
}

// ------------------------------------------------------------------ Dialoge

function bind(dlg, key, text) {
  for (const el of dlg.querySelectorAll(`[data-bind="${key}"]`)) el.textContent = text;
}

function dialogError(dlg, msg) {
  const el = $(".dlg-error", dlg);
  el.textContent = msg || "";
  el.hidden = !msg;
}

function clearPasswords(dlg) {
  for (const el of dlg.querySelectorAll('input[type="password"]')) el.value = "";
}

function openStart(name) {
  const dlg = $("#dlg-start");
  dlg.dataset.vm = name;
  bind(dlg, "name", name);
  dialogError(dlg, "");
  clearPasswords(dlg);
  dlg.showModal();
}

function openRetry(name) {
  const dlg = $("#dlg-retry");
  dlg.dataset.vm = name;
  bind(dlg, "name", name);
  dialogError(dlg, "");
  clearPasswords(dlg);
  dlg.showModal();
}

function confirmDialog(title, text, okLabel) {
  const dlg = $("#dlg-confirm");
  bind(dlg, "title", title);
  bind(dlg, "text", text);
  bind(dlg, "ok", okLabel);
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
  });
}

function openClone(source) {
  const dlg = $("#dlg-clone");
  const form = $("form", dlg);
  const sel = form.elements.source;
  const candidates = state.vms.filter((v) => v.state === "shutoff" && !v.busy);
  const preferred = source || (state.info && state.info.template);
  sel.innerHTML = candidates
    .map((v) => `<option value="${esc(v.name)}" ${v.name === preferred ? "selected" : ""}>${esc(v.name)}${v.is_template ? " (Template)" : ""}</option>`)
    .join("");
  if (!candidates.length) {
    sel.innerHTML = '<option value="">Keine ausgeschaltete VM verfügbar</option>';
  }
  form.elements.suffix.value = "";
  bind(dlg, "prefix", state.info ? state.info.clone_prefix : "");
  updateNewName();
  clearPasswords(dlg);
  dialogError(dlg, "");
  dlg.showModal();
}

function updateNewName() {
  const dlg = $("#dlg-clone");
  const suffix = $("form", dlg).elements.suffix.value.trim();
  const prefix = state.info ? state.info.clone_prefix : "";
  bind(dlg, "newname", suffix ? prefix + suffix : "–");
}

// ------------------------------------------------------------------ Aktionen

async function action(name, act) {
  try {
    if (act === "start") return openStart(name);
    if (act === "clone") return openClone(name);
    if (act === "retry") return openRetry(name);
    if (act === "shutdown") {
      await api("POST", `/api/vms/${encodeURIComponent(name)}/shutdown`);
    } else if (act === "destroy") {
      const ok = await confirmDialog(
        `„${name}“ hart ausschalten?`,
        "Das entspricht dem Ziehen des Netzsteckers. Nicht gespeicherte Daten im Gast gehen verloren.",
        "Hart ausschalten",
      );
      if (!ok) return;
      await api("POST", `/api/vms/${encodeURIComponent(name)}/destroy`);
    } else if (act === "delete") {
      const ok = await confirmDialog(
        `Klon „${name}“ löschen?`,
        "Die Domain wird entfernt, inklusive Disk-Volume und NVRAM-Datei. Das lässt sich nicht rückgängig machen.",
        "Klon löschen",
      );
      if (!ok) return;
      await api("POST", `/api/vms/${encodeURIComponent(name)}/delete`);
    }
    await refresh();
  } catch (e) {
    showError(e.message);
  }
}

document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-act]");
  if (!btn || btn.disabled) return;
  const act = btn.dataset.act;
  if (act === "check") return; // Checkbox, siehe change-Handler
  if (act === "dismiss") {
    dismissed.add(btn.dataset.job);
    save("vmdash.dismissed", [...dismissed].slice(-200));
    render();
    return;
  }
  const card = btn.closest("[data-vm]");
  const name = btn.dataset.target || (card && card.dataset.vm);
  if (name) action(name, act);
});

document.addEventListener("change", (ev) => {
  const el = ev.target;
  if (el.dataset && el.dataset.act === "check") {
    const id = el.dataset.job;
    const idx = Number(el.dataset.idx);
    const list = new Set(checked[id] || []);
    if (el.checked) list.add(idx); else list.delete(idx);
    checked[id] = [...list];
    save("vmdash.checklist", checked);
  }
});

for (const btn of document.querySelectorAll("[data-close]")) {
  btn.addEventListener("click", () => {
    const dlg = btn.closest("dialog");
    clearPasswords(dlg);
    dlg.close("cancel");
  });
}

// Passwortfelder beim Schließen (auch per Esc) immer leeren
for (const dlg of document.querySelectorAll("dialog")) {
  dlg.addEventListener("close", () => clearPasswords(dlg));
}

$("#dlg-confirm form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  $("#dlg-confirm").close("ok");
});

async function submitPassphrase(dlg, path) {
  const form = $("form", dlg);
  const passphrase = form.elements.passphrase.value;
  clearPasswords(dlg);
  if (!passphrase) return;
  const submit = $('button[type="submit"]', dlg);
  submit.disabled = true;
  try {
    await api("POST", path, { passphrase });
    dlg.close("ok");
    await refresh();
  } catch (e) {
    dialogError(dlg, e.message);
  } finally {
    submit.disabled = false;
  }
}

$("#dlg-start form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const dlg = $("#dlg-start");
  submitPassphrase(dlg, `/api/vms/${encodeURIComponent(dlg.dataset.vm)}/start`);
});

$("#dlg-retry form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const dlg = $("#dlg-retry");
  submitPassphrase(dlg, `/api/vms/${encodeURIComponent(dlg.dataset.vm)}/unlock`);
});

$("#dlg-clone form").addEventListener("input", (ev) => {
  if (ev.target.name === "suffix") updateNewName();
});

$("#dlg-clone form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const dlg = $("#dlg-clone");
  const f = ev.target.elements;
  const source = f.source.value;
  const suffix = f.suffix.value.trim();
  const oldP = f.old.value;
  const n1 = f.new1.value;
  const n2 = f.new2.value;
  clearPasswords(dlg);
  let err = "";
  if (!source) err = "Bitte eine ausgeschaltete Quell-VM wählen.";
  else if (!/^[a-z0-9-]+$/.test(suffix)) err = "Kundenkürzel: nur a–z, 0–9 und Bindestrich.";
  else if (!oldP || !n1) err = "Bitte alle Passphrasen eingeben.";
  else if (n1 !== n2) err = "Die neuen Passphrasen stimmen nicht überein.";
  else if (n1 === oldP) err = "Die neue Passphrase muss sich von der alten unterscheiden.";
  if (err) {
    dialogError(dlg, err + " Passphrasen bitte erneut eingeben.");
    return;
  }
  const submit = $('button[type="submit"]', dlg);
  submit.disabled = true;
  try {
    await api("POST", `/api/vms/${encodeURIComponent(source)}/clone`, {
      suffix, old_passphrase: oldP, new_passphrase: n1,
    });
    dlg.close("ok");
    await refresh();
  } catch (e) {
    dialogError(dlg, e.message + " Passphrasen bitte erneut eingeben.");
  } finally {
    submit.disabled = false;
  }
});

$("#btn-clone").addEventListener("click", () => openClone(null));

// ------------------------------------------------------------------ Start

(async function init() {
  try {
    state.info = await api("GET", "/api/info");
    $("#mock-banner").hidden = state.info.mode !== "mock";
    $("#rdp-client-link").hidden = !state.info.rdpgw;
    $("#version").textContent = `vmdash ${state.info.version} · Backend: ${state.info.mode}`;
  } catch (e) {
    showError(`Dashboard nicht erreichbar: ${e.message}`);
  }
  await refresh();
  setInterval(refresh, POLL_MS);
})();
