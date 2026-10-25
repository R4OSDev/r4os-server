"use strict";

const navigationItems = [...document.querySelectorAll("[data-view]")];
const contentArea = document.querySelector("#content-area");
const connection = document.querySelector("#connection");
const connectionLabel = document.querySelector("#connection-label");
const statusMessage = document.querySelector("#status-message");
const version = document.querySelector("#version");
let connected = false;
let checking = false;
let currentMessage = "Bereit.";

function showStatus(message, isError = false) {
  statusMessage.textContent = message;
  statusMessage.title = message;
  statusMessage.classList.toggle("is-error", isError);
}

function selectView(announce = true) {
  const requestedView = window.location.hash.slice(1) || "apps";
  const selected = navigationItems.find((item) => item.dataset.view === requestedView) || navigationItems[0];
  for (const item of navigationItems) {
    if (item === selected) {
      item.setAttribute("aria-current", "page");
    } else {
      item.removeAttribute("aria-current");
    }
  }
  const label = selected.textContent.trim();
  document.title = `${label} – R4OS Server`;
  contentArea.setAttribute("aria-label", `Arbeitsbereich: ${label}`);
  if (announce) {
    currentMessage = `Bereich „${label}“ ausgewählt.`;
    if (connected) showStatus(currentMessage);
  }
}

async function checkConnection() {
  if (checking) return;
  checking = true;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 4000);
  try {
    const response = await fetch("/api/status", {
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error("Status nicht verfügbar");
    const status = await response.json();
    if (status.status !== "ok" || typeof status.version !== "string" || !status.version) {
      throw new Error("Ungültige Statusantwort");
    }
    const wasConnected = connected;
    connected = true;
    connection.dataset.state = "connected";
    connectionLabel.textContent = "Verbunden";
    connection.title = "WebApp erreichbar";
    version.textContent = `v${status.version}`;
    if (!wasConnected) showStatus(currentMessage);
  } catch {
    connected = false;
    connection.dataset.state = "error";
    connectionLabel.textContent = "Getrennt";
    connection.title = "WebApp nicht erreichbar";
    showStatus("Verbindung zur WebApp unterbrochen. Erneuter Versuch erfolgt automatisch.", true);
  } finally {
    window.clearTimeout(timeout);
    checking = false;
  }
}

window.addEventListener("hashchange", () => selectView());
window.addEventListener("online", checkConnection);
selectView(false);
checkConnection();
window.setInterval(checkConnection, 15000);
