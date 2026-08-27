"use strict";

const elements = {
  tabs: [...document.querySelectorAll('[role="tab"]')],
  panels: [...document.querySelectorAll('[role="tabpanel"]')],
  lanternHome: document.getElementById("lantern-home"),
  manageServer: document.getElementById("manage-server"),
  connectAddress: document.getElementById("connect-address"),
  copyAddress: document.getElementById("copy-address"),
  workspaceState: document.getElementById("workspace-state"),
  workspacePill: document.getElementById("workspace-pill"),
  openSchematics: document.getElementById("open-schematics"),
  schematicFrame: document.getElementById("schematic-frame"),
  sessionState: document.getElementById("session-state"),
  loginOpen: document.getElementById("login-open"),
  logoutButton: document.getElementById("logout-button"),
  accessDescription: document.getElementById("access-description"),
  accessAction: document.getElementById("access-action"),
  loginDialog: document.getElementById("login-dialog"),
  loginForm: document.getElementById("login-form"),
  loginClose: document.getElementById("login-close"),
  loginCancel: document.getElementById("login-cancel"),
  loginSubmit: document.getElementById("login-submit"),
  adminPassword: document.getElementById("admin-password"),
  loginError: document.getElementById("login-error"),
  toast: document.getElementById("toast")
};

let toastTimer;
let previousDialogFocus = null;

function sameOriginOptions(options = {}) {
  return {
    credentials: "same-origin",
    ...options,
    headers: {
      accept: "application/json",
      ...(options.headers || {})
    }
  };
}

async function responseDetail(response, fallback) {
  try {
    const body = await response.json();
    return typeof body.detail === "string" ? body.detail : fallback;
  } catch {
    return fallback;
  }
}

function showToast(message, tone = "info") {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.dataset.tone = tone;
  elements.toast.classList.add("is-visible");
  toastTimer = window.setTimeout(() => {
    elements.toast.classList.remove("is-visible");
  }, 3600);
}

function configureLocalLinks() {
  const lanternUrl = new URL(window.location.href);
  lanternUrl.port = "8090";
  lanternUrl.pathname = "/";
  lanternUrl.search = "";
  lanternUrl.hash = "";

  elements.lanternHome.href = lanternUrl.href;
  elements.manageServer.href = lanternUrl.href;
  elements.connectAddress.textContent = `${window.location.hostname}:25565`;
}

function selectedTabFromLocation() {
  return window.location.hash === "#schematics" ? "schematics" : "overview";
}

function selectTab(name, { focus = false, updateHistory = true } = {}) {
  const selectedTab = elements.tabs.find((tab) => tab.dataset.tab === name) || elements.tabs[0];
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
    url.hash = selectedName === "schematics" ? "schematics" : "";
    window.history.pushState({ tab: selectedName }, "", url);
  }
}

function handleTabKeydown(event) {
  const currentIndex = elements.tabs.indexOf(event.currentTarget);
  let nextIndex = currentIndex;

  if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % elements.tabs.length;
  else if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + elements.tabs.length) % elements.tabs.length;
  else if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = elements.tabs.length - 1;
  else return;

  event.preventDefault();
  selectTab(elements.tabs[nextIndex].dataset.tab, { focus: true });
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

async function refreshWorkspaceReadiness() {
  setWorkspaceStatus("checking", "Checking schematic workspace…", "Checking");
  try {
    const response = await fetch("/readyz", sameOriginOptions());
    if (!response.ok) throw new Error(await responseDetail(response, "Schematic workspace unavailable"));
    setWorkspaceStatus("ready", "Schematic workspace ready", "Ready");
  } catch {
    setWorkspaceStatus("error", "Schematic workspace unavailable", "Unavailable");
  }
}

function renderSession(session) {
  const enabled = Boolean(session.enabled);
  const authenticated = enabled && Boolean(session.authenticated);

  elements.sessionState.dataset.state = authenticated ? "admin" : "guest";
  elements.loginOpen.hidden = !enabled || authenticated;
  elements.logoutButton.hidden = !authenticated;
  elements.accessAction.hidden = !enabled || authenticated;

  if (!enabled) {
    elements.sessionState.textContent = "Read-only library";
    elements.accessDescription.textContent =
      "Shared schematics are readable. Administrator changes are unavailable on this connection.";
  } else if (authenticated) {
    elements.sessionState.textContent = "Administrator";
    elements.accessDescription.textContent =
      "Administrator access is active. You can add, version, restore, and remove shared schematics.";
  } else {
    elements.sessionState.textContent = "Guest · read only";
    elements.accessDescription.textContent =
      "Shared schematics are readable without signing in. Administrator access enables library changes.";
  }
}

async function refreshSession() {
  try {
    const response = await fetch("/api/session", sameOriginOptions());
    if (!response.ok) throw new Error(await responseDetail(response, "Unable to check access"));
    const session = await response.json();
    renderSession(session);
    return session;
  } catch {
    renderSession({ enabled: false, authenticated: false });
    elements.sessionState.textContent = "Access status unavailable";
    showToast("Could not check administrator access.", "error");
    return null;
  }
}

function openLoginDialog() {
  previousDialogFocus = document.activeElement;
  elements.loginError.hidden = true;
  elements.loginError.textContent = "";
  elements.loginForm.reset();
  elements.loginDialog.showModal();
  window.requestAnimationFrame(() => elements.adminPassword.focus());
}

function closeLoginDialog() {
  if (!elements.loginDialog.open) return;
  elements.loginDialog.close();
  if (previousDialogFocus instanceof HTMLElement) previousDialogFocus.focus();
}

function setLoginBusy(busy) {
  elements.loginSubmit.disabled = busy;
  elements.loginCancel.disabled = busy;
  elements.loginClose.disabled = busy;
  elements.adminPassword.disabled = busy;
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
    const response = await fetch("/api/session/login", sameOriginOptions({
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password: elements.adminPassword.value })
    }));
    if (!response.ok) {
      let message = await responseDetail(response, "Sign in failed");
      if (response.status === 429) {
        const retryAfter = response.headers.get("Retry-After");
        if (retryAfter) message = `${message}. Try again in ${retryAfter} seconds.`;
      }
      throw new Error(message);
    }

    await refreshSession();
    closeLoginDialog();
    reloadSchematicSession();
    showToast("Administrator access enabled.");
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
    const response = await fetch("/api/session/logout", sameOriginOptions({ method: "POST" }));
    if (!response.ok) throw new Error(await responseDetail(response, "Sign out failed"));
    await refreshSession();
    reloadSchematicSession();
    showToast("Signed out. The shared library is read only.");
  } catch (error) {
    showToast(error instanceof Error ? error.message : "Sign out failed", "error");
  } finally {
    elements.logoutButton.disabled = false;
  }
}

for (const tab of elements.tabs) {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  tab.addEventListener("keydown", handleTabKeydown);
}

elements.openSchematics.addEventListener("click", () => selectTab("schematics", { focus: true }));
elements.copyAddress.addEventListener("click", copyConnectAddress);
elements.loginOpen.addEventListener("click", openLoginDialog);
elements.accessAction.addEventListener("click", openLoginDialog);
elements.loginClose.addEventListener("click", closeLoginDialog);
elements.loginCancel.addEventListener("click", closeLoginDialog);
elements.loginForm.addEventListener("submit", login);
elements.logoutButton.addEventListener("click", logout);
elements.loginDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  if (!elements.loginSubmit.disabled) closeLoginDialog();
});
window.addEventListener("popstate", () => {
  selectTab(selectedTabFromLocation(), { updateHistory: false });
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshSession();
});

configureLocalLinks();
selectTab(selectedTabFromLocation(), { updateHistory: false });
refreshSession();
refreshWorkspaceReadiness();
