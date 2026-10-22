(() => {
  const trialForm = document.getElementById("trialForm");
  const prUrlInput = document.getElementById("prUrlInput");
  const replayCheckbox = document.getElementById("replayCheckbox");
  const startTrialBtn = document.getElementById("startTrialBtn");
  const apiKeyIndicator = document.getElementById("apiKeyIndicator");
  const presetChips = document.querySelectorAll(".preset-chip");

  const errorBanner = document.getElementById("errorBanner");
  const errorText = document.getElementById("errorText");

  const prHeaderCard = document.getElementById("prHeaderCard");
  const prTitleDisplay = document.getElementById("prTitleDisplay");
  const prUrlDisplay = document.getElementById("prUrlDisplay");
  const filesChangedList = document.getElementById("filesChangedList");

  const prosecutorStatus = document.getElementById("prosecutorStatus");
  const defenderStatus = document.getElementById("defenderStatus");
  const judgeStatus = document.getElementById("judgeStatus");

  const verdictSection = document.getElementById("verdictSection");
  const verdictStamp = document.getElementById("verdictStamp");
  const confidenceBarFill = document.getElementById("confidenceBarFill");
  const confidenceValue = document.getElementById("confidenceValue");
  const judgeRulingText = document.getElementById("judgeRulingText");
  const judgeSummaryText = document.getElementById("judgeSummaryText");

  const statDuration = document.getElementById("statDuration");
  const statFiled = document.getElementById("statFiled");
  const statValid = document.getElementById("statValid");
  const statDropped = document.getElementById("statDropped");
  const statFixes = document.getElementById("statFixes");
  const statExisting = document.getElementById("statExisting");

  const chargesContainer = document.getElementById("chargesContainer");
  const chargesCounter = document.getElementById("chargesCounter");
  const sseTranscript = document.getElementById("sseTranscript");
  const eventCountPill = document.getElementById("eventCountPill");

  const stepIds = [
    "step-fetch",
    "step-prosecutor",
    "step-sandbox",
    "step-defender",
    "step-verdict",
  ];

  let activeSource = null;
  let eventCount = 0;
  let chargesMap = new Map();

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatDiffHtml(diffText) {
    if (!diffText) return "";
    return diffText
      .split("\n")
      .map((line) => {
        const safe = escapeHtml(line);
        if (line.startsWith("+") && !line.startsWith("+++")) {
          return `<span class="diff-line-add">${safe}</span>`;
        }
        if (line.startsWith("-") && !line.startsWith("---")) {
          return `<span class="diff-line-del">${safe}</span>`;
        }
        return safe;
      })
      .join("\n");
  }

  function setStepState(activeStepId, markAllDone = false) {
    let passedActive = false;
    for (const id of stepIds) {
      const el = document.getElementById(id);
      if (!el) continue;
      el.classList.remove("active", "done");
      if (markAllDone) {
        el.classList.add("done");
      } else if (id === activeStepId) {
        el.classList.add("active");
        passedActive = true;
      } else if (!passedActive) {
        el.classList.add("done");
      }
    }
  }

  function resetCourtroomUI() {
    if (activeSource) {
      activeSource.close();
      activeSource = null;
    }
    eventCount = 0;
    chargesMap.clear();
    eventCountPill.textContent = "0 events";
    chargesCounter.textContent = "0 charges";
    sseTranscript.innerHTML = "";
    chargesContainer.innerHTML = "";

    errorBanner.classList.add("hidden");
    prHeaderCard.classList.add("hidden");
    verdictSection.classList.add("hidden");
    verdictSection.className = "verdict-panel hidden";

    prosecutorStatus.textContent = "Inspecting PR diff and preparing charges…";
    defenderStatus.textContent = "Waiting for sandbox evidence results…";
    judgeStatus.textContent = "Presiding over trial…";

    for (const id of stepIds) {
      const el = document.getElementById(id);
      if (el) el.classList.remove("active", "done");
    }
    setStepState("step-fetch");
  }

  function logSseEvent(eventName, data, summaryText) {
    eventCount += 1;
    eventCountPill.textContent = `${eventCount} event${eventCount === 1 ? "" : "s"}`;

    const now = new Date().toLocaleTimeString([], {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });

    const item = document.createElement("div");
    item.className = "sse-item";
    item.innerHTML = `
      <div class="sse-item-top">
        <span class="sse-event-name">${escapeHtml(eventName)}</span>
        <span class="sse-time">${escapeHtml(now)}</span>
      </div>
      <div class="sse-summary">${escapeHtml(summaryText)}</div>
    `;
    sseTranscript.prepend(item);
  }

  function renderChargeCard(charge) {
    chargesMap.set(charge.id, charge);
    chargesCounter.textContent = `${chargesMap.size} charge${chargesMap.size === 1 ? "" : "s"}`;

    const card = document.createElement("article");
    card.className = "charge-card";
    card.id = `charge-card-${charge.id}`;
    card.innerHTML = `
      <div class="charge-card-header">
        <div class="charge-title-group">
          <span class="charge-id">${escapeHtml(charge.id)}</span>
          <span class="sev-badge sev-${escapeHtml(charge.severity)}">${escapeHtml(charge.severity)}</span>
          <span class="charge-title">${escapeHtml(charge.title)}</span>
        </div>
        <span id="charge-status-${charge.id}" class="charge-status-badge">FILED &middot; PENDING SANDBOX</span>
      </div>
      <div class="charge-card-body">
        <div class="meta-row">
          <span>Target Symbol: <code>${escapeHtml(charge.target_symbol)}</code></span>
          <span>Basis: <em>${escapeHtml(charge.intended_behavior)}</em></span>
        </div>
        <p class="charge-explanation">${escapeHtml(charge.explanation)}</p>
        <details class="code-details">
          <summary>Prosecutor's Generated Pytest Evidence (${escapeHtml(charge.id)})</summary>
          <pre class="code-block">${escapeHtml(charge.test_code)}</pre>
        </details>
        <div id="evidence-slot-${charge.id}"></div>
        <div id="defense-slot-${charge.id}"></div>
        <div id="ruling-slot-${charge.id}"></div>
      </div>
    `;
    chargesContainer.appendChild(card);
  }

  function updateWithTestResult(tr) {
    const statusBadge = document.getElementById(`charge-status-${tr.charge_id}`);
    const slot = document.getElementById(`evidence-slot-${tr.charge_id}`);
    if (!slot) return;

    if (tr.phase === "initial") {
      let badgeLabel = tr.status.toUpperCase();
      let originLabel = tr.new_code
        ? "New-Code Defect (AST confirmed absent on BASE)"
        : "Regression Check vs BASE";

      if (tr.status === "fail_valid" && statusBadge) {
        statusBadge.textContent = tr.new_code
          ? "PROVEN IN SANDBOX (NEW CODE)"
          : "PROVEN IN SANDBOX (REGRESSION)";
        statusBadge.className = "charge-status-badge status-valid";
      }

      slot.innerHTML = `
        <div class="sub-box sub-box-evidence">
          <div class="sub-box-title">
            <span>Sandbox Execution &middot; Initial Phase (${escapeHtml(badgeLabel)})</span>
            <span>${escapeHtml(originLabel)}</span>
          </div>
          <details class="code-details">
            <summary>Pytest Sandbox Output (${escapeHtml(tr.status)})</summary>
            <pre class="code-block">${escapeHtml(tr.output)}</pre>
          </details>
        </div>
      `;
    } else if (tr.phase === "after_fix") {
      const defSlot = document.getElementById(`defense-slot-${tr.charge_id}`);
      const retestEl = document.createElement("div");
      retestEl.className = "sub-box sub-box-evidence";
      retestEl.innerHTML = `
        <div class="sub-box-title">
          <span>Post-Fix Sandbox Verification</span>
          <span>Status: ${escapeHtml(tr.status.toUpperCase())}</span>
        </div>
        <details class="code-details">
          <summary>Post-Fix Pytest Output</summary>
          <pre class="code-block">${escapeHtml(tr.output)}</pre>
        </details>
      `;
      if (defSlot) {
        defSlot.appendChild(retestEl);
      } else {
        slot.appendChild(retestEl);
      }
    }
  }

  function updateWithChargeDropped(payload) {
    const statusBadge = document.getElementById(`charge-status-${payload.charge_id}`);
    if (statusBadge) {
      statusBadge.textContent = `DROPPED: ${String(payload.reason).toUpperCase()}`;
      statusBadge.className = "charge-status-badge status-dropped";
    }
  }

  function updateWithDefense(def) {
    const slot = document.getElementById(`defense-slot-${def.charge_id}`);
    if (!slot) return;

    const isRebut = def.stance === "rebut";
    const quoteHtml =
      isRebut && def.quoted_intent
        ? `<div class="quote-intent">&ldquo;${escapeHtml(def.quoted_intent)}&rdquo;</div>`
        : "";
    const fileHtml =
      !isRebut && def.fixed_file_path
        ? `<div class="meta-row"><span>Patched File: <code>${escapeHtml(def.fixed_file_path)}</code></span></div>`
        : "";

    slot.innerHTML = `
      <div class="sub-box sub-box-defense">
        <div class="sub-box-title">
          <span>Defender Response &middot; Stance: ${escapeHtml(def.stance.toUpperCase())}</span>
        </div>
        <p class="charge-explanation">${escapeHtml(def.reasoning)}</p>
        ${quoteHtml}
        ${fileHtml}
        <div id="diff-slot-${def.charge_id}"></div>
      </div>
    `;
  }

  function updateWithFixApplied(payload) {
    const statusBadge = document.getElementById(`charge-status-${payload.charge_id}`);
    if (statusBadge) {
      if (payload.existing_tests_passed) {
        statusBadge.textContent = "FIXED & VERIFIED IN SANDBOX";
        statusBadge.className = "charge-status-badge status-fixed";
      } else {
        statusBadge.textContent = "FIX REJECTED (REGRESSION)";
        statusBadge.className = "charge-status-badge status-valid";
      }
    }

    const diffSlot = document.getElementById(`diff-slot-${payload.charge_id}`);
    if (diffSlot && payload.diff) {
      diffSlot.innerHTML = `
        <details class="code-details" open>
          <summary>Applied Unified Diff &middot; Existing Suite: ${
            payload.existing_tests_passed ? "PASSED" : "FAILED"
          }</summary>
          <pre class="code-block">${formatDiffHtml(payload.diff)}</pre>
        </details>
      `;
    }
  }

  function updateWithVerdict(data) {
    setStepState("step-verdict", true);
    verdictSection.classList.remove("hidden");
    verdictSection.classList.add(`verdict-${data.verdict}`);

    const labelMap = {
      merge: "MERGE APPROVED",
      merge_with_fixes: "MERGE WITH FIXES",
      block: "BLOCK PULL REQUEST",
    };
    verdictStamp.textContent = labelMap[data.verdict] || data.verdict;
    verdictStamp.className = `verdict-stamp stamp-${data.verdict}`;

    const pct = Math.round((Number(data.confidence) || 0) * 100);
    confidenceBarFill.style.width = `${pct}%`;
    confidenceValue.textContent = `${pct}%`;

    judgeRulingText.textContent = data.ruling || "";
    judgeSummaryText.textContent = data.summary || "";

    const st = data.stats || {};
    statDuration.textContent = st.duration_s != null ? `${st.duration_s}s` : "—";
    statFiled.textContent = st.charges_filed ?? 0;
    statValid.textContent = st.charges_valid ?? 0;
    statDropped.textContent = st.charges_dropped ?? 0;
    statFixes.textContent = st.fixes_applied ?? 0;
    statExisting.textContent = st.existing_tests_passed ? "PASS" : "FAIL";

    if (Array.isArray(data.rebuttal_rulings)) {
      for (const rr of data.rebuttal_rulings) {
        const rSlot = document.getElementById(`ruling-slot-${rr.charge_id}`);
        const statusBadge = document.getElementById(`charge-status-${rr.charge_id}`);
        if (statusBadge) {
          if (rr.upheld) {
            statusBadge.textContent = "DISMISSED (REBUTTAL UPHELD)";
            statusBadge.className = "charge-status-badge status-dismissed";
          } else {
            statusBadge.textContent = "STANDING (REBUTTAL OVERRULED)";
            statusBadge.className = "charge-status-badge status-valid";
          }
        }
        if (rSlot) {
          rSlot.innerHTML = `
            <div class="sub-box sub-box-ruling">
              <div class="sub-box-title">
                <span>Judicial Rebuttal Ruling</span>
                <span>${rr.upheld ? "UPHELD (CHARGE DISMISSED)" : "OVERRULED"}</span>
              </div>
              <p class="charge-explanation">${escapeHtml(rr.reason)}</p>
            </div>
          `;
        }
      }
    }

    judgeStatus.textContent = `Verdict rendered: ${labelMap[data.verdict] || data.verdict} (${pct}% confidence)`;
  }

  function startTrial(prUrl, replay) {
    resetCourtroomUI();
    startTrialBtn.disabled = true;
    startTrialBtn.textContent = "Trial in Session…";

    const params = new URLSearchParams({
      pr_url: prUrl,
      replay: replay ? "true" : "false",
    });
    const source = new EventSource(`/api/trial?${params.toString()}`);
    activeSource = source;

    const finishStream = () => {
      source.close();
      if (activeSource === source) {
        activeSource = null;
      }
      startTrialBtn.disabled = false;
      startTrialBtn.textContent = "Convene Trial";
    };

    source.addEventListener("trial_started", (e) => {
      const data = JSON.parse(e.data);
      setStepState("step-prosecutor");
      prHeaderCard.classList.remove("hidden");
      prTitleDisplay.textContent = data.pr_title || "Pull Request";
      prUrlDisplay.textContent = data.pr_url || prUrl;
      prUrlDisplay.href = data.pr_url || prUrl;
      filesChangedList.innerHTML = (data.files_changed || [])
        .map((f) => `<span class="file-chip">${escapeHtml(f)}</span>`)
        .join("");
      logSseEvent("trial_started", data, `Docket opened: ${data.pr_title}`);
    });

    source.addEventListener("charge_filed", (e) => {
      const data = JSON.parse(e.data);
      setStepState("step-prosecutor");
      renderChargeCard(data);
      prosecutorStatus.textContent = `Filed ${chargesMap.size} charge(s) with executable pytest evidence.`;
      logSseEvent("charge_filed", data, `[${data.id}] (${data.severity}) ${data.title}`);
    });

    source.addEventListener("test_result", (e) => {
      const data = JSON.parse(e.data);
      if (data.phase === "initial") {
        setStepState("step-sandbox");
      }
      updateWithTestResult(data);
      logSseEvent(
        "test_result",
        data,
        `[${data.charge_id}] phase=${data.phase} -> ${data.status} (new_code=${data.new_code})`
      );
    });

    source.addEventListener("charge_dropped", (e) => {
      const data = JSON.parse(e.data);
      updateWithChargeDropped(data);
      logSseEvent("charge_dropped", data, `[${data.charge_id}] dropped (${data.reason})`);
    });

    source.addEventListener("defense", (e) => {
      const data = JSON.parse(e.data);
      setStepState("step-defender");
      updateWithDefense(data);
      defenderStatus.textContent = `Responded to ${data.charge_id} with stance "${data.stance.toUpperCase()}".`;
      logSseEvent("defense", data, `[${data.charge_id}] stance=${data.stance}`);
    });

    source.addEventListener("fix_applied", (e) => {
      const data = JSON.parse(e.data);
      setStepState("step-defender");
      updateWithFixApplied(data);
      logSseEvent(
        "fix_applied",
        data,
        `[${data.charge_id}] patch applied (existing_tests_passed=${data.existing_tests_passed})`
      );
    });

    source.addEventListener("verdict", (e) => {
      const data = JSON.parse(e.data);
      updateWithVerdict(data);
      logSseEvent(
        "verdict",
        data,
        `Verdict=${data.verdict} | Confidence=${Math.round((data.confidence || 0) * 100)}%`
      );
      finishStream();
    });

    source.addEventListener("error", (e) => {
      if (e.data) {
        try {
          const data = JSON.parse(e.data);
          errorBanner.classList.remove("hidden");
          errorText.textContent = data.message || "Unknown trial error.";
          logSseEvent("error", data, data.message || "Error");
        } catch (_) {}
      }
      finishStream();
    });
  }

  trialForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const url = prUrlInput.value.trim();
    if (!url) return;
    startTrial(url, replayCheckbox.checked);
  });

  presetChips.forEach((chip) => {
    chip.addEventListener("click", () => {
      presetChips.forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      const url = chip.getAttribute("data-url");
      if (url) {
        prUrlInput.value = url;
        startTrial(url, replayCheckbox.checked);
      }
    });
  });

  async function initEnvironmentStatus() {
    try {
      const res = await fetch("/api/fixtures");
      const info = await res.json();
      if (info.has_gemini_key) {
        apiKeyIndicator.textContent = "Gemini API Key Active · Live & Replay Ready";
        apiKeyIndicator.className = "status-pill status-live";
      } else {
        apiKeyIndicator.textContent = "Replay Mode Ready (No API Key Needed)";
        apiKeyIndicator.className = "status-pill status-replay";
        replayCheckbox.checked = true;
      }
    } catch (_) {
      apiKeyIndicator.textContent = "Replay Mode Ready";
      apiKeyIndicator.className = "status-pill status-replay";
    }
    // Automatically run the initial demo replay so the courtroom is alive on load
    startTrial(prUrlInput.value.trim(), replayCheckbox.checked);
  }

  initEnvironmentStatus();
})();
