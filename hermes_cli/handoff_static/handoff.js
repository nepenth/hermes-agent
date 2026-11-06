import RFB from "../novnc/core/rfb.js";

const message = document.getElementById("message");
const screen = document.getElementById("screen");
const modeButton = document.getElementById("mode");
const handbackButton = document.getElementById("handback");
const timer = document.getElementById("timer");
let session = null;
let rfb = null;
let holding = false;
let countdown = null;
let handedBack = false;
let retries = 0;
const MAX_RETRIES = 3;

async function wakeDesktop() {
  show("Starting the agent's desktop…");
  await api("api/wake", {});
}

function show(text) {
  message.textContent = text;
  message.hidden = false;
}

function hideMessage() {
  message.hidden = true;
}

async function api(path, body) {
  const response = await fetch(`./${path}`, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function setHolding(value) {
  holding = value;
  modeButton.textContent = value ? "Watch only" : "Take control";
  if (rfb) rfb.viewOnly = !value;
}

function startCountdown(deadline) {
  const render = () => {
    const remaining = Math.max(0, Math.ceil(Number(deadline) - Date.now() / 1000));
    timer.textContent = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;
    if (!remaining) {
      clearInterval(countdown);
      show("This handoff session has expired.");
    }
  };
  render();
  countdown = setInterval(render, 1000);
}

function connectScreen() {
  const url = new URL("../ws", import.meta.url);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  rfb = new RFB(screen, url.href);
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.viewOnly = !holding;
  rfb.addEventListener("connect", async () => {
    retries = 0;
    hideMessage();
    // A reload or second tab shares this handoff's viewer id; the closing socket may have released
    // the lease. Re-assert control when this page believes it holds it.
    if (holding) {
      try { await api("api/takeover", {}); } catch (error) { show(error.message); }
    }
  });
  rfb.addEventListener("disconnect", (event) => {
    if (handedBack) return;
    if (event.detail?.clean) {
      show("The desktop connection closed.");
      return;
    }
    // The screen may have stopped (idle) or the agent host restarted: wake it and reconnect a few times
    // before asking the human to do anything.
    if (retries < MAX_RETRIES) {
      retries += 1;
      show(`Reconnecting to the agent's desktop (attempt ${retries} of ${MAX_RETRIES})…`);
      setTimeout(async () => {
        try { await wakeDesktop(); } catch (error) { show(error.message); return; }
        connectScreen();
      }, 1500 * retries);
      return;
    }
    show("Could not reach the agent's desktop. Reload this page to try again.");
  });
}

async function start() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get("t");
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  try {
    session = token ? await api("api/redeem", { token }) : await api("api/status");
    document.getElementById("agent").textContent = session.agent;
    document.getElementById("reason").textContent = session.reason;
    startCountdown(session.session_deadline);
    await wakeDesktop();
    await api("api/takeover", {});
    setHolding(true);
    connectScreen();
  } catch (error) {
    show(error.message || "Could not open this desktop handoff.");
    modeButton.disabled = true;
    handbackButton.disabled = true;
  }
}

modeButton.addEventListener("click", async () => {
  modeButton.disabled = true;
  try {
    await api(holding ? "api/release" : "api/takeover", {});
    setHolding(!holding);
  } catch (error) {
    show(error.message);
  } finally {
    modeButton.disabled = false;
  }
});

handbackButton.addEventListener("click", async () => {
  handbackButton.disabled = true;
  try {
    await api("api/handback", {});
    handedBack = true;
    if (rfb) rfb.disconnect();
    clearInterval(countdown);
    timer.textContent = "";
    modeButton.disabled = true;
    show("Handed back — you can close this tab.");
  } catch (error) {
    show(error.message);
    handbackButton.disabled = false;
  }
});

start();
