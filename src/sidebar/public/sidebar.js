import zoomSdk from "@zoom/appssdk";

// --- State ---
var authToken = null;
var meetingUuid = null;
var userId = null;
var ws = null;
var participants = new Map();
var sessionStage = null; // null | "pending" | "connected" | "recording" | "analyzing" | "complete" | "error"

// --- DOM refs ---
var sectionLoading = document.getElementById("section-loading");
var sectionSetup = document.getElementById("section-setup");
var sectionReady = document.getElementById("section-ready");
var sectionScanning = document.getElementById("section-scanning");
var errorMsg = document.getElementById("error-msg");
var apiKeyForm = document.getElementById("api-key-form");
var apiKeyInput = document.getElementById("api-key-input");
var saveKeyBtn = document.getElementById("save-key-btn");
var startScanBtn = document.getElementById("start-scan-btn");
var stopScanBtn = document.getElementById("stop-scan-btn");
var changeKeyBtn = document.getElementById("change-key-btn");
var participantsContainer = document.getElementById("participants-container");
var noParticipants = document.getElementById("no-participants");
var scanStatusBadge = document.getElementById("scan-status-badge");
var excludeSelfToggle = document.getElementById("exclude-self-toggle");
var rescanSelect = document.getElementById("rescan-select");
var scanAllBtn = document.getElementById("scan-all-btn");

// --- Helpers ---
function showSection(section) {
  [sectionLoading, sectionSetup, sectionReady, sectionScanning].forEach(function (s) {
    s.classList.remove("active");
  });
  section.classList.add("active");
}

function showError(msg) {
  errorMsg.textContent = msg;
  errorMsg.classList.add("visible");
  setTimeout(function () {
    errorMsg.classList.remove("visible");
  }, 5000);
}

function apiUrl(path) {
  return window.location.origin + path;
}

function wsUrl() {
  var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return proto + "//" + window.location.host + "/ws/sidebar?token=" + encodeURIComponent(authToken);
}

function updateSessionBadge() {
  if (!scanStatusBadge) return;
  switch (sessionStage) {
    case "pending":
      scanStatusBadge.textContent = "Connecting...";
      scanStatusBadge.className = "status-badge status-scanning status-shimmer";
      break;
    case "connected":
      scanStatusBadge.textContent = "Connected";
      scanStatusBadge.className = "status-badge status-scanning status-shimmer";
      break;
    case "recording":
      scanStatusBadge.textContent = "Recording";
      scanStatusBadge.className = "status-badge status-scanning status-shimmer";
      break;
    case "analyzing":
      scanStatusBadge.textContent = "Analyzing";
      scanStatusBadge.className = "status-badge status-scanning status-shimmer";
      break;
    case "monitoring":
      scanStatusBadge.textContent = "Monitoring";
      scanStatusBadge.className = "status-badge status-monitoring status-shimmer";
      break;
    case "complete":
      scanStatusBadge.textContent = "Complete";
      scanStatusBadge.className = "status-badge status-live reveal";
      break;
    case "error":
      scanStatusBadge.textContent = "Error";
      scanStatusBadge.className = "status-badge status-error";
      break;
    default:
      scanStatusBadge.textContent = "Scanning";
      scanStatusBadge.className = "status-badge status-scanning status-shimmer";
  }

  // Show "Scan All" button only when monitoring or when all initial scans are done
  if (scanAllBtn) {
    var showScanAll = sessionStage === "monitoring" || sessionStage === "complete";
    scanAllBtn.style.display = showScanAll ? "inline-block" : "none";
  }
}

// --- Init ---
async function init() {
  try {
    await zoomSdk.config({
      capabilities: [
        "getAppContext",
        "startRTMS",
        "stopRTMS",
        "onRTMSStatusChange",
      ],
    });

    var ctx = await zoomSdk.getAppContext();
    var contextValue = typeof ctx === "string" ? ctx : ctx.context;
    var resp = await fetch(apiUrl("/api/sidebar/auth"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ context: contextValue }),
    });

    if (!resp.ok) {
      throw new Error("Auth failed: " + resp.status);
    }

    var data = await resp.json();
    authToken = data.token;
    meetingUuid = data.meetingUuid;
    userId = data.userId;

    if (data.hasApiKey) {
      showSection(sectionReady);
    } else {
      showSection(sectionSetup);
    }
  } catch (err) {
    showError("Failed to initialize: " + err.message);
    console.error("Init error:", err);
  }
}

// --- API Key form ---
apiKeyForm.addEventListener("submit", async function (e) {
  e.preventDefault();
  var key = apiKeyInput.value.trim();
  if (!key) return;

  saveKeyBtn.disabled = true;
  saveKeyBtn.innerHTML = '<span class="spinner"></span>Saving...';

  try {
    var resp = await fetch(apiUrl("/api/sidebar/api-key"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + authToken,
      },
      body: JSON.stringify({ apiKey: key }),
    });

    if (!resp.ok) {
      var errData = await resp.json();
      throw new Error(errData.error || "Failed to save key");
    }

    apiKeyInput.value = "";
    showSection(sectionReady);
  } catch (err) {
    showError(err.message);
  } finally {
    saveKeyBtn.disabled = false;
    saveKeyBtn.textContent = "Save API Key";
  }
});

// --- Change API Key ---
changeKeyBtn.addEventListener("click", function () {
  showSection(sectionSetup);
});

// --- Start Scan ---
startScanBtn.addEventListener("click", async function () {
  startScanBtn.disabled = true;
  startScanBtn.innerHTML = '<span class="spinner"></span>Starting...';

  try {
    connectWebSocket();

    // Stop any existing RTMS session first (may fail if none active — that's fine)
    try { await zoomSdk.stopRTMS(); } catch (_) { /* ignore */ }

    try {
      await zoomSdk.startRTMS();
    } catch (rtmsErr) {
      // RTMS may already be running (e.g., sidebar was refreshed).
      // Continue to scanning view — the server session is likely still active.
      console.warn("startRTMS failed (may already be active):", rtmsErr);
    }

    participants.clear();
    sessionStage = "pending";
    updateSessionBadge();
    renderParticipants();
    showSection(sectionScanning);
  } catch (err) {
    showError("Failed to start scan: " + err.message);
    console.error("Start scan error:", err);
  } finally {
    startScanBtn.disabled = false;
    startScanBtn.textContent = "Start Liveness Scan";
  }
});

// --- Stop Scan ---
stopScanBtn.addEventListener("click", async function () {
  try {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "stop_monitoring", meetingUuid: meetingUuid }));
    }
    await zoomSdk.stopRTMS();
    sessionStage = "complete";
    updateSessionBadge();
  } catch (err) {
    console.error("Stop scan error:", err);
  }
});

// --- Scan All ---
if (scanAllBtn) {
  scanAllBtn.addEventListener("click", function () {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    var count = 0;
    participants.forEach(function (data, pid) {
      if (data.stage === "done") {
        ws.send(JSON.stringify({ type: "retry_participant", meetingUuid: meetingUuid, participantId: pid }));
        // Reset local state for responsive UI
        data.verdict = null;
        data.score = null;
        data.error = null;
        data.stage = "recording";
        data.framesCollected = 0;
        data.framesNeeded = 4;
        data.verdictChanged = false;
        participants.set(pid, data);
        count++;
      }
    });
    if (count > 0) {
      sessionStage = "recording";
      updateSessionBadge();
      renderParticipants();
    }
  });
}

// --- WebSocket ---
function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  ws = new WebSocket(wsUrl());

  ws.onopen = function () {
    console.log("Sidebar WS connected");
    var monitorMsg = { type: "start_monitoring", meetingUuid: meetingUuid };
    if (excludeSelfToggle && excludeSelfToggle.checked) {
      monitorMsg.excludeSelf = true;
    }
    if (rescanSelect) {
      var interval = parseInt(rescanSelect.value, 10);
      if (interval > 0) {
        monitorMsg.rescanInterval = interval;
      }
    }
    ws.send(JSON.stringify(monitorMsg));
  };

  ws.onmessage = function (event) {
    try {
      var msg = JSON.parse(event.data);
      handleWsMessage(msg);
    } catch (err) {
      console.error("WS parse error:", err);
    }
  };

  ws.onclose = function () {
    console.log("Sidebar WS disconnected");
  };

  ws.onerror = function (err) {
    console.error("Sidebar WS error:", err);
  };
}

function handleWsMessage(msg) {
  switch (msg.type) {
    case "stage": {
      // Lifecycle stage updates
      if (msg.stage === "connected") {
        sessionStage = "connected";
        // Update the "waiting" text to show connected
        noParticipants.textContent = "Connected to meeting. Waiting for video...";
      } else if (msg.stage === "recording" && msg.participantId) {
        sessionStage = "recording";
        // Ensure participant exists with recording stage
        var rp = participants.get(msg.participantId) || {};
        if (msg.userName) rp.userName = msg.userName;
        rp.stage = "recording";
        if (!rp.framesCollected) rp.framesCollected = 0;
        participants.set(msg.participantId, rp);
        renderParticipants();
      } else if (msg.stage === "decoding" && msg.participantId) {
        sessionStage = "recording";
        var dp = participants.get(msg.participantId) || {};
        dp.stage = "decoding";
        participants.set(msg.participantId, dp);
        renderParticipants();
      } else if (msg.stage === "analyzing" && msg.participantId) {
        var ap = participants.get(msg.participantId) || {};
        ap.stage = "analyzing";
        participants.set(msg.participantId, ap);
        renderParticipants();
      }
      updateSessionBadge();
      break;
    }
    case "scan_progress": {
      var p = participants.get(msg.participantId) || {};
      if (msg.userName) p.userName = msg.userName;
      p.framesCollected = msg.framesCollected;
      p.framesNeeded = msg.framesNeeded;
      if (msg.framesCollected > 0) p.stage = "recording";
      if (!p.verdict) p.verdict = null;
      participants.set(msg.participantId, p);
      renderParticipants();
      break;
    }
    case "participant_result": {
      var pr = participants.get(msg.participantId) || {};
      // Detect if this is a re-scan (participant already had a verdict)
      var hadPreviousVerdict = pr.verdict != null;
      var previousVerdict = pr.verdict;
      pr.verdict = msg.verdict;
      pr.score = msg.score;
      pr.confidence = msg.confidence;
      pr.error = msg.error;
      pr.stage = "done";
      pr.lastCheckedAt = Date.now();
      pr.scanCount = msg.scanCount || (pr.scanCount || 0) + 1;
      // Flag verdict change for flash animation
      if (hadPreviousVerdict && previousVerdict !== msg.verdict) {
        pr.verdictChanged = true;
      } else {
        pr.verdictChanged = false;
      }
      participants.set(msg.participantId, pr);
      renderParticipants();

      // Check if all participants are done
      var allDone = true;
      participants.forEach(function (pd) {
        if (pd.stage !== "done") allDone = false;
      });
      if (allDone && participants.size > 0) {
        // If rescan is enabled, show "Monitoring" instead of "Complete"
        var rescanEnabled = rescanSelect && parseInt(rescanSelect.value, 10) > 0;
        sessionStage = rescanEnabled ? "monitoring" : "complete";
        updateSessionBadge();
      }
      break;
    }
    case "session_state": {
      if (msg.state === "complete") {
        sessionStage = "complete";
      } else if (msg.state === "error") {
        sessionStage = "error";
      } else if (msg.state === "active" || msg.state === "pending") {
        sessionStage = "recording";
      }
      updateSessionBadge();
      break;
    }
    case "error": {
      showError(msg.message || "An error occurred");
      break;
    }
  }
}

// --- Render ---
function renderParticipants() {
  if (participants.size === 0) {
    noParticipants.style.display = "block";
    return;
  }
  noParticipants.style.display = "none";

  var existingCards = participantsContainer.querySelectorAll(".participant-card");
  existingCards.forEach(function (card) {
    if (!participants.has(card.dataset.pid)) {
      card.remove();
    }
  });

  participants.forEach(function (data, pid) {
    var card = participantsContainer.querySelector('[data-pid="' + pid + '"]');
    if (!card) {
      card = document.createElement("div");
      card.className = "participant-card";
      card.dataset.pid = pid;
      participantsContainer.appendChild(card);
    }
    // Add result border glow on verdict
    card.classList.remove("result-live", "result-fake", "card-alert");
    if (data.verdict === "live") card.classList.add("result-live");
    else if (data.verdict === "fake") card.classList.add("result-fake");
    // Flash animation when verdict changes (e.g., live → fake)
    if (data.verdictChanged) {
      card.classList.add("card-alert");
      card.addEventListener("animationend", function () { card.classList.remove("card-alert"); }, { once: true });
    }

    card.innerHTML = renderParticipantCard(pid, data);
  });
}

function retryParticipant(pid) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    // Spin the retry button for tactile feedback
    var card = participantsContainer.querySelector('[data-pid="' + pid + '"]');
    if (card) {
      var btn = card.querySelector(".btn-retry");
      if (btn) {
        btn.classList.add("spinning");
        btn.addEventListener("animationend", function () { btn.classList.remove("spinning"); }, { once: true });
      }
    }

    ws.send(JSON.stringify({ type: "retry_participant", meetingUuid: meetingUuid, participantId: pid }));
    // Reset local state immediately for responsive UI
    var p = participants.get(pid) || {};
    p.verdict = null;
    p.score = null;
    p.error = null;
    p.stage = "recording";
    p.framesCollected = 0;
    p.framesNeeded = 4;
    participants.set(pid, p);
    renderParticipants();
  }
}

function renderParticipantCard(pid, data) {
  var displayName = data.userName ? escapeHtml(data.userName) : "Participant " + escapeHtml(pid);
  var retryBtn = '<button class="btn-retry" onclick="retryParticipant(\'' + escapeHtml(pid) + '\')" title="Rescan">\u21BB</button>';
  var scanCountBadge = data.scanCount && data.scanCount > 1 ? '<span class="scan-count">x' + data.scanCount + '</span>' : '';
  var html = '<div class="card-header"><div class="name">' + displayName + scanCountBadge + "</div>" + retryBtn + "</div>";

  if (data.error) {
    html += '<div class="details">';
    html += '<span class="status-badge status-error reveal">Error</span>';
    html += '<span style="font-size:12px;color:#666;">' + escapeHtml(data.error) + "</span>";
    html += "</div>";
  } else if (data.verdict) {
    var isLive = data.verdict === "live";
    html += '<div class="details">';
    html +=
      '<span class="status-badge reveal ' +
      (isLive ? "status-live" : "status-fake") +
      '">' +
      data.verdict.toUpperCase() +
      "</span>";
    html +=
      '<span class="score reveal ' +
      (isLive ? "live" : "fake") +
      '">' +
      data.score +
      "</span>";
    html += "</div>";
    // Show "last checked" timestamp for re-scans
    if (data.lastCheckedAt && data.scanCount > 1) {
      var ago = formatTimeAgo(data.lastCheckedAt);
      html += '<div class="last-checked">Checked ' + ago + '</div>';
    }
  } else if (data.stage === "decoding") {
    html += '<div class="details">';
    html += '<span class="status-badge status-scanning">Decoding...</span>';
    html += '<span style="font-size:12px;color:#666;">Processing video frames</span>';
    html += "</div>";
    html += '<div class="progress-bar indeterminate"><div class="fill"></div></div>';
  } else if (data.stage === "analyzing") {
    html += '<div class="details">';
    html += '<span class="status-badge status-scanning">Analyzing...</span>';
    html += '<span style="font-size:12px;color:#666;">Submitting to Moveris API</span>';
    html += "</div>";
    html += '<div class="progress-bar indeterminate"><div class="fill"></div></div>';
  } else {
    // Recording — time-based progress (seconds accumulated)
    var elapsedSec = data.framesCollected || 0;
    var targetSec = data.framesNeeded || 4;
    var pct = targetSec > 0 ? Math.min(100, Math.round((elapsedSec / targetSec) * 100)) : 0;
    var stageLabel = elapsedSec === 0 ? "Detected" : "Recording";
    var timeLabel = elapsedSec.toFixed(1) + "s / " + targetSec.toFixed(1) + "s";
    html += '<div class="details">';
    html += '<span class="status-badge status-scanning">' + stageLabel + '</span>';
    html += '<span style="font-size:12px;color:#666;">' + timeLabel + '</span>';
    html += "</div>";
    html += '<div class="progress-bar"><div class="fill" style="width:' + pct + '%"></div></div>';
  }

  return html;
}

function formatTimeAgo(timestamp) {
  var seconds = Math.floor((Date.now() - timestamp) / 1000);
  if (seconds < 60) return "just now";
  var minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + "m ago";
  var hours = Math.floor(minutes / 60);
  return hours + "h ago";
}

function escapeHtml(str) {
  var div = document.createElement("div");
  div.appendChild(document.createTextNode(String(str)));
  return div.innerHTML;
}

// --- Boot ---
init();
