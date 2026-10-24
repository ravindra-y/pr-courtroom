/* ==========================================================================
   PR Courtroom — Frontend Application (Plain JS, EventSource SSE)
   ========================================================================== */

// --- 1. Constants & State ---
const DEMO_PR_URL = "https://github.com/demo/shop/pull/1";
const REPLAY_STORAGE_KEY = "pr_courtroom_replay_mode";
const STEP_ORDER = ["fetch", "prosecution", "evidence", "defense", "verdict"];
const EXHIBIT_LETTERS = ["A", "B", "C", "D", "E", "F"];

let activeEventSource = null;
let toastTimer = null;

const trialState = {
  running: false,
  finished: false,
  prUrl: "",
  replay: true,
  events: [],
  trialStarted: null,
  chargesById: {},
  chargeOrder: [],
  exhibitLetterByChargeId: {},
  testResultsByChargeId: {},
  droppedByChargeId: {},
  defensesByChargeId: {},
  fixes: [],
  verdict: null,
  transcriptCount: 0,
  exhibitsCount: 0,
};

// --- 2. DOM References ---
const dom = {
  screenFlash: document.getElementById("screen-flash"),
  trialForm: document.getElementById("trial-form"),
  prUrlInput: document.getElementById("pr-url-input"),
  urlValidationMsg: document.getElementById("url-validation-msg"),
  loadDemoBtn: document.getElementById("load-demo-btn"),
  replayCheckbox: document.getElementById("replay-checkbox"),
  startBtn: document.getElementById("start-btn"),
  cancelBtn: document.getElementById("cancel-btn"),
  stepperList: document.getElementById("stepper-list"),
  alertBanner: document.getElementById("alert-banner"),
  alertTitle: document.getElementById("alert-title"),
  alertMessage: document.getElementById("alert-message"),
  tryReplayBtn: document.getElementById("try-replay-btn"),
  idleState: document.getElementById("idle-state"),
  idleDemoBtn: document.getElementById("idle-demo-btn"),
  trialColumns: document.getElementById("trial-columns"),
  transcriptIndicator: document.getElementById("transcript-indicator"),
  transcriptCount: document.getElementById("transcript-count"),
  transcriptList: document.getElementById("transcript-list"),
  exhibitsCount: document.getElementById("exhibits-count"),
  exhibitsEmpty: document.getElementById("exhibits-empty"),
  exhibitsList: document.getElementById("exhibits-list"),
  verdictSection: document.getElementById("verdict-section"),
  toast: document.getElementById("toast"),
};

// --- 3. Utilities (XSS-safe escaping, URL validation, typing, diff, markdown) ---

/**
 * Escape HTML special characters for ALL text from SSE events to prevent XSS.
 */
function escapeHtml(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Validate a GitHub PR URL matching github.com/<owner>/<repo>/pull/<n>.
 * Returns { valid: boolean, normalized: string, message: string }.
 */
function validatePrUrl(rawInput) {
  const trimmed = String(rawInput || "").trim();
  if (!trimmed) {
    return {
      valid: false,
      normalized: "",
      message: "Enter a GitHub PR URL (github.com/owner/repo/pull/123)",
    };
  }
  const withProtocol = /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
  const pattern = /^https?:\/\/github\.com\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)\/pull\/([1-9]\d*)(?:\/.*)?$/i;
  const match = withProtocol.split("?")[0].split("#")[0].match(pattern);
  if (!match) {
    return {
      valid: false,
      normalized: trimmed,
      message: "Must match github.com/<owner>/<repo>/pull/<number>",
    };
  }
  return {
    valid: true,
    normalized: `https://github.com/${match[1]}/${match[2]}/pull/${match[3]}`,
    message: "",
  };
}

/**
 * Check if the user has requested reduced motion.
 */
function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Fast typing effect for agent card text; skippable on click.
 */
function typeText(element, rawText, intervalMs = 9) {
  const text = String(rawText || "");
  if (!element) {
    return;
  }
  if (prefersReducedMotion() || text.length === 0) {
    element.textContent = text;
    return;
  }

  let index = 0;
  const step = Math.max(2, Math.ceil(text.length / 55));
  element.textContent = "";
  element.classList.add("typing-cursor");

  const finishImmediately = () => {
    if (timerId !== null) {
      clearInterval(timerId);
      timerId = null;
    }
    element.textContent = text;
    element.classList.remove("typing-cursor");
  };

  let timerId = setInterval(() => {
    index = Math.min(text.length, index + step);
    element.textContent = text.slice(0, index);
    if (index >= text.length) {
      finishImmediately();
    }
  }, intervalMs);

  const card = element.closest(".court-card") || element;
  card.addEventListener("click", finishImmediately, { once: true });
}

/**
 * Format a unified diff string into safe, color-coded HTML lines.
 */
function formatDiffHtml(diffText) {
  const lines = String(diffText || "").split("\n");
  if (lines.length === 0 || (lines.length === 1 && !lines[0])) {
    return `<span class="diff-line diff-meta">(No diff lines)</span>`;
  }
  return lines
    .map((line) => {
      const safe = escapeHtml(line);
      if (line.startsWith("+++") || line.startsWith("---")) {
        return `<span class="diff-line diff-meta">${safe}</span>`;
      }
      if (line.startsWith("@@")) {
        return `<span class="diff-line diff-hunk">${safe}</span>`;
      }
      if (line.startsWith("+")) {
        return `<span class="diff-line diff-add">${safe}</span>`;
      }
      if (line.startsWith("-")) {
        return `<span class="diff-line diff-del">${safe}</span>`;
      }
      return `<span class="diff-line">${safe}</span>`;
    })
    .join("");
}

/**
 * Apply lightweight syntax-like styling to already-escaped Python test code.
 */
function formatPythonCodeHtml(codeText) {
  let safe = escapeHtml(codeText || "");
  safe = safe.replace(
    /\b(def|from|import|assert|with|return|raise|for|in|if|else|elif|not|and|or|True|False|None)\b/g,
    '<span class="tok-kw">$1</span>'
  );
  safe = safe.replace(/\b(test_[A-Za-z0-9_]+|pytest)\b/g, '<span class="tok-fn">$1</span>');
  return safe;
}

/**
 * Show a temporary toast notification in the bottom-right corner.
 */
function showToast(message) {
  if (!dom.toast) {
    return;
  }
  dom.toast.textContent = message;
  dom.toast.classList.remove("hidden");
  if (toastTimer) {
    clearTimeout(toastTimer);
  }
  toastTimer = setTimeout(() => {
    dom.toast.classList.add("hidden");
  }, 2200);
}

/**
 * Copy text to clipboard using Clipboard API with textarea fallback.
 */
async function copyToClipboard(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      showToast("Copied pytest exhibit to clipboard");
      return;
    }
  } catch (_) {
    // Fallback below
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
    showToast("Copied pytest exhibit to clipboard");
  } finally {
    document.body.removeChild(ta);
  }
}

/**
 * Map charge_dropped reason to the uppercase diagonal courtroom stamp text.
 */
function formatDroppedStampText(reason) {
  const r = String(reason || "").toLowerCase();
  if (r.includes("overruled")) {
    return "OVERRULED BY EVIDENCE";
  }
  if (r.includes("broken")) {
    return "BROKEN TEST DISCARDED";
  }
  if (r.includes("pre-existing") || r.includes("preexisting")) {
    return "PRE-EXISTING";
  }
  return r.toUpperCase() || "DROPPED";
}

/**
 * Generate a Markdown report from the current trial's recorded events and trigger a Blob download.
 */
function downloadMarkdownReport() {
  const started = trialState.trialStarted || {};
  const verdict = trialState.verdict || {};
  const stats = verdict.stats || {};
  const lines = [];

  lines.push(`# PR Courtroom — Official Trial Report`);
  lines.push(``);
  lines.push(`- **PR Title:** ${started.pr_title || "Unknown PR"}`);
  lines.push(`- **PR URL:** ${started.pr_url || trialState.prUrl || DEMO_PR_URL}`);
  lines.push(`- **Changed Files:** ${(started.files_changed || []).join(", ") || "N/A"}`);
  lines.push(`- **Verdict:** \`${(verdict.verdict || "N/A").toUpperCase()}\``);
  lines.push(`- **Confidence:** ${Math.round((Number(verdict.confidence) || 0) * 100)}%`);
  lines.push(``);
  lines.push(`## Judicial Ruling`);
  lines.push(`> ${verdict.ruling || ""}`);
  lines.push(``);
  lines.push(`**Summary:** ${verdict.summary || ""}`);
  lines.push(``);

  if (Array.isArray(verdict.rebuttal_rulings) && verdict.rebuttal_rulings.length > 0) {
    lines.push(`### Rebuttal Rulings`);
    for (const rr of verdict.rebuttal_rulings) {
      lines.push(
        `- **${rr.charge_id}**: ${rr.upheld ? "UPHELD (Charge Dismissed)" : "OVERRULED"} — ${rr.reason}`
      );
    }
    lines.push(``);
  }

  lines.push(`## Charges & Sandbox Exhibits`);
  for (const cid of trialState.chargeOrder) {
    const ch = trialState.chargesById[cid];
    if (!ch) continue;
    const letter = trialState.exhibitLetterByChargeId[cid] || cid;
    const droppedReason = trialState.droppedByChargeId[cid];
    const def = trialState.defensesByChargeId[cid];
    const tr = trialState.testResultsByChargeId[cid];

    lines.push(`### [${ch.id}] ${ch.title} (Exhibit ${letter})`);
    lines.push(`- **Severity:** ${ch.severity}`);
    lines.push(`- **Target Symbol:** \`${ch.target_symbol}\``);
    lines.push(`- **Intended Behavior:** ${ch.intended_behavior}`);
    lines.push(`- **Explanation:** ${ch.explanation}`);
    if (droppedReason) {
      lines.push(`- **Status:** Dropped (${formatDroppedStampText(droppedReason)})`);
    } else if (tr) {
      lines.push(`- **Latest Sandbox Status:** \`${tr.status}\` (phase: \`${tr.phase}\`)`);
    }
    if (def) {
      lines.push(`- **Defense (${def.stance.toUpperCase()}):** ${def.reasoning}`);
      if (def.quoted_intent) {
        lines.push(`  > "${def.quoted_intent}"`);
      }
    }
    lines.push(``);
    lines.push("```python");
    lines.push((ch.test_code || "").trim());
    lines.push("```");
    lines.push(``);
  }

  lines.push(`## Trial Statistics`);
  lines.push(`- **Duration:** ${stats.duration_s ?? 0}s`);
  lines.push(`- **Charges Filed:** ${stats.charges_filed ?? 0}`);
  lines.push(`- **Proven by Sandbox:** ${stats.charges_valid ?? 0}`);
  lines.push(`- **Dropped:** ${stats.charges_dropped ?? 0}`);
  lines.push(`- **Fixes Applied:** ${stats.fixes_applied ?? 0}`);
  lines.push(`- **Existing Tests Passed:** ${stats.existing_tests_passed ? "Yes" : "No"}`);
  lines.push(``);

  const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "pr-courtroom-report.md";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  showToast("Downloaded Markdown trial report");
}

// --- 4. Stepper & UI State Helpers ---

/**
 * Highlight the current stage in the 5-step progress stepper.
 */
function setStepperStage(stageName, markAllDone = false) {
  const targetIdx = STEP_ORDER.indexOf(stageName);
  const items = dom.stepperList.querySelectorAll(".step-item");
  items.forEach((item) => {
    const step = item.getAttribute("data-step");
    const idx = STEP_ORDER.indexOf(step);
    item.classList.remove("step-active", "step-done");
    if (markAllDone) {
      item.classList.add("step-done");
      if (step === "verdict") {
        item.classList.add("step-active");
      }
    } else if (idx < targetIdx) {
      item.classList.add("step-done");
    } else if (idx === targetIdx) {
      item.classList.add("step-active");
    }
  });
}

/**
 * Toggle running vs idle controls in the top bar.
 */
function setRunningState(isRunning) {
  trialState.running = isRunning;
  dom.startBtn.disabled = isRunning;
  dom.prUrlInput.disabled = isRunning;
  dom.loadDemoBtn.disabled = isRunning;
  dom.replayCheckbox.disabled = isRunning;

  if (isRunning) {
    dom.cancelBtn.classList.remove("hidden");
    dom.transcriptIndicator.classList.add("indicator-live");
    dom.transcriptIndicator.classList.remove("indicator-done");
  } else {
    dom.cancelBtn.classList.add("hidden");
    dom.transcriptIndicator.classList.remove("indicator-live");
    if (trialState.finished) {
      dom.transcriptIndicator.classList.add("indicator-done");
    }
  }
}

/**
 * Show or hide the inline error / connection-lost banner.
 */
function showBanner(title, message, showReplayBtn = true) {
  dom.alertTitle.textContent = title;
  dom.alertMessage.textContent = message;
  if (showReplayBtn) {
    dom.tryReplayBtn.classList.remove("hidden");
  } else {
    dom.tryReplayBtn.classList.add("hidden");
  }
  dom.alertBanner.classList.remove("hidden");
}

function hideBanner() {
  dom.alertBanner.classList.add("hidden");
}

/**
 * Reset the courtroom transcript, exhibits, and verdict panels for a fresh trial run.
 */
function resetCourtroomUI() {
  trialState.finished = false;
  trialState.events = [];
  trialState.trialStarted = null;
  trialState.chargesById = {};
  trialState.chargeOrder = [];
  trialState.exhibitLetterByChargeId = {};
  trialState.testResultsByChargeId = {};
  trialState.droppedByChargeId = {};
  trialState.defensesByChargeId = {};
  trialState.fixes = [];
  trialState.verdict = null;
  trialState.transcriptCount = 0;
  trialState.exhibitsCount = 0;

  dom.transcriptList.innerHTML = "";
  dom.exhibitsList.innerHTML = "";
  dom.exhibitsEmpty.classList.remove("hidden");
  dom.verdictSection.innerHTML = "";
  dom.verdictSection.classList.add("hidden");
  dom.transcriptCount.textContent = "0 entries";
  dom.exhibitsCount.textContent = "0 exhibits";
  hideBanner();
}

/**
 * Highlight the newest transcript card with its agent glow and clear previous active glow.
 */
function markActiveCard(newCardEl) {
  const prev = dom.transcriptList.querySelectorAll(".court-card.card-active");
  prev.forEach((el) => el.classList.remove("card-active"));
  if (newCardEl) {
    newCardEl.classList.add("card-active");
  }
}

function incrementTranscriptCount() {
  trialState.transcriptCount += 1;
  dom.transcriptCount.textContent =
    trialState.transcriptCount === 1
      ? "1 entry"
      : `${trialState.transcriptCount} entries`;
}

// --- 5. Render Functions per Card Type ---

/**
 * Render the "Court is in session" intro card when trial_started arrives.
 */
function renderSessionIntroCard(data) {
  const card = document.createElement("article");
  card.className = "court-card card-session";

  const files = Array.isArray(data.files_changed) ? data.files_changed : [];
  const filesPills = files
    .map((f) => `<span class="mono-pill">${escapeHtml(f)}</span>`)
    .join(" ");

  card.innerHTML = `
    <div class="card-header">
      <div class="agent-identity">
        <span class="agent-avatar avatar-clerk" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="m14 13-7.5 7.5c-.83.83-2.17.83-3 0a2.12 2.12 0 0 1 0-3L11 10" />
            <path d="m16 16 6-6" /><path d="m8 8 6-6" /><path d="m9 7 8 8" /><path d="m21 11-8-8" />
          </svg>
        </span>
        <div class="agent-meta">
          <span class="agent-name">Court Clerk</span>
          <span class="agent-role">Opening Docket</span>
        </div>
      </div>
      <div class="badge-row">
        <span class="badge stance-rebut">Court is in session</span>
      </div>
    </div>
    <h3 class="card-title">${escapeHtml(data.pr_title || "Pull Request Under Trial")}</h3>
    <p class="card-text" data-typed></p>
    <div class="card-meta-line">
      <span>Changed files (${files.length}):</span>
      ${filesPills || `<span class="mono-pill">None</span>`}
    </div>
  `;

  dom.transcriptList.appendChild(card);
  markActiveCard(card);
  incrementTranscriptCount();

  const textEl = card.querySelector("[data-typed]");
  typeText(
    textEl,
    `Court is in session. All claims regarding "${data.pr_title || "this PR"}" require executable pytest evidence in the sandbox.`
  );
}

/**
 * Render a red Prosecutor card for a charge_filed event.
 */
function renderProsecutorCard(charge) {
  const severity = ["low", "medium", "high"].includes(charge.severity)
    ? charge.severity
    : "medium";
  const card = document.createElement("article");
  card.className = "court-card card-prosecutor";
  card.id = `prosecutor-card-${escapeHtml(charge.id)}`;
  card.setAttribute("data-charge-id", charge.id);

  card.innerHTML = `
    <div class="card-header">
      <div class="agent-identity">
        <span class="agent-avatar avatar-prosecutor" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
        </span>
        <div class="agent-meta">
          <span class="agent-name">Prosecutor</span>
          <span class="agent-role">Indictment &amp; Pytest Specification</span>
        </div>
      </div>
      <div class="badge-row">
        <span class="badge badge-id">${escapeHtml(charge.id)}</span>
        <span class="badge severity-${severity}">${escapeHtml(severity)} severity</span>
      </div>
    </div>
    <h3 class="card-title">${escapeHtml(charge.title)}</h3>
    <div class="card-meta-line">
      <span>Target symbol:</span>
      <span class="mono-pill">${escapeHtml(charge.target_symbol)}</span>
    </div>
    <p class="card-text" data-typed></p>
    <blockquote class="intent-quote">
      <span class="quote-label">Basis / Intended Behavior</span>
      ${escapeHtml(charge.intended_behavior)}
    </blockquote>
  `;

  dom.transcriptList.appendChild(card);
  markActiveCard(card);
  incrementTranscriptCount();

  const textEl = card.querySelector("[data-typed]");
  typeText(textEl, charge.explanation || "");
}

/**
 * Dim a dropped charge's Prosecutor card and overlay a diagonal stamp.
 */
function applyDroppedStampToChargeCard(chargeId, reason) {
  const card = document.getElementById(`prosecutor-card-${chargeId}`);
  if (!card) {
    return;
  }
  card.classList.remove("card-active");
  card.classList.add("card-dropped");

  const existingStamp = card.querySelector(".dropped-stamp");
  if (existingStamp) {
    existingStamp.remove();
  }

  const stamp = document.createElement("div");
  stamp.className = "dropped-stamp";
  stamp.textContent = formatDroppedStampText(reason);
  card.appendChild(stamp);
}

/**
 * Render a blue Defender card for a defense event.
 */
function renderDefenderCard(defense) {
  const isRebut = defense.stance === "rebut";
  const stanceLabel = isRebut ? "Rebuttal" : "Fix";
  const stanceClass = isRebut ? "stance-rebut" : "stance-fix";

  const card = document.createElement("article");
  card.className = "court-card card-defender";

  const quoteBlock =
    isRebut && defense.quoted_intent
      ? `
      <blockquote class="intent-quote quote-rebuttal">
        <span class="quote-label">Quoted Documented Intent</span>
        "${escapeHtml(defense.quoted_intent)}"
      </blockquote>`
      : "";

  const fileMeta =
    !isRebut && defense.fixed_file_path
      ? `
      <div class="card-meta-line">
        <span>Patched file:</span>
        <span class="mono-pill">${escapeHtml(defense.fixed_file_path)}</span>
      </div>`
      : "";

  card.innerHTML = `
    <div class="card-header">
      <div class="agent-identity">
        <span class="agent-avatar avatar-defender" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            <polyline points="9 12 11 14 15 10" />
          </svg>
        </span>
        <div class="agent-meta">
          <span class="agent-name">Defender</span>
          <span class="agent-role">Defense Counsel Response</span>
        </div>
      </div>
      <div class="badge-row">
        <span class="badge badge-id">${escapeHtml(defense.charge_id)}</span>
        <span class="badge ${stanceClass}">${stanceLabel}</span>
      </div>
    </div>
    ${fileMeta}
    <p class="card-text" data-typed></p>
    ${quoteBlock}
  `;

  dom.transcriptList.appendChild(card);
  markActiveCard(card);
  incrementTranscriptCount();

  const textEl = card.querySelector("[data-typed]");
  typeText(textEl, defense.reasoning || "");
}

/**
 * Render a collapsible diff viewer card when fix_applied arrives.
 */
function renderFixAppliedCard(fixData) {
  const passed = Boolean(fixData.existing_tests_passed);
  const statusBadge = passed
    ? `<span class="badge badge-pass">Existing tests passed</span>`
    : `<span class="badge badge-fail">Existing tests regressed — reverted</span>`;

  const card = document.createElement("article");
  card.className = "court-card card-fix";

  card.innerHTML = `
    <div class="card-header">
      <div class="agent-identity">
        <span class="agent-avatar avatar-sandbox" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="16 18 22 12 16 6" />
            <polyline points="8 6 2 12 8 18" />
          </svg>
        </span>
        <div class="agent-meta">
          <span class="agent-name">Sandbox Bailiff</span>
          <span class="agent-role">Patch Verification for ${escapeHtml(fixData.charge_id)}</span>
        </div>
      </div>
      <div class="badge-row">
        <span class="badge badge-id">${escapeHtml(fixData.charge_id)}</span>
        ${statusBadge}
      </div>
    </div>
    <p class="card-text" data-typed></p>
    <details class="collapsible-block" open>
      <summary class="collapsible-summary">
        <span>Unified Diff Patch (${escapeHtml(fixData.charge_id)})</span>
        <span>Toggle diff</span>
      </summary>
      <pre class="diff-viewer">${formatDiffHtml(fixData.diff || "")}</pre>
    </details>
  `;

  dom.transcriptList.appendChild(card);
  markActiveCard(card);
  incrementTranscriptCount();

  const textEl = card.querySelector("[data-typed]");
  typeText(
    textEl,
    passed
      ? `Applied Defense patch for ${fixData.charge_id}; existing test suite passed without regressions.`
      : `Defense patch for ${fixData.charge_id} broke existing tests and was automatically reverted.`
  );
}

/**
 * Render or update an Exhibit card in the right column when test_result arrives.
 */
function renderOrUpdateExhibitCard(testResult) {
  dom.exhibitsEmpty.classList.add("hidden");

  const chargeId = testResult.charge_id;
  const charge = trialState.chargesById[chargeId] || {};
  const letter = trialState.exhibitLetterByChargeId[chargeId] || "A";
  const existingCard = document.getElementById(`exhibit-card-${chargeId}`);

  // If this is an after_fix update for an existing exhibit card, animate red -> green!
  if (existingCard && testResult.phase === "after_fix") {
    const isPass = testResult.status === "pass";
    existingCard.classList.remove("exhibit-fail", "exhibit-dropped", "exhibit-transitioning");
    if (isPass) {
      // Force reflow so the green transition animation plays cleanly
      void existingCard.offsetWidth;
      existingCard.classList.add("exhibit-pass", "exhibit-transitioning");
    } else {
      existingCard.classList.add("exhibit-fail");
    }

    const statusBadgeEl = existingCard.querySelector("[data-exhibit-status]");
    if (statusBadgeEl) {
      statusBadgeEl.className = isPass ? "badge badge-pass" : "badge badge-fail";
      statusBadgeEl.textContent = isPass ? "AFTER FIX: PASS" : `AFTER FIX: ${testResult.status}`;
    }

    const outputEl = existingCard.querySelector("[data-exhibit-output]");
    if (outputEl) {
      outputEl.textContent = testResult.output || "(No output)";
    }
    return;
  }

  // Initial exhibit creation
  const isFailValid = testResult.status === "fail_valid";
  const isPass = testResult.status === "pass";
  const cardStateClass = isFailValid
    ? "exhibit-fail"
    : isPass
    ? "exhibit-pass"
    : "exhibit-dropped";

  const codeTagLabel = testResult.new_code ? "NEW CODE" : "REGRESSION";
  const codeTagClass = testResult.new_code ? "stance-fix" : "severity-high";

  let statusLabel = "PROVEN FAIL";
  let statusClass = "badge-fail";
  if (testResult.status === "pass") {
    statusLabel = "PASSED ON HEAD";
    statusClass = "badge-pass";
  } else if (testResult.status === "fail_preexisting") {
    statusLabel = "PRE-EXISTING";
    statusClass = "severity-low";
  } else if (testResult.status === "error" || testResult.status === "timeout") {
    statusLabel = testResult.status.toUpperCase();
    statusClass = "severity-low";
  }

  const card = document.createElement("article");
  card.className = `exhibit-card ${cardStateClass}`;
  card.id = `exhibit-card-${escapeHtml(chargeId)}`;

  const testCodeRaw = charge.test_code || "# Test code unavailable";

  card.innerHTML = `
    <div class="exhibit-header">
      <div class="exhibit-label-group">
        <span class="exhibit-label">Exhibit ${escapeHtml(letter)}</span>
        <span class="badge badge-id">${escapeHtml(chargeId)}</span>
      </div>
      <div class="badge-row">
        ${
          isFailValid
            ? `<span class="badge ${codeTagClass}">${codeTagLabel}</span>`
            : ""
        }
        <span class="badge ${statusClass}" data-exhibit-status>${escapeHtml(statusLabel)}</span>
      </div>
    </div>
    <h3 class="exhibit-charge-title">${escapeHtml(charge.title || `Charge ${chargeId}`)}</h3>
    <pre class="pytest-output-box" data-exhibit-output>${escapeHtml(testResult.output || "")}</pre>
    <details class="collapsible-block">
      <summary class="collapsible-summary">
        <span>Executable Pytest Code (${escapeHtml(charge.target_symbol || chargeId)})</span>
        <span>View test</span>
      </summary>
      <div class="test-code-header">
        <span class="mono-pill">test_${escapeHtml(chargeId)}.py</span>
        <button type="button" class="btn btn-secondary btn-sm" data-copy-test>
          Copy test
        </button>
      </div>
      <pre class="code-pre">${formatPythonCodeHtml(testCodeRaw)}</pre>
    </details>
  `;

  const copyBtn = card.querySelector("[data-copy-test]");
  if (copyBtn) {
    copyBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      copyToClipboard(testCodeRaw);
    });
  }

  dom.exhibitsList.appendChild(card);
  trialState.exhibitsCount += 1;
  dom.exhibitsCount.textContent =
    trialState.exhibitsCount === 1
      ? "1 exhibit"
      : `${trialState.exhibitsCount} exhibits`;
}

/**
 * Render the full-width dramatic Verdict card at the bottom of the page.
 */
function renderVerdictCard(data) {
  // Subtle screen flash
  if (!prefersReducedMotion() && dom.screenFlash) {
    dom.screenFlash.classList.remove("flash-active");
    void dom.screenFlash.offsetWidth;
    dom.screenFlash.classList.add("flash-active");
  }

  const rawVerdict = String(data.verdict || "block").toLowerCase();
  let badgeText = "BLOCK";
  let badgeClass = "verdict-block";
  if (rawVerdict === "merge") {
    badgeText = "MERGE";
    badgeClass = "verdict-merge";
  } else if (rawVerdict === "merge_with_fixes") {
    badgeText = "MERGE WITH FIXES";
    badgeClass = "verdict-merge_with_fixes";
  }

  const confidencePct = Math.round((Number(data.confidence) || 0) * 100);
  const stats = data.stats || {};
  const rulings = Array.isArray(data.rebuttal_rulings) ? data.rebuttal_rulings : [];

  const rulingsHtml =
    rulings.length > 0
      ? `
      <div class="rebuttal-rulings-list">
        <h4 class="rebuttal-rulings-title">Judicial Rulings on Rebuttals</h4>
        ${rulings
          .map(
            (r) => `
          <div class="rebuttal-item">
            <span class="badge badge-id">${escapeHtml(r.charge_id)}</span>
            <span class="badge ${r.upheld ? "badge-pass" : "badge-fail"}">
              ${r.upheld ? "REBUTTAL UPHELD (DISMISSED)" : "REBUTTAL OVERRULED"}
            </span>
            <span>${escapeHtml(r.reason)}</span>
          </div>`
          )
          .join("")}
      </div>`
      : "";

  dom.verdictSection.innerHTML = `
    <article class="verdict-card">
      <div class="verdict-top">
        <div class="verdict-badge-group">
          <div class="gavel-strike-box" aria-hidden="true">
            <svg class="gavel-strike-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="m14 13-7.5 7.5c-.83.83-2.17.83-3 0a2.12 2.12 0 0 1 0-3L11 10" />
              <path d="m16 16 6-6" />
              <path d="m8 8 6-6" />
              <path d="m9 7 8 8" />
              <path d="m21 11-8-8" />
            </svg>
          </div>
          <div>
            <span class="verdict-eyebrow" id="verdict-heading">Final Courtroom Verdict</span>
            <span class="verdict-big-badge ${badgeClass}">${badgeText}</span>
          </div>
        </div>

        <div class="confidence-box">
          <div class="confidence-label-row">
            <span>Deterministic Confidence</span>
            <span class="confidence-pct">${confidencePct}%</span>
          </div>
          <div class="confidence-track" role="progressbar" aria-valuenow="${confidencePct}" aria-valuemin="0" aria-valuemax="100">
            <div class="confidence-fill" id="confidence-fill-bar"></div>
          </div>
        </div>
      </div>

      <blockquote class="verdict-ruling-quote">
        "${escapeHtml(data.ruling || "")}"
      </blockquote>
      <p class="verdict-summary">${escapeHtml(data.summary || "")}</p>

      ${rulingsHtml}

      <div class="verdict-stats-grid">
        <div class="stat-tile">
          <span class="stat-value">${escapeHtml(stats.duration_s ?? 0)}s</span>
          <span class="stat-label">Duration</span>
        </div>
        <div class="stat-tile">
          <span class="stat-value">${escapeHtml(stats.charges_filed ?? 0)}</span>
          <span class="stat-label">Charges Filed</span>
        </div>
        <div class="stat-tile">
          <span class="stat-value">${escapeHtml(stats.charges_valid ?? 0)}</span>
          <span class="stat-label">Proven Valid</span>
        </div>
        <div class="stat-tile">
          <span class="stat-value">${escapeHtml(stats.charges_dropped ?? 0)}</span>
          <span class="stat-label">Dropped</span>
        </div>
        <div class="stat-tile">
          <span class="stat-value">${escapeHtml(stats.fixes_applied ?? 0)}</span>
          <span class="stat-label">Fixes Applied</span>
        </div>
      </div>

      <div class="verdict-actions">
        <button type="button" id="run-again-btn" class="btn btn-secondary">
          Run again
        </button>
        <button type="button" id="download-report-btn" class="btn btn-primary">
          Download report
        </button>
      </div>
    </article>
  `;

  dom.verdictSection.classList.remove("hidden");

  // Animate the confidence bar width on next frame
  requestAnimationFrame(() => {
    const fillBar = document.getElementById("confidence-fill-bar");
    if (fillBar) {
      fillBar.style.width = `${confidencePct}%`;
    }
  });

  const runAgainBtn = document.getElementById("run-again-btn");
  if (runAgainBtn) {
    runAgainBtn.addEventListener("click", () => {
      connect();
    });
  }

  const downloadBtn = document.getElementById("download-report-btn");
  if (downloadBtn) {
    downloadBtn.addEventListener("click", () => {
      downloadMarkdownReport();
    });
  }

  dom.verdictSection.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "nearest" });
}

// --- 6. SSE Event Handlers ---

function handleTrialStarted(data) {
  trialState.trialStarted = data;
  setStepperStage("prosecution");
  renderSessionIntroCard(data);
}

function handleChargeFiled(charge) {
  setStepperStage("prosecution");
  trialState.chargesById[charge.id] = charge;
  if (!trialState.chargeOrder.includes(charge.id)) {
    const idx = trialState.chargeOrder.length;
    trialState.chargeOrder.push(charge.id);
    trialState.exhibitLetterByChargeId[charge.id] =
      EXHIBIT_LETTERS[idx] || String(idx + 1);
  }
  renderProsecutorCard(charge);
}

function handleTestResult(testResult) {
  if (testResult.phase === "initial") {
    setStepperStage("evidence");
  } else {
    setStepperStage("defense");
  }
  trialState.testResultsByChargeId[testResult.charge_id] = testResult;
  renderOrUpdateExhibitCard(testResult);
}

function handleChargeDropped(data) {
  setStepperStage("evidence");
  trialState.droppedByChargeId[data.charge_id] = data.reason;
  applyDroppedStampToChargeCard(data.charge_id, data.reason);
}

function handleDefense(defense) {
  setStepperStage("defense");
  trialState.defensesByChargeId[defense.charge_id] = defense;
  renderDefenderCard(defense);
}

function handleFixApplied(fixData) {
  setStepperStage("defense");
  trialState.fixes.push(fixData);
  renderFixAppliedCard(fixData);
}

function handleVerdict(verdictData) {
  trialState.verdict = verdictData;
  trialState.finished = true;
  closeEventSource();
  setRunningState(false);
  setStepperStage("verdict", true);
  markActiveCard(null);
  renderVerdictCard(verdictData);
}

function handleServerError(errData) {
  trialState.finished = true;
  closeEventSource();
  setRunningState(false);
  const message =
    (errData && errData.message) ||
    "The courtroom encountered an error while running this trial.";
  showBanner("Trial Halted", message, true);
}

// --- 7. Connection Management (EventSource) ---

function closeEventSource() {
  if (activeEventSource) {
    activeEventSource.close();
    activeEventSource = null;
  }
}

/**
 * Validate input, reset courtroom state, and open an EventSource stream to /api/trial.
 */
function connect() {
  closeEventSource();

  let replay = Boolean(dom.replayCheckbox.checked);
  const rawUrl = dom.prUrlInput.value.trim();

  // If replay mode is ON and URL is empty, auto-fill demo PR URL for convenience
  const effectiveRawUrl = !rawUrl && replay ? DEMO_PR_URL : rawUrl;
  if (!rawUrl && replay) {
    dom.prUrlInput.value = DEMO_PR_URL;
  }

  const check = validatePrUrl(effectiveRawUrl);
  if (!check.valid && !replay) {
    dom.prUrlInput.classList.add("input-invalid");
    dom.urlValidationMsg.textContent = check.message;
    dom.prUrlInput.focus();
    return;
  }

  dom.prUrlInput.classList.remove("input-invalid");
  dom.urlValidationMsg.textContent = "";

  const normalizedUrl = check.valid ? check.normalized : DEMO_PR_URL;
  dom.prUrlInput.value = normalizedUrl;

  // If the user entered a real (non-demo) PR URL, run it live instead of replaying demo.json
  if (
    check.valid &&
    normalizedUrl.toLowerCase() !== DEMO_PR_URL.toLowerCase() &&
    replay
  ) {
    replay = false;
    dom.replayCheckbox.checked = false;
    try {
      localStorage.setItem(REPLAY_STORAGE_KEY, "false");
    } catch (_) {
      // Ignore storage errors
    }
  }

  resetCourtroomUI();
  trialState.prUrl = normalizedUrl;
  trialState.replay = replay;

  dom.idleState.classList.add("hidden");
  dom.trialColumns.classList.remove("hidden");
  setStepperStage("fetch");
  setRunningState(true);

  const query = new URLSearchParams({
    pr_url: normalizedUrl,
    replay: replay ? "true" : "false",
  });
  const endpoint = `/api/trial?${query.toString()}`;
  const es = new EventSource(endpoint);
  activeEventSource = es;

  const bindJsonEvent = (eventName, handler) => {
    es.addEventListener(eventName, (ev) => {
      if (!ev || typeof ev.data !== "string") {
        return;
      }
      try {
        const parsed = JSON.parse(ev.data);
        trialState.events.push({ event: eventName, data: parsed });
        handler(parsed);
      } catch (_) {
        // Ignore malformed frame
      }
    });
  };

  bindJsonEvent("trial_started", handleTrialStarted);
  bindJsonEvent("charge_filed", handleChargeFiled);
  bindJsonEvent("test_result", handleTestResult);
  bindJsonEvent("charge_dropped", handleChargeDropped);
  bindJsonEvent("defense", handleDefense);
  bindJsonEvent("fix_applied", handleFixApplied);
  bindJsonEvent("verdict", handleVerdict);
  bindJsonEvent("error", handleServerError);

  // Connection lost handler — never auto-reconnect or restart the trial
  es.onerror = () => {
    if (trialState.finished) {
      closeEventSource();
      return;
    }
    closeEventSource();
    setRunningState(false);
    showBanner(
      "Connection Lost",
      "Connection to the courtroom SSE stream was interrupted. The trial was stopped to prevent duplicate runs.",
      true
    );
  };
}

// --- 8. Initialization & Event Listeners ---

function init() {
  // Restore persisted Replay Mode setting (default ON)
  try {
    const saved = localStorage.getItem(REPLAY_STORAGE_KEY);
    if (saved === "false") {
      dom.replayCheckbox.checked = false;
    } else {
      dom.replayCheckbox.checked = true;
    }
  } catch (_) {
    dom.replayCheckbox.checked = true;
  }

  dom.replayCheckbox.addEventListener("change", () => {
    try {
      localStorage.setItem(
        REPLAY_STORAGE_KEY,
        dom.replayCheckbox.checked ? "true" : "false"
      );
    } catch (_) {
      // Ignore storage errors
    }
  });

  // Live URL input validation feedback
  dom.prUrlInput.addEventListener("input", () => {
    const val = dom.prUrlInput.value.trim();
    if (!val) {
      dom.prUrlInput.classList.remove("input-invalid");
      dom.urlValidationMsg.textContent = "";
      return;
    }
    const res = validatePrUrl(val);
    if (res.valid) {
      dom.prUrlInput.classList.remove("input-invalid");
      dom.urlValidationMsg.textContent = "";
      if (res.normalized.toLowerCase() !== DEMO_PR_URL.toLowerCase()) {
        dom.replayCheckbox.checked = false;
        try {
          localStorage.setItem(REPLAY_STORAGE_KEY, "false");
        } catch (_) {
          // Ignore storage errors
        }
      }
    } else {
      dom.prUrlInput.classList.add("input-invalid");
      dom.urlValidationMsg.textContent = res.message;
    }
  });

  // Load demo PR button in top bar
  dom.loadDemoBtn.addEventListener("click", () => {
    dom.prUrlInput.value = DEMO_PR_URL;
    dom.prUrlInput.classList.remove("input-invalid");
    dom.urlValidationMsg.textContent = "";
    dom.prUrlInput.focus();
  });

  // Idle empty state button: runs entered PR URL if valid, otherwise fills demo PR and starts trial
  dom.idleDemoBtn.addEventListener("click", () => {
    const existing = dom.prUrlInput.value.trim();
    const res = validatePrUrl(existing);
    if (!existing || !res.valid) {
      dom.prUrlInput.value = DEMO_PR_URL;
    }
    dom.prUrlInput.classList.remove("input-invalid");
    dom.urlValidationMsg.textContent = "";
    connect();
  });

  // Start trial form submit
  dom.trialForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!trialState.running) {
      connect();
    }
  });

  // Cancel button
  dom.cancelBtn.addEventListener("click", () => {
    closeEventSource();
    setRunningState(false);
    showToast("Trial cancelled");
  });

  // "Try replay mode" button inside error banner
  dom.tryReplayBtn.addEventListener("click", () => {
    dom.replayCheckbox.checked = true;
    try {
      localStorage.setItem(REPLAY_STORAGE_KEY, "true");
    } catch (_) {
      // Ignore
    }
    dom.prUrlInput.value = DEMO_PR_URL;
    connect();
  });
}

document.addEventListener("DOMContentLoaded", init);
