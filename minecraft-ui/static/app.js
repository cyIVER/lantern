"use strict";

/**
 * @typedef {{revision: string|number, session: {enabled: boolean, authenticated: boolean, actor: string|null, role: "guest"|"admin"}, minecraft: {state: string, available: boolean, players: number|null, allowed: string[]}, schematics: {pendingCount: number, catalogCount: number}, admin: null|{attention?: Array<{id?: string,title?: string,state?: string,revision?: number,eligible?: boolean,sha256?: string,byteSize?: number,metadata?: object,failedRequirements?: string[],requirements?: Array<object>,recommendations?: Array<object>,downloadUrl?: string}>, files?: {entries?: Array<object>}, mods?: {entries?: Array<object>}, restores?: Array<object>, jobs?: unknown[], audit?: Array<object>}} Workspace
 * @typedef {{type: "session.login", username: string, password: string}|{type: "session.logout"}|{type: "minecraft.power", action: "start"|"stop"|"restart", confirmation?: string, idempotencyKey: string}|{type: "schematic.review", submissionId: string, decision: "publish"|"reject", expectedRevision: number, reasonCode?: string, idempotencyKey: string}|{type: "file.list", directory: string, idempotencyKey: string}|{type: "file.read", path: string, idempotencyKey: string}|{type: "file.save", path: string, content: string, expectedRevision: string, idempotencyKey: string}|{type: "mods.enable"|"mods.disable"|"mods.delete", filename: string, expectedRevision: string, idempotencyKey: string, confirmation?: string}|{type: "backup.create", name: string, idempotencyKey: string}|{type: "restore.prepare"|"restore.execute", backupId: string, idempotencyKey: string, confirmation?: string}} PortalIntent
 * @typedef {{outcome: "done", workspace?: Workspace, notice?: string, files?: {directory: string,entries: Array<object>}, document?: {path: string,content: string,revision: string}, mod?: object, backup?: object, restore?: object, submission?: object}|{outcome: "accepted", job: unknown}|{outcome: "confirmation_required", challenge: {token: string, message: string, effects?: string[]}}|{outcome: "invalid", issues: unknown[], recommendations: unknown[]}} IntentResponse
 */

const elements = {
  tabs: [...document.querySelectorAll('[role="tab"]')],
  panels: [...document.querySelectorAll('[role="tabpanel"]')],
  lanternHome: document.getElementById("lantern-home"),
  manageServer: document.getElementById("manage-server"),
  pelicanLink: document.getElementById("pelican-link"),
  connectAddress: document.getElementById("connect-address"),
  copyAddress: document.getElementById("copy-address"),
  workspaceState: document.getElementById("workspace-state"),
  workspacePill: document.getElementById("workspace-pill"),
  openSchematics: document.getElementById("open-schematics"),
  schematicFrame: document.getElementById("schematic-frame"),
  schematicUploadForm: document.getElementById("schematic-upload-form"),
  schematicFile: document.getElementById("schematic-file"),
  schematicTitle: document.getElementById("schematic-title"),
  schematicPromote: document.getElementById("schematic-promote"),
  schematicSubmit: document.getElementById("schematic-submit"),
  schematicUploadStatus: document.getElementById("schematic-upload-status"),
  sessionState: document.getElementById("session-state"),
  loginOpen: document.getElementById("login-open"),
  logoutButton: document.getElementById("logout-button"),
  accessDescription: document.getElementById("access-description"),
  accessAction: document.getElementById("access-action"),
  minecraftState: document.getElementById("minecraft-state"),
  minecraftStatusCopy: document.getElementById("minecraft-status-copy"),
  serverControls: document.getElementById("server-controls"),
  powerButtons: [...document.querySelectorAll("[data-power-action]")],
  adminTab: document.getElementById("tab-admin"),
  adminPendingBadge: document.getElementById("admin-pending-badge"),
  adminWelcome: document.getElementById("admin-welcome"),
  reviewCount: document.getElementById("review-count"),
  reviewQueue: document.getElementById("review-queue"),
  adminFiles: document.getElementById("admin-files"),
  fileEditor: document.getElementById("file-editor"),
  fileEditorName: document.getElementById("file-editor-name"),
  fileEditorRevision: document.getElementById("file-editor-revision"),
  fileEditorContent: document.getElementById("file-editor-content"),
  fileSave: document.getElementById("file-save"),
  fileStatus: document.getElementById("file-status"),
  adminMods: document.getElementById("admin-mods"),
  modUploadForm: document.getElementById("mod-upload-form"),
  modFile: document.getElementById("mod-file"),
  modUpload: document.getElementById("mod-upload"),
  modStatus: document.getElementById("mod-status"),
  adminRestores: document.getElementById("admin-restores"),
  backupForm: document.getElementById("backup-form"),
  backupName: document.getElementById("backup-name"),
  backupCreate: document.getElementById("backup-create"),
  backupStatus: document.getElementById("backup-status"),
  adminJobs: document.getElementById("admin-jobs"),
  adminAudit: document.getElementById("admin-audit"),
  loginDialog: document.getElementById("login-dialog"),
  loginForm: document.getElementById("login-form"),
  loginClose: document.getElementById("login-close"),
  loginCancel: document.getElementById("login-cancel"),
  loginSubmit: document.getElementById("login-submit"),
  adminUsername: document.getElementById("admin-username"),
  adminPassword: document.getElementById("admin-password"),
  loginError: document.getElementById("login-error"),
  confirmDialog: document.getElementById("confirm-dialog"),
  confirmForm: document.getElementById("confirm-form"),
  confirmClose: document.getElementById("confirm-close"),
  confirmCancel: document.getElementById("confirm-cancel"),
  confirmSubmit: document.getElementById("confirm-submit"),
  confirmMessage: document.getElementById("confirm-message"),
  confirmEffects: document.getElementById("confirm-effects"),
  confirmError: document.getElementById("confirm-error"),
  toast: document.getElementById("toast")
};

let toastTimer;
let previousDialogFocus = null;
let currentWorkspace = null;
let pendingConfirmation = null;
let pendingConfirmationStatus = null;
let activeDocument = null;
let currentFileDirectory = "";

function sameOriginOptions(options = {}) {
  return {credentials: "same-origin", ...options, headers: {accept: "application/json", ...(options.headers || {})}};
}

async function responseDetail(response, fallback) {
  try {
    const body = await response.json();
    return bodyDetail(body, fallback);
  } catch {
    // Use a safe fallback rather than transport details.
  }
  return fallback;
}

function bodyDetail(body, fallback) {
  if (body && typeof body.detail === "string") return body.detail;
  if (body && body.detail && typeof body.detail.message === "string") return body.detail.message;
  if (body && typeof body.notice === "string") return body.notice;
  return fallback;
}

function showToast(message, tone = "info") {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.dataset.tone = tone;
  elements.toast.classList.add("is-visible");
  toastTimer = window.setTimeout(() => elements.toast.classList.remove("is-visible"), 3600);
}

function configureLocalLinks() {
  const lanternUrl = new URL(window.location.href);
  lanternUrl.port = "8090";
  lanternUrl.pathname = "/";
  lanternUrl.search = "";
  lanternUrl.hash = "";
  const pelicanUrl = new URL(window.location.href);
  pelicanUrl.port = "";
  pelicanUrl.pathname = "/";
  pelicanUrl.search = "";
  pelicanUrl.hash = "";
  elements.lanternHome.href = lanternUrl.href;
  elements.manageServer.href = lanternUrl.href;
  elements.pelicanLink.href = pelicanUrl.href;
  elements.connectAddress.textContent = `${window.location.hostname}:25565`;
}

function selectedTabFromLocation() {
  const requested = window.location.hash.slice(1);
  if (requested === "schematics") return requested;
  if (requested === "admin" && !elements.adminTab.hidden) return requested;
  return "overview";
}

function selectTab(name, {focus = false, updateHistory = true} = {}) {
  const selectableTabs = elements.tabs.filter((tab) => !tab.hidden);
  const selectedTab = selectableTabs.find((tab) => tab.dataset.tab === name) || selectableTabs[0];
  const selectedName = selectedTab.dataset.tab;
  for (const tab of elements.tabs) {
    const selected = tab === selectedTab;
    tab.classList.toggle("is-active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected && focus) tab.focus();
  }
  for (const panel of elements.panels) {
    const selected = panel.id === `panel-${selectedName}`;
    panel.hidden = !selected;
    panel.toggleAttribute("inert", !selected);
  }
  document.body.dataset.view = selectedName;
  if (updateHistory) {
    const url = new URL(window.location.href);
    url.hash = selectedName === "overview" ? "" : selectedName;
    window.history.pushState({tab: selectedName}, "", url);
  }
}

function handleTabKeydown(event) {
  const selectableTabs = elements.tabs.filter((tab) => !tab.hidden);
  const currentIndex = selectableTabs.indexOf(event.currentTarget);
  let nextIndex = currentIndex;
  if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % selectableTabs.length;
  else if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + selectableTabs.length) % selectableTabs.length;
  else if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = selectableTabs.length - 1;
  else return;
  event.preventDefault();
  selectTab(selectableTabs[nextIndex].dataset.tab, {focus: true});
}

async function copyConnectAddress() {
  const address = elements.connectAddress.textContent.trim();
  try {
    await navigator.clipboard.writeText(address);
  } catch {
    const temporary = document.createElement("textarea");
    temporary.value = address;
    temporary.setAttribute("readonly", "");
    temporary.style.position = "fixed";
    temporary.style.opacity = "0";
    document.body.append(temporary);
    temporary.select();
    const copied = document.execCommand("copy");
    temporary.remove();
    if (!copied) throw new Error("copy failed");
  }
  showToast(`Copied ${address}`);
}

function setWorkspaceStatus(state, message, pillLabel) {
  elements.workspaceState.dataset.state = state;
  elements.workspaceState.textContent = message;
  elements.workspacePill.dataset.state = state;
  elements.workspacePill.textContent = pillLabel;
}

function normaliseState(value) {
  return typeof value === "string" && value.trim() ? value.trim().toLowerCase() : "unknown";
}

function titleCase(value) {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function isPowerAllowed(allowed, action) {
  return allowed.includes(action) || allowed.includes(`minecraft.${action}`) || allowed.includes(`minecraft.power.${action}`);
}

function safeSummary(value, fallback) {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (value && typeof value === "object") {
    for (const key of ["message", "title", "name", "action", "status", "path"]) {
      if (typeof value[key] === "string" && value[key].trim()) return value[key].trim();
    }
  }
  return fallback;
}

function setTextList(container, values, emptyMessage, formatter) {
  container.replaceChildren();
  if (!Array.isArray(values) || values.length === 0) {
    container.textContent = emptyMessage;
    container.classList.add("empty-state");
    return;
  }
  container.classList.remove("empty-state");
  const list = document.createElement("ul");
  list.className = "surface-list";
  for (const value of values.slice(0, 20)) {
    const item = document.createElement("li");
    item.textContent = formatter(value);
    list.append(item);
  }
  container.append(list);
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "size unknown";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function operationRow(title, detail) {
  const row = document.createElement("div");
  row.className = "operation-row";
  const copy = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = title;
  const small = document.createElement("small");
  small.textContent = detail;
  copy.append(strong, small);
  const actions = document.createElement("div");
  actions.className = "row-actions";
  row.append(copy, actions);
  return {row, actions};
}

function actionButton(label, tone, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${tone}`;
  button.textContent = label;
  button.addEventListener("click", () => handler(button));
  return button;
}

function setSurfaceEmpty(container, message) {
  container.replaceChildren();
  container.textContent = message;
  container.classList.add("empty-state");
}

function renderReviewQueue(entries) {
  elements.reviewQueue.replaceChildren();
  if (!Array.isArray(entries) || entries.length === 0) {
    setSurfaceEmpty(elements.reviewQueue, "No schematics are waiting for review.");
    return;
  }
  elements.reviewQueue.classList.remove("empty-state");
  for (const item of entries) {
    if (!item || typeof item.id !== "string" || !Number.isInteger(item.revision)) continue;
    const failed = Array.isArray(item.failedRequirements) ? item.failedRequirements : [];
    const filename = item.metadata && typeof item.metadata.filename === "string" ? item.metadata.filename : "uploaded schematic";
    const size = Number.isInteger(item.byteSize) ? `${Math.max(1, Math.ceil(item.byteSize / 1024))} KiB` : "unknown size";
    const detail = `${filename} · ${size} · ${failed.length ? `Needs review: ${failed.join(", ")}` : "All automated requirements passed"}`;
    const {row, actions} = operationRow(item.title || item.id, detail);
    const evidence = document.createElement("details");
    evidence.className = "review-evidence";
    const summary = document.createElement("summary");
    summary.textContent = "Requirements and recommendations";
    const facts = document.createElement("p");
    const requirements = Array.isArray(item.requirements) ? item.requirements : [];
    const recommendations = Array.isArray(item.recommendations) ? item.recommendations : [];
    const requirementText = requirements.map((entry) => `${entry && entry.passed ? "Pass" : "Check"}: ${entry && entry.code ? entry.code : "requirement"}`).join(" · ");
    const recommendationText = recommendations.map((entry) => `${entry && entry.field ? entry.field : "field"}: ${Array.isArray(entry && entry.value) ? entry.value.join(", ") : entry && entry.value ? entry.value : "review"}`).join(" · ");
    facts.textContent = `${requirementText || "No requirement results."}${recommendationText ? ` Recommendations: ${recommendationText}` : ""}`;
    evidence.append(summary, facts);
    row.querySelector(".operation-copy")?.append(evidence);
    if (typeof item.downloadUrl === "string" && item.downloadUrl.startsWith("/api/admin/submissions/")) {
      const download = document.createElement("a");
      download.className = "button button-secondary";
      download.href = item.downloadUrl;
      download.textContent = "Download";
      download.setAttribute("download", "");
      actions.append(download);
    }
    const publish = actionButton("Publish", "button-primary", (button) => reviewSubmission(item, "publish", button));
    publish.disabled = item.eligible !== true;
    if (publish.disabled) publish.title = "Resolve all hard requirements before publishing.";
    actions.append(
      publish,
      actionButton("Reject", "button-danger", (button) => reviewSubmission(item, "reject", button))
    );
    elements.reviewQueue.append(row);
  }
  if (!elements.reviewQueue.children.length) setSurfaceEmpty(elements.reviewQueue, "No actionable reviews are available.");
}

function parentDirectory(directory) {
  const segments = directory.split("/").filter(Boolean);
  segments.pop();
  return segments.join("/");
}

function renderFiles(files, directory = "") {
  const entries = files && Array.isArray(files.entries) ? files.entries : null;
  if (!entries) {
    setSurfaceEmpty(elements.adminFiles, "File operations are not available yet.");
    elements.fileEditor.hidden = true;
    activeDocument = null;
    currentFileDirectory = "";
    return false;
  }
  currentFileDirectory = typeof files.directory === "string" ? files.directory : directory;
  elements.adminFiles.replaceChildren();
  const navigation = document.createElement("div");
  navigation.className = "file-navigation";
  const location = document.createElement("strong");
  location.textContent = currentFileDirectory ? `/${currentFileDirectory}` : "/ configuration root";
  const controls = document.createElement("div");
  controls.className = "row-actions";
  const back = actionButton("Back", "button-secondary", (button) => listDirectory(parentDirectory(currentFileDirectory), button));
  const root = actionButton("Root", "button-secondary", (button) => listDirectory("", button));
  back.disabled = !currentFileDirectory;
  root.disabled = !currentFileDirectory;
  controls.append(back, root);
  navigation.append(location, controls);
  elements.adminFiles.append(navigation);
  if (!entries.length) {
    const empty = document.createElement("p");
    empty.className = "surface-message";
    empty.textContent = "This directory contains no editable files or visible directories.";
    elements.adminFiles.append(empty);
    elements.adminFiles.classList.remove("empty-state");
    return true;
  }
  elements.adminFiles.classList.remove("empty-state");
  for (const entry of entries) {
    if (!entry || typeof entry.path !== "string") continue;
    const {row, actions} = operationRow(entry.name || entry.path, `${formatBytes(entry.byte_size)} · ${entry.kind || "file"}`);
    const directoryEntry = entry.kind === "directory";
    actions.append(actionButton(directoryEntry ? "Open" : "Edit", "button-secondary", (button) => {
      if (directoryEntry) listDirectory(entry.path, button);
      else readFile(entry.path, button);
    }));
    elements.adminFiles.append(row);
  }
  return true;
}

function renderMods(mods) {
  const entries = mods && Array.isArray(mods.entries) ? mods.entries : null;
  elements.modUploadForm.hidden = !entries;
  if (!entries) {
    setSurfaceEmpty(elements.adminMods, "Mod operations are not available yet.");
    return false;
  }
  elements.adminMods.replaceChildren();
  if (!entries.length) {
    setSurfaceEmpty(elements.adminMods, "No mod files were returned by the server.");
    return true;
  }
  elements.adminMods.classList.remove("empty-state");
  for (const mod of entries) {
    if (!mod || typeof mod.name !== "string" || typeof mod.revision !== "string") continue;
    const enabled = Boolean(mod.enabled);
    const {row, actions} = operationRow(mod.name, `${enabled ? "Enabled" : "Disabled"} · ${formatBytes(mod.byte_size)}`);
    actions.append(
      actionButton(enabled ? "Disable" : "Enable", "button-secondary", (button) => changeMod(mod, enabled ? "disable" : "enable", button)),
      actionButton("Delete", "button-danger", (button) => changeMod(mod, "delete", button))
    );
    elements.adminMods.append(row);
  }
  return true;
}

function renderRestores(restores, available) {
  elements.backupForm.hidden = !available;
  elements.adminRestores.replaceChildren();
  if (!available) {
    setSurfaceEmpty(elements.adminRestores, "Backup operations are not available yet.");
    return;
  }
  if (!Array.isArray(restores) || !restores.length) {
    setSurfaceEmpty(elements.adminRestores, "No verified restore points are available.");
    return;
  }
  elements.adminRestores.classList.remove("empty-state");
  for (const backup of restores) {
    if (!backup || typeof backup.backup_id !== "string") continue;
    const verified = backup.state === "ready" && backup.consistency_proven === true && typeof backup.checksum_sha256 === "string" && /^[a-fA-F0-9]{64}$/.test(backup.checksum_sha256);
    const detail = `${backup.state || "unknown"} · ${formatBytes(backup.byte_size)}${verified ? " · offline + SHA-256 verified" : " · not restore-eligible"}`;
    const {row, actions} = operationRow(backup.name || backup.backup_id, detail);
    if (verified) actions.append(actionButton("Restore", "button-danger", (button) => prepareRestore(backup.backup_id, button)));
    elements.adminRestores.append(row);
  }
}

function renderAudit(entries) {
  setTextList(elements.adminAudit, entries, "No administrative activity yet.", (item) => {
    if (!item || typeof item !== "object") return "Administrative action";
    const actor = typeof item.actor === "string" ? item.actor : "unknown actor";
    const action = typeof item.action === "string" ? item.action : "action";
    const target = typeof item.target === "string" ? ` on ${item.target}` : "";
    const outcome = typeof item.outcome === "string" ? ` · ${item.outcome}` : "";
    return `${actor}: ${action}${target}${outcome}`;
  });
}

/** @param {Workspace} workspace */
function renderWorkspace(workspace) {
  currentWorkspace = workspace;
  const session = workspace.session || {};
  const minecraft = workspace.minecraft || {};
  const schematics = workspace.schematics || {};
  const authenticated = Boolean(session.enabled && session.authenticated && session.role === "admin");
  const actor = typeof session.actor === "string" ? session.actor : "Administrator";
  const pendingCount = Math.max(0, Number(schematics.pendingCount) || 0);
  const catalogCount = Math.max(0, Number(schematics.catalogCount) || 0);
  elements.sessionState.dataset.state = authenticated ? "admin" : "guest";
  elements.loginOpen.hidden = !session.enabled || authenticated;
  elements.logoutButton.hidden = !authenticated;
  elements.accessAction.hidden = !session.enabled || authenticated;
  elements.adminTab.hidden = !authenticated;
  if (!session.enabled) {
    elements.sessionState.textContent = "Read-only library";
    elements.accessDescription.textContent = "Shared schematics are readable. Administrator sign-in is unavailable.";
  } else if (authenticated) {
    elements.sessionState.textContent = actor;
    elements.accessDescription.textContent = `${actor} is signed in. Administrator library tools are enabled.`;
  } else {
    elements.sessionState.textContent = "Guest";
    elements.accessDescription.textContent = "Shared schematics and ordinary server controls are available without signing in.";
  }
  if (!authenticated) {
    currentFileDirectory = "";
    activeDocument = null;
    elements.fileEditor.hidden = true;
  }
  if (!authenticated && document.body.dataset.view === "admin") selectTab("overview");

  const serverState = normaliseState(minecraft.state);
  const available = Boolean(minecraft.available);
  const allowed = Array.isArray(minecraft.allowed) ? minecraft.allowed : [];
  const playerCount = Number.isInteger(minecraft.players) ? minecraft.players : null;
  elements.serverControls.setAttribute("aria-busy", "false");
  elements.minecraftState.dataset.state = available ? serverState : "unavailable";
  elements.minecraftState.textContent = available ? titleCase(serverState) : "Unavailable";
  elements.minecraftStatusCopy.textContent = available
    ? `${titleCase(serverState)}${playerCount === null ? "" : ` · ${playerCount} player${playerCount === 1 ? "" : "s"}`}`
    : "Minecraft control service is unavailable.";
  for (const button of elements.powerButtons) {
    const action = button.dataset.powerAction;
    button.disabled = !available || !isPowerAllowed(allowed, action);
    button.title = button.disabled ? `${titleCase(action)} is not available in the current server state.` : "";
  }

  setWorkspaceStatus("ready", `${catalogCount} catalog schematic${catalogCount === 1 ? "" : "s"}`, "Ready");
  elements.adminPendingBadge.textContent = String(pendingCount);
  elements.adminPendingBadge.hidden = !authenticated || pendingCount === 0;
  elements.reviewCount.textContent = `${pendingCount} pending`;
  if (!authenticated || !workspace.admin) return;
  const admin = workspace.admin;
  elements.adminWelcome.textContent = `Signed in as ${actor}. Review pending work before making server changes.`;
  renderReviewQueue(admin.attention);
  const filesAvailable = Boolean(admin.files && Array.isArray(admin.files.entries));
  if (!currentFileDirectory || !filesAvailable) renderFiles(admin.files, "");
  const modsAvailable = renderMods(admin.mods);
  renderRestores(admin.restores, filesAvailable || modsAvailable);
  setTextList(elements.adminJobs, admin.jobs, "No operations are in progress.", (item) => safeSummary(item, "Background operation"));
  renderAudit(admin.audit);
}

function renderWorkspaceUnavailable() {
  currentWorkspace = null;
  setWorkspaceStatus("error", "Minecraft workspace unavailable", "Unavailable");
  elements.serverControls.setAttribute("aria-busy", "false");
  elements.minecraftState.dataset.state = "unavailable";
  elements.minecraftState.textContent = "Unavailable";
  elements.minecraftStatusCopy.textContent = "Could not load Minecraft status. Try again shortly.";
  for (const button of elements.powerButtons) button.disabled = true;
  elements.loginOpen.hidden = true;
  elements.logoutButton.hidden = true;
  elements.adminTab.hidden = true;
  elements.sessionState.dataset.state = "guest";
  elements.sessionState.textContent = "Access status unavailable";
  if (document.body.dataset.view === "admin") selectTab("overview");
}

async function refreshWorkspace() {
  try {
    const response = await fetch("/api/workspace", sameOriginOptions());
    if (!response.ok) throw new Error(await responseDetail(response, "Unable to load Minecraft workspace"));
    const workspace = await response.json();
    renderWorkspace(workspace);
    return workspace;
  } catch {
    renderWorkspaceUnavailable();
    return null;
  }
}

/** @param {PortalIntent} intent @returns {Promise<IntentResponse>} */
async function sendIntent(intent) {
  const response = await fetch("/api/intents", sameOriginOptions({method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify(intent)}));
  let result = null;
  try {
    result = await response.json();
  } catch {
    // A valid intent response is always JSON.
  }
  if (!response.ok && !(result && typeof result.outcome === "string")) {
    throw new Error(bodyDetail(result, "The requested action failed"));
  }
  return result;
}

async function sendBinary(url, file, headers) {
  const response = await fetch(url, sameOriginOptions({method: "POST", headers, body: file}));
  let result = null;
  try {
    result = await response.json();
  } catch {
    // Binary endpoints still return a JSON operation result.
  }
  if (!response.ok) {
    throw new Error(bodyDetail(result, "The upload failed"));
  }
  return result;
}

function recommendedTitle(filename) {
  return filename.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim().replace(/\b\w/g, (character) => character.toUpperCase());
}

function setFormBusy(form, busy) {
  form.setAttribute("aria-busy", String(busy));
  for (const control of form.querySelectorAll("button, input, textarea")) control.disabled = busy;
}

async function uploadSchematic(event) {
  event.preventDefault();
  if (!elements.schematicUploadForm.reportValidity()) return;
  const file = elements.schematicFile.files[0];
  if (!file) return;
  setFormBusy(elements.schematicUploadForm, true);
  elements.schematicUploadStatus.textContent = "Uploading and checking schematic…";
  try {
    const metadata = {title: elements.schematicTitle.value.trim(), description: "", tags: []};
    const result = await sendBinary("/api/submissions", file, {
      "x-schematic-filename": file.name,
      "x-schematic-promote": String(elements.schematicPromote.checked),
      "x-schematic-metadata": encodeURIComponent(JSON.stringify(metadata))
    });
    elements.schematicUploadStatus.textContent = result.notice || "Schematic uploaded.";
    showToast(elements.schematicUploadStatus.textContent);
    elements.schematicUploadForm.reset();
    reloadSchematicSession();
    await refreshWorkspace();
  } catch (error) {
    elements.schematicUploadStatus.textContent = error instanceof Error ? error.message : "Schematic upload failed.";
    showToast(elements.schematicUploadStatus.textContent, "error");
  } finally {
    setFormBusy(elements.schematicUploadForm, false);
  }
}

async function executeAdminIntent(intent, button, statusElement) {
  button.disabled = true;
  statusElement.textContent = "Working…";
  try {
    const result = await sendIntent(intent);
    const completed = await applyIntentResult(result, intent);
    statusElement.textContent = completed ? (result.notice || "Completed.") : "Confirmation required.";
    if (!completed) pendingConfirmationStatus = statusElement;
    return result;
  } catch (error) {
    statusElement.textContent = error instanceof Error ? error.message : "Operation failed.";
    showToast(statusElement.textContent, "error");
    return null;
  } finally {
    button.disabled = false;
  }
}

async function reviewSubmission(item, decision, button) {
  const intent = {
    type: "schematic.review",
    submissionId: item.id,
    decision,
    expectedRevision: item.revision,
    idempotencyKey: makeIdempotencyKey(),
    ...(decision === "reject" ? {reasonCode: "admin_rejected"} : {})
  };
  button.disabled = true;
  try {
    const result = await sendIntent(intent);
    await applyIntentResult(result, intent);
    showToast(result.notice || `Schematic ${decision === "publish" ? "published" : "rejected"}.`);
  } catch (error) {
    showToast(error instanceof Error ? error.message : "Review action failed.", "error");
  } finally {
    button.disabled = false;
  }
}

async function listDirectory(directory, button = null) {
  if (button) button.disabled = true;
  elements.fileStatus.textContent = `Opening /${directory || ""}…`;
  const intent = {type: "file.list", directory, idempotencyKey: makeIdempotencyKey()};
  try {
    const result = await sendIntent(intent);
    if (result.outcome !== "done" || !result.files || !Array.isArray(result.files.entries)) {
      throw new Error("The server did not return a directory listing.");
    }
    activeDocument = null;
    elements.fileEditor.hidden = true;
    renderFiles(result.files, directory);
    elements.fileStatus.textContent = `Opened /${result.files.directory || ""}`;
  } catch (error) {
    elements.fileStatus.textContent = error instanceof Error ? error.message : "Could not list directory.";
    showToast(elements.fileStatus.textContent, "error");
  } finally {
    if (button) button.disabled = false;
  }
}

async function readFile(path, button) {
  elements.fileStatus.textContent = `Loading ${path}…`;
  const intent = {type: "file.read", path, idempotencyKey: makeIdempotencyKey()};
  button.disabled = true;
  try {
    const result = await sendIntent(intent);
    if (result.outcome !== "done" || !result.document) throw new Error("The server did not return the requested file.");
    activeDocument = result.document;
    elements.fileEditorName.textContent = result.document.path;
    elements.fileEditorRevision.textContent = `Revision ${result.document.revision.slice(0, 12)}`;
    elements.fileEditorContent.value = result.document.content;
    elements.fileEditor.hidden = false;
    elements.fileStatus.textContent = "File loaded.";
    elements.fileEditorContent.focus();
  } catch (error) {
    elements.fileStatus.textContent = error instanceof Error ? error.message : "Could not read file.";
    showToast(elements.fileStatus.textContent, "error");
  } finally {
    button.disabled = false;
  }
}

async function saveFile(event) {
  event.preventDefault();
  if (!activeDocument) return;
  const intent = {
    type: "file.save",
    path: activeDocument.path,
    content: elements.fileEditorContent.value,
    expectedRevision: activeDocument.revision,
    idempotencyKey: makeIdempotencyKey()
  };
  elements.fileSave.disabled = true;
  elements.fileStatus.textContent = "Saving with revision check…";
  try {
    const result = await sendIntent(intent);
    if (result.outcome !== "done" || !result.document) throw new Error("The server did not confirm the saved file.");
    activeDocument = result.document;
    elements.fileEditorRevision.textContent = `Revision ${result.document.revision.slice(0, 12)}`;
    elements.fileEditorContent.value = result.document.content;
    elements.fileStatus.textContent = result.notice || "File saved.";
    showToast(elements.fileStatus.textContent);
    await refreshWorkspace();
  } catch (error) {
    elements.fileStatus.textContent = error instanceof Error ? error.message : "File save failed.";
    showToast(elements.fileStatus.textContent, "error");
  } finally {
    elements.fileSave.disabled = false;
  }
}

async function changeMod(mod, action, button) {
  const intent = {
    type: `mods.${action}`,
    filename: mod.name,
    expectedRevision: mod.revision,
    idempotencyKey: makeIdempotencyKey()
  };
  await executeAdminIntent(intent, button, elements.modStatus);
}

async function uploadMod(event) {
  event.preventDefault();
  if (!elements.modUploadForm.reportValidity()) return;
  const file = elements.modFile.files[0];
  if (!file) return;
  setFormBusy(elements.modUploadForm, true);
  elements.modStatus.textContent = "Uploading mod disabled…";
  try {
    const result = await sendBinary("/api/admin/mods", file, {
      "x-mod-filename": file.name,
      "idempotency-key": makeIdempotencyKey()
    });
    elements.modStatus.textContent = result.notice || "Mod staged disabled.";
    elements.modUploadForm.reset();
    showToast(elements.modStatus.textContent);
    await refreshWorkspace();
  } catch (error) {
    elements.modStatus.textContent = error instanceof Error ? error.message : "Mod upload failed.";
    showToast(elements.modStatus.textContent, "error");
  } finally {
    setFormBusy(elements.modUploadForm, false);
  }
}

async function createBackup(event) {
  event.preventDefault();
  const intent = {
    type: "backup.create",
    name: elements.backupName.value.trim() || "Minecraft UI safety backup",
    idempotencyKey: makeIdempotencyKey()
  };
  const result = await executeAdminIntent(intent, elements.backupCreate, elements.backupStatus);
  if (result && result.outcome === "done") elements.backupName.value = "";
}

async function prepareRestore(backupId, button) {
  const intent = {type: "restore.prepare", backupId, idempotencyKey: makeIdempotencyKey()};
  await executeAdminIntent(intent, button, elements.backupStatus);
}

function describeIssue(value) {
  return safeSummary(value, "The request was not valid.");
}

function describeJob(value) {
  return safeSummary(value, "The operation was accepted and is being processed.");
}

async function applyIntentResult(result, originalIntent) {
  if (!result || typeof result.outcome !== "string") throw new Error("The server returned an invalid response.");
  if (result.outcome === "done") {
    if (result.workspace) renderWorkspace(result.workspace);
    else await refreshWorkspace();
    if (result.notice) showToast(result.notice);
    return true;
  }
  if (result.outcome === "accepted") {
    showToast(describeJob(result.job));
    await refreshWorkspace();
    return true;
  }
  if (result.outcome === "confirmation_required") {
    openConfirmation(originalIntent, result.challenge);
    return false;
  }
  if (result.outcome === "invalid") {
    const issues = Array.isArray(result.issues) ? result.issues.map(describeIssue) : [];
    const recommendations = Array.isArray(result.recommendations)
      ? result.recommendations.map((item) => safeSummary(item, "")).filter(Boolean)
      : [];
    throw new Error([...issues, ...recommendations].join(" ") || "The requested action is not available.");
  }
  throw new Error("The server returned an unsupported outcome.");
}

function makeIdempotencyKey() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
  return `minecraft-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function openLoginDialog() {
  previousDialogFocus = document.activeElement;
  elements.loginError.hidden = true;
  elements.loginError.textContent = "";
  elements.loginForm.reset();
  elements.loginDialog.showModal();
  window.requestAnimationFrame(() => elements.adminUsername.focus());
}

function closeLoginDialog() {
  if (!elements.loginDialog.open) return;
  elements.loginDialog.close();
  if (previousDialogFocus instanceof HTMLElement) previousDialogFocus.focus();
}

function setLoginBusy(busy) {
  for (const control of [elements.loginSubmit, elements.loginCancel, elements.loginClose, elements.adminUsername, elements.adminPassword]) control.disabled = busy;
  elements.loginSubmit.textContent = busy ? "Signing in…" : "Sign in";
}

function reloadSchematicSession() {
  try {
    elements.schematicFrame.contentWindow.location.reload();
  } catch {
    elements.schematicFrame.src = elements.schematicFrame.src;
  }
}

async function login(event) {
  event.preventDefault();
  if (!elements.loginForm.reportValidity()) return;
  setLoginBusy(true);
  elements.loginError.hidden = true;
  try {
    const intent = {type: "session.login", username: elements.adminUsername.value.trim(), password: elements.adminPassword.value};
    const completed = await applyIntentResult(await sendIntent(intent), intent);
    if (completed) {
      closeLoginDialog();
      reloadSchematicSession();
      showToast("Signed in to the administrator workspace.");
    }
  } catch (error) {
    elements.loginError.textContent = error instanceof Error ? error.message : "Sign in failed";
    elements.loginError.hidden = false;
    elements.adminPassword.select();
  } finally {
    setLoginBusy(false);
  }
}

async function logout() {
  elements.logoutButton.disabled = true;
  try {
    const intent = {type: "session.logout"};
    await applyIntentResult(await sendIntent(intent), intent);
    reloadSchematicSession();
    showToast("Signed out.");
  } catch (error) {
    showToast(error instanceof Error ? error.message : "Sign out failed", "error");
  } finally {
    elements.logoutButton.disabled = false;
  }
}

function setPowerBusy(busy) {
  elements.serverControls.setAttribute("aria-busy", String(busy));
  if (busy) for (const button of elements.powerButtons) button.disabled = true;
  else if (currentWorkspace) renderWorkspace(currentWorkspace);
}

async function requestPower(action) {
  const intent = {type: "minecraft.power", action, idempotencyKey: makeIdempotencyKey()};
  setPowerBusy(true);
  try {
    const completed = await applyIntentResult(await sendIntent(intent), intent);
    if (completed) showToast(`${titleCase(action)} request completed.`);
  } catch (error) {
    showToast(error instanceof Error ? error.message : "Server operation failed", "error");
  } finally {
    setPowerBusy(false);
  }
}

function openConfirmation(intent, challenge) {
  previousDialogFocus = document.activeElement;
  pendingConfirmation = {
    ...intent,
    ...(intent.type === "restore.prepare" ? {type: "restore.execute"} : {}),
    confirmation: challenge.token
  };
  elements.confirmMessage.textContent = challenge.message || "Confirm this Minecraft operation.";
  elements.confirmEffects.replaceChildren();
  const effects = Array.isArray(challenge.effects) ? challenge.effects : [];
  elements.confirmEffects.hidden = effects.length === 0;
  for (const effect of effects) {
    const item = document.createElement("li");
    item.textContent = String(effect);
    elements.confirmEffects.append(item);
  }
  elements.confirmError.hidden = true;
  elements.confirmError.textContent = "";
  elements.confirmDialog.showModal();
  window.requestAnimationFrame(() => elements.confirmSubmit.focus());
}

function closeConfirmation() {
  if (!elements.confirmDialog.open) return;
  pendingConfirmation = null;
  if (pendingConfirmationStatus) pendingConfirmationStatus.textContent = "Operation cancelled.";
  pendingConfirmationStatus = null;
  elements.confirmDialog.close();
  setPowerBusy(false);
  if (previousDialogFocus instanceof HTMLElement) previousDialogFocus.focus();
}

function setConfirmationBusy(busy) {
  for (const control of [elements.confirmSubmit, elements.confirmCancel, elements.confirmClose]) control.disabled = busy;
  elements.confirmSubmit.textContent = busy ? "Confirming…" : "Confirm";
}

async function confirmIntent(event) {
  event.preventDefault();
  if (!pendingConfirmation) return;
  setConfirmationBusy(true);
  elements.confirmError.hidden = true;
  try {
    const intent = pendingConfirmation;
    const result = await sendIntent(intent);
    const completed = await applyIntentResult(result, intent);
    if (completed) {
      pendingConfirmation = null;
      elements.confirmDialog.close();
      setPowerBusy(false);
      if (pendingConfirmationStatus) pendingConfirmationStatus.textContent = result.notice || "Completed.";
      pendingConfirmationStatus = null;
      showToast("Confirmed operation completed.");
    }
  } catch (error) {
    elements.confirmError.textContent = error instanceof Error ? error.message : "Confirmation failed";
    elements.confirmError.hidden = false;
  } finally {
    setConfirmationBusy(false);
  }
}

for (const tab of elements.tabs) {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  tab.addEventListener("keydown", handleTabKeydown);
}
for (const button of elements.powerButtons) button.addEventListener("click", () => requestPower(button.dataset.powerAction));
elements.openSchematics.addEventListener("click", () => selectTab("schematics", {focus: true}));
elements.copyAddress.addEventListener("click", copyConnectAddress);
elements.schematicFile.addEventListener("change", () => {
  const file = elements.schematicFile.files[0];
  if (file && !elements.schematicTitle.value.trim()) elements.schematicTitle.value = recommendedTitle(file.name);
});
elements.schematicUploadForm.addEventListener("submit", uploadSchematic);
elements.loginOpen.addEventListener("click", openLoginDialog);
elements.accessAction.addEventListener("click", openLoginDialog);
elements.loginClose.addEventListener("click", closeLoginDialog);
elements.loginCancel.addEventListener("click", closeLoginDialog);
elements.loginForm.addEventListener("submit", login);
elements.logoutButton.addEventListener("click", logout);
elements.fileEditor.addEventListener("submit", saveFile);
elements.modUploadForm.addEventListener("submit", uploadMod);
elements.backupForm.addEventListener("submit", createBackup);
elements.confirmClose.addEventListener("click", closeConfirmation);
elements.confirmCancel.addEventListener("click", closeConfirmation);
elements.confirmForm.addEventListener("submit", confirmIntent);
elements.loginDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  if (!elements.loginSubmit.disabled) closeLoginDialog();
});
elements.confirmDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  if (!elements.confirmSubmit.disabled) closeConfirmation();
});
window.addEventListener("popstate", () => selectTab(selectedTabFromLocation(), {updateHistory: false}));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshWorkspace();
});

configureLocalLinks();
selectTab(selectedTabFromLocation(), {updateHistory: false});
refreshWorkspace();
