# PR Courtroom

> *"AI reviewers give opinions. PR Courtroom gives evidence."*

PR Courtroom is an adversarial, evidence-driven AI code review tool for Python pull requests. Three Gemini agents (**Prosecutor**, **Defender**, **Judge**) put a Pull Request on trial while a local pytest sandbox executes generated tests against `BASE` and `HEAD` before any charge is allowed to stand.

## Architecture & Flow

1. **GitHub Fetch (`backend/github_client.py`)**: Fetches PR title, description, unified diff, `base` SHA, and `head` SHA, extracting both snapshots into `workdir/<run_id>/base` and `workdir/<run_id>/head`.
2. **Prosecutor (`backend/agents/prosecutor.py`)**: Inspects the diff and files up to 3 `Charge` indictments, each with a standalone `pytest` test function.
3. **Sandbox Evidence Check (`backend/sandbox.py`)**: Runs all charge tests in parallel (`asyncio.gather`) inside isolated temporary directories (`10s` timeout, stripped environment with only `PATH` and `PYTHONPATH`):
   - Passes on `HEAD` $\rightarrow$ `pass` (dropped: *overruled by evidence*)
   - Timeout / Syntax / Import / Name / Fixture error $\rightarrow$ `error` or `timeout` (dropped: *broken test*)
   - Fails on `HEAD` with `AssertionError` or runtime exception:
     - `target_symbol` absent on `BASE` (checked via Python `ast`) $\rightarrow$ `fail_valid`, `new_code=True`
     - Passes on `BASE` $\rightarrow$ `fail_valid`, `new_code=False` (regression)
     - Fails on `BASE` $\rightarrow$ `fail_preexisting` (dropped: *pre-existing*)
4. **Defender (`backend/agents/defender.py`)**: Responds to each `fail_valid` charge with either `stance="fix"` (full replacement file) or `stance="rebut"` (quoting documented intent from the PR description or docstring).
5. **Patch Verification**: Applies one combined fix per file, re-runs charge tests and existing repository tests, and rejects any fix that regresses existing tests.
6. **Judge & Deterministic Scoring (`backend/agents/judge.py`, `backend/scoring.py`)**: The Judge rules on rebuttals (`upheld` / overruled) and writes the ruling; the backend deterministically computes `verdict` (`merge`, `merge_with_fixes`, `block`) and `confidence`.
7. **Live SSE Courtroom UI (`frontend/`)**: Streams every event over Server-Sent Events (`GET /api/trial?pr_url=<url>&replay=<true|false>`).

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

## Running Tests

```bash
python -m pytest tests/ -v
```
