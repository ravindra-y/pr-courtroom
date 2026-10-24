# PR Courtroom

> **"AI reviewers give opinions. PR Courtroom gives evidence."**

PR Courtroom is a local, evidence-based AI pull request trial system built with **Python 3.13, FastAPI, Google Gemini (`google-genai`), and an isolated Pytest Sandbox**. Instead of leaving subjective style comments, three specialized Gemini agents (**Prosecutor**, **Defender**, and **Judge**) put a pull request on trial:

1. **The Prosecutor** inspects the PR diff and writes up to 3 executable `pytest` tests designed to fail on real bugs.
2. **The Sandbox Bailiff** executes those tests in an isolated subprocess (`PATH` and `PYTHONPATH` only, 10s timeout) against `HEAD` and `BASE`. Claims that pass on `HEAD`, fail to collect/import, or fail on `BASE` (pre-existing) are automatically thrown out. Only bugs proven by failing tests survive.
3. **The Defender** examines each proven charge and either **fixes** the code (returning a complete replacement file) or **rebuts** the charge by quoting explicit intent from the PR description or docstrings.
4. **The Sandbox Bailiff** applies each fix, runs the repository's existing test suite to prevent regressions, and re-runs the Prosecutor's exhibit test to verify it turns green.
5. **The Judge** rules on rebuttals (`upheld` or `overruled`) and writes an authoritative courtroom opinion, while the **Court Clerk (`backend/scoring.py`)** deterministically computes the final verdict (`MERGE`, `MERGE WITH FIXES`, or `BLOCK`) and confidence score.
6. **Live Courtroom UI** streams every step in real time over Server-Sent Events (`EventSource`).

---

## Quick Setup (Windows 10/11 PowerShell with Python 3.13)

### 1. Create and Activate a Virtual Environment

Open **PowerShell** in the project root directory and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

> **If PowerShell blocks script execution (`PSSecurityException`):**  
> Run this safe, **session-only** workaround (it applies only to the current PowerShell window and does not alter your machine-wide execution policy), then activate again:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
> .\.venv\Scripts\Activate.ps1
> ```

*(On Linux/macOS shells, activate with `source .venv/bin/activate`.)*

### 2. Upgrade `pip` and Install Dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Configure `.env`

Copy `.env.example` to `.env` and fill in your keys (only required for **Live Mode**; **Replay Mode** works immediately with no API keys):

```powershell
Copy-Item .env.example .env
```

```ini
GEMINI_API_KEY=your_gemini_api_key_here
GITHUB_TOKEN=your_github_personal_access_token_here
GEMINI_MODEL_FAST=gemini-2.5-flash
GEMINI_MODEL_JUDGE=gemini-2.5-flash
SANDBOX_TIMEOUT_SECONDS=10
```

### 4. Start the Application

```powershell
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open **[http://127.0.0.1:8000](http://127.0.0.1:8000)** in your browser.

---

## Using Replay Mode (No API Key Required)

Replay Mode is **ON by default** in the top bar (and persisted in `localStorage`):

1. Open `http://127.0.0.1:8000`.
2. Click **"Load demo PR"** (or click **"Load demo PR & Start Trial"** in the center card).
3. Ensure **"Replay mode"** is toggled **ON** and click **"Start trial"**.
4. The backend streams the recorded trial from `backend/fixtures/demo.json` (with `backup_1.json` and `backup_2.json` also available via `GET /api/fixtures` or `&fixture=backup_1`) with realistic courtroom pacing (`0.5s`–`2.0s` per event) and zero network or API calls.

You can also stream directly from PowerShell:

```powershell
curl.exe -N "http://127.0.0.1:8000/api/trial?replay=true"
```

---

## Running Tests

Run the full unit and integration test suite (sandbox classification, AST symbol checks, deterministic scoring, agent retry/rate-limit handling, and SSE streaming):

```powershell
python -m pytest tests/ -v
```

---

## 2-Minute Hackathon Demo Script

### **0:00 – 0:20 | The Hook & The Problem**
- **Show:** The PR Courtroom black-theme courtroom UI at `http://127.0.0.1:8000`.
- **Say:** *"Traditional AI code reviewers flood PRs with unverified opinions and false positives. **PR Courtroom gives evidence.** In our courtroom, an AI Prosecutor isn't allowed to just claim a bug exists—it must write a `pytest` test that actually fails in an isolated sandbox."*

### **0:20 – 0:45 | Filing the Indictment & Sandbox Evidence**
- **Action:** Click **"Load demo PR"** (`https://github.com/demo/shop/pull/1` — *"Add discounts and bulk pricing"*) and click **"Start trial"**.
- **Show:** Watch the 5-stage progress stepper advance from **Fetch PR** → **Prosecution** → **Evidence**.
  - On the **left (Courtroom transcript)**, 3 red **Prosecutor** cards slide in:
    - **C1 (High):** `ZeroDivisionError` when `cart_total([])` is called on an empty cart.
    - **C2 (High):** `apply_discount` accepts `percent > 100` despite the docstring stating `0` to `100` inclusive.
    - **C3 (Low — Bait):** `apply_discount` raises `ValueError` on negative item quantities.
  - On the **right (Exhibits)**, **Exhibit A (`REGRESSION`)**, **Exhibit B (`NEW CODE`)**, and **Exhibit C (`NEW CODE`)** appear in red with real failing `pytest` tracebacks. Expand an exhibit to show the generated test and click **"Copy test"**.

### **0:45 – 1:25 | The Defense: Real Fixes vs. Documented Intent**
- **Show:** The stepper moves to **Defense**.
  - Three blue **Defender** cards appear in the transcript:
    - For **C1** and **C2**, the Defender chooses **`FIX`** and submits a complete patched `shop/cart.py`.
    - For **C3** (the negative quantity bait), the Defender chooses **`REBUTTAL`** and quotes the PR description/docstring verbatim: *"Negative quantities are rejected with ValueError; this is intentional because returns are handled by a separate refund flow."*
  - Next, two green **Sandbox Bailiff** diff cards appear showing the unified diff patch and **`EXISTING TESTS PASSED`**.
  - Watch **Exhibit A** and **Exhibit B** in the right column **animate from red to green (`AFTER FIX: PASS`)** as the sandbox re-runs the Prosecutor's tests on the patched code!

### **1:25 – 2:00 | The Gavel Strike & Deterministic Verdict**
- **Show:** The screen flashes subtly and the full-width **Verdict Card** strikes at the bottom:
  - Badge: **`MERGE WITH FIXES`** (Gold) with **Deterministic Confidence: `90%`**.
  - **Judicial Ruling:** The Judge upholds the rebuttal on **C3** (dismissing the bait charge) and confirms **C1** and **C2** are cured by verified fixes.
  - **Action:** Click **"Download report"** to export the complete Markdown trial transcript and pytest exhibits (`pr-courtroom-report.md`).
