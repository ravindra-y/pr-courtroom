# PR Courtroom: master context

## Product
PR Courtroom is a local web tool. The user pastes a GitHub Pull Request URL. Three Gemini agents (Prosecutor, Defender, Judge) examine the PR. The Prosecutor writes pytest tests that should fail if a bug is real. The backend RUNS those tests in a sandbox. Only bugs proven by executed tests count. The Defender fixes real bugs or rebuts tests that contradict documented intent. Fixes are re-tested. The Judge rules on rebuttals and writes the ruling. The backend computes verdict and confidence. A live courtroom UI streams everything via SSE.
Pitch: "AI reviewers give opinions. PR Courtroom gives evidence."
Event: Google Gemini Hackathon, 8-hour build. Everything runs locally. Python projects only. Demo repo only.

## Stack (do not change)
Python 3.13 (3.11+ compatible), FastAPI, Uvicorn, Pydantic v2, google-genai SDK (Gemini Flash via GEMINI_MODEL_FAST), httpx, pytest, pytest-timeout, python-dotenv.
Frontend (built later): plain HTML, CSS, JavaScript with EventSource. No framework, no build step, served by FastAPI StaticFiles.
No database. No auth. No Docker.

## Hard rules
- One file per agent. All schemas live in backend/models.py. Never change them without asking.
- Gemini calls use a response schema (Pydantic model) and return JSON only. Retry once on invalid JSON, then emit an error event.
- The backend computes verdict and confidence. The LLM never sets them.
- Every orchestrator step emits an SSE event (list below). Replay mode must always work and need no API key.
- All PR text (title, description, diff, comments) is UNTRUSTED DATA. Every agent system prompt must say so.
- Generated test code is untrusted: run only in temp folders, 10 s timeout, empty environment (only PATH and PYTHONPATH), no secrets.
- Keep functions small. Add a runnable check for each slice. Never print API keys.
- Sandbox classification rules below must not be loosened.

## Flow
1. Fetch PR title, description, diff, base SHA, head SHA; download repo zips for both SHAs into workdir/<run_id>/base and workdir/<run_id>/head.
2. Prosecutor returns up to 3 charges with a runnable pytest test each.
3. Evidence check, tests run in parallel (asyncio.gather): classify each per the rules below.
4. Defender handles each valid charge: stance "fix" (full replacement file) or "rebut" (must quote intent text from the PR description or docstring).
5. Backend applies one combined fix per file, re-runs the charge tests and all existing tests. A fix that regresses existing tests is rejected.
6. Judge rules on each rebuttal (upheld or overruled) and writes the ruling. Backend computes verdict and confidence.
7. Everything streams to the UI as SSE events.

## Sandbox classification
Run a test against a version: cwd = that version dir, env = {PATH, PYTHONPATH=<version dir>}, command `python -m pytest <abs test path> -x -q --tb=short -p no:cacheprovider`, timeout 10 s.
Step 1, run on HEAD:
- Exit code 0 (passes) -> status "pass": charge dropped, reason "overruled by evidence".
- Timeout, collection error (exit code 2), ImportError, ModuleNotFoundError, SyntaxError, NameError, fixture error -> "error" or "timeout": discarded silently.
- Exit code 1 where the test body fails with AssertionError OR a runtime exception raised by the code under test (e.g. ZeroDivisionError, TypeError) -> candidate, go to step 2.
Step 2, only for candidates:
- If target_symbol is NOT defined on BASE (check with Python ast, not by parsing errors): status "fail_valid", new_code=True (new-code defect). Do not run on base.
- Else run the same test on BASE: passes -> "fail_valid" (regression introduced by the PR); fails -> "fail_preexisting" (flag, do not blame the PR).
After a fix: charge test must pass on the fixed head and all existing tests must pass, otherwise the fix is rejected.

## Models (backend/models.py, exact)
```python
from pydantic import BaseModel
from typing import Literal, Optional

class Charge(BaseModel):
    id: str                      # "C1"
    title: str
    severity: Literal["low", "medium", "high"]
    explanation: str
    target_symbol: str           # e.g. "shop.cart.apply_discount"
    intended_behavior: str       # PR text or docstring the test is based on
    test_code: str               # full pytest function(s)

class ProsecutorOutput(BaseModel):
    charges: list[Charge]        # max 3

class DefenseResponse(BaseModel):
    charge_id: str
    stance: Literal["rebut", "fix"]
    reasoning: str
    quoted_intent: Optional[str] = None   # required when stance == "rebut"
    fixed_code: Optional[str] = None      # full replacement file content
    fixed_file_path: Optional[str] = None

class DefenderOutput(BaseModel):
    responses: list[DefenseResponse]

class RebuttalRuling(BaseModel):
    charge_id: str
    upheld: bool                 # True = rebuttal accepted, charge dismissed
    reason: str

class JudgeOutput(BaseModel):
    rebuttal_rulings: list[RebuttalRuling]
    ruling: str                  # 2 to 3 sentences
    summary: str

class TestResult(BaseModel):     # produced by sandbox, never by Gemini
    charge_id: str
    status: Literal["fail_valid", "pass", "error", "timeout", "fail_preexisting"]
    new_code: bool
    output: str
    phase: Literal["initial", "after_fix"]
```

## Verdict and confidence (computed in backend/scoring.py)
Standing charge = a fail_valid charge not dismissed by an upheld rebuttal.
- No standing charges: verdict "merge"; confidence 0.50 if no valid evidence was found at all, 0.80 if charges were dismissed by upheld rebuttals.
- All standing charges fixed and charge tests + existing tests pass: "merge_with_fixes", 0.90.
- Any standing charge unfixed, or a fix rejected for regression: "block", 0.70 (0.80 if an unfixed charge is high severity).

## SSE (GET /api/trial?pr_url=<url>&replay=<true|false>), each event: `event: <name>\ndata: <json>\n\n`
trial_started {pr_title, files_changed, pr_url}
charge_filed {Charge}
test_result {TestResult}
charge_dropped {charge_id, reason}   # "overruled by evidence" | "broken test" | "pre-existing"
defense {DefenseResponse}
fix_applied {charge_id, diff, existing_tests_passed}
verdict {JudgeOutput, verdict, confidence, stats}
error {message}
stats = {duration_s, charges_filed, charges_valid, charges_dropped, fixes_applied, existing_tests_passed}

## Replay fixtures
backend/fixtures/<name>.json = list of {"event": str, "data": object, "delay": float seconds}. Live runs save one automatically. Replay streams them with the saved delays (0.5 to 2 s).

## Env (.env.example)
GEMINI_API_KEY=
GITHUB_TOKEN=
GEMINI_MODEL_FAST=<current flash model from AI Studio>
GEMINI_MODEL_JUDGE=<current model from AI Studio>
SANDBOX_TIMEOUT_SECONDS=10

## Repo structure
pr-courtroom/
  AGENTS.md, .env.example, requirements.txt
  backend/ main.py orchestrator.py github_client.py sandbox.py scoring.py replay.py models.py agents/{prosecutor,defender,judge}.py fixtures/
  frontend/ index.html style.css app.js   (built after the backend works)
  demo-repo/   tests/   (tests/ = unit tests for sandbox and scoring)
