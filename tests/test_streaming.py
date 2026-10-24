import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from backend.agents.gemini_client import generate_structured, redact_secrets
from backend.github_client import GitHubClientError, WORKDIR_ROOT
from backend.main import app
from backend.models import (
    Charge,
    DefenderOutput,
    DefenseResponse,
    JudgeOutput,
    ProsecutorOutput,
    RebuttalRuling,
    TestResult,
)
from backend.orchestrator import run_trial
from backend.replay import FIXTURES_DIR, list_fixtures, replay_trial
from backend.sandbox import run_test

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_replay_demo_fixture() -> None:
    async def _run() -> None:
        assert "demo" in list_fixtures()
        events = []
        async for ev_name, ev_data in replay_trial("demo", delay_scale=0.0):
            events.append((ev_name, ev_data))

        names = [n for n, _ in events]
        assert names[0] == "trial_started"
        assert names.count("charge_filed") == 3
        assert names.count("defense") == 3
        assert names.count("fix_applied") == 2
        assert names[-1] == "verdict"

        for name, data in events:
            if name == "charge_filed":
                Charge.model_validate(data)
            elif name == "test_result":
                TestResult.model_validate(data)
            elif name == "defense":
                DefenseResponse.model_validate(data)
            elif name == "verdict":
                JudgeOutput.model_validate(data)

        verdict_data = events[-1][1]
        assert verdict_data["verdict"] == "merge_with_fixes"
        assert verdict_data["confidence"] == 0.9
        assert verdict_data["rebuttal_rulings"][0]["charge_id"] == "C3"
        assert verdict_data["rebuttal_rulings"][0]["upheld"] is True

    asyncio.run(_run())


def test_run_trial_event_sequence_and_fixture_saving() -> None:
    async def _run() -> None:
        mock_prosecutor_out = ProsecutorOutput(
            charges=[
                Charge(
                    id="C1",
                    title="ZeroDivisionError on empty cart",
                    severity="high",
                    explanation="cart_total([]) divides by zero",
                    target_symbol="shop.cart.cart_total",
                    intended_behavior="Empty cart returns 0",
                    test_code=(
                        "from shop.cart import cart_total\n"
                        "def test_empty_cart():\n"
                        "    assert cart_total([]) == 0\n"
                    ),
                ),
                Charge(
                    id="C2",
                    title="apply_discount accepts percent > 100",
                    severity="high",
                    explanation="No validation for percent > 100",
                    target_symbol="shop.cart.apply_discount",
                    intended_behavior="percent must be between 0 and 100 inclusive.",
                    test_code=(
                        "import pytest\n"
                        "from shop.cart import apply_discount\n"
                        "def test_percent_over_100():\n"
                        "    with pytest.raises(ValueError):\n"
                        "        apply_discount([{'price': 50, 'qty': 1}], 150)\n"
                    ),
                ),
                Charge(
                    id="C3",
                    title="apply_discount rejects negative qty",
                    severity="low",
                    explanation="Raises ValueError on negative qty",
                    target_symbol="shop.cart.apply_discount",
                    intended_behavior="implied by function name",
                    test_code=(
                        "from shop.cart import apply_discount\n"
                        "def test_negative_qty():\n"
                        "    assert apply_discount([{'price': 20, 'qty': 2}, {'price': 10, 'qty': -1}], 10) == 27.0\n"
                    ),
                ),
            ]
        )

        fixed_cart = (
            "def cart_total(items):\n"
            "    if not items:\n"
            "        return 0\n"
            "    subtotal = sum(item['price'] * item['qty'] for item in items)\n"
            "    avg = subtotal / len(items)\n"
            "    if avg > 100:\n"
            "        return subtotal * 0.95\n"
            "    return subtotal\n\n\n"
            "def apply_discount(items, percent):\n"
            '    """Return the cart total after a percentage discount. percent must be between 0 and 100 inclusive. Negative quantities are rejected with ValueError; this is intentional because returns are handled by a separate refund flow."""\n'
            "    if percent < 0 or percent > 100:\n"
            "        raise ValueError('percent must be between 0 and 100 inclusive')\n"
            "    for item in items:\n"
            "        if item['qty'] < 0:\n"
            "            raise ValueError('Negative quantities are not allowed')\n"
            "    total = cart_total(items)\n"
            "    return total - total * percent / 100\n"
        )

        mock_defender_out = DefenderOutput(
            responses=[
                DefenseResponse(
                    charge_id="C1",
                    stance="fix",
                    reasoning="Return 0 when items is empty.",
                    fixed_file_path="shop/cart.py",
                    fixed_code=fixed_cart,
                ),
                DefenseResponse(
                    charge_id="C2",
                    stance="fix",
                    reasoning="Raise ValueError when percent < 0 or percent > 100.",
                    fixed_file_path="shop/cart.py",
                    fixed_code=fixed_cart,
                ),
                DefenseResponse(
                    charge_id="C3",
                    stance="rebut",
                    reasoning="Docstring states negative quantities are rejected by design.",
                    quoted_intent="Negative quantities are rejected with ValueError; this is intentional",
                ),
            ]
        )

        mock_judge_out = JudgeOutput(
            rebuttal_rulings=[
                RebuttalRuling(
                    charge_id="C3",
                    upheld=True,
                    reason="Docstring explicitly states negative quantities raise ValueError by design.",
                )
            ],
            ruling="C1 and C2 were fixed and verified; C3 was dismissed on rebuttal.",
            summary="Merge with fixes (0.90).",
        )

        async def fake_prosecute(pr: dict, head_dir: str) -> ProsecutorOutput:
            return mock_prosecutor_out

        async def fake_defend(pr: dict, valid_charges: list, test_results: dict, head_dir: str) -> DefenderOutput:
            return mock_defender_out

        async def fake_judge(pr: dict, transcript: dict, computed_context: dict) -> JudgeOutput:
            return mock_judge_out

        run_id = "test_stream_run"
        saved_fixture = FIXTURES_DIR / f"{run_id}.json"
        try:
            with (
                patch("backend.orchestrator.prosecute", side_effect=fake_prosecute),
                patch("backend.orchestrator.defend", side_effect=fake_defend),
                patch("backend.orchestrator.judge", side_effect=fake_judge),
            ):
                events = []
                async for ev_name, ev_data in run_trial("https://github.com/demo/shop/pull/1", run_id=run_id):
                    events.append((ev_name, ev_data))

            event_names = [n for n, _ in events]
            assert event_names == [
                "trial_started",
                "charge_filed",
                "charge_filed",
                "charge_filed",
                "test_result",
                "test_result",
                "test_result",
                "defense",
                "defense",
                "defense",
                "fix_applied",
                "test_result",
                "fix_applied",
                "test_result",
                "verdict",
            ]
            assert events[-1][1]["verdict"] == "merge_with_fixes"
            assert events[-1][1]["confidence"] == 0.9
            assert not (WORKDIR_ROOT / run_id).exists()
            assert saved_fixture.is_file()
        finally:
            if saved_fixture.exists():
                saved_fixture.unlink()

    asyncio.run(_run())


def test_edge_cases_pr_not_found_and_github_rate_limit() -> None:
    async def _run() -> None:
        with patch(
            "backend.orchestrator.prepare_workspace",
            new=AsyncMock(
                side_effect=GitHubClientError(
                    "PR owner/repo#999 not found (404). The pull request or repository may not exist or may be private."
                )
            ),
        ):
            events_404 = [
                ev async for ev in run_trial("https://github.com/owner/repo/pull/999", run_id="t404")
            ]
        assert events_404 == [
            (
                "error",
                {
                    "message": "PR owner/repo#999 not found (404). The pull request or repository may not exist or may be private."
                },
            )
        ]

        with patch(
            "backend.orchestrator.prepare_workspace",
            new=AsyncMock(
                side_effect=GitHubClientError(
                    "GitHub API rate limit exceeded or access forbidden (403) while fetching PR owner/repo#1."
                )
            ),
        ):
            events_rl = [
                ev async for ev in run_trial("https://github.com/owner/repo/pull/1", run_id="trl")
            ]
        assert events_rl[0][0] == "error"
        assert "rate limit exceeded" in events_rl[0][1]["message"]

    asyncio.run(_run())


def test_edge_case_gemini_rate_limit_retries_once_after_short_wait(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_RATE_LIMIT_WAIT_SECONDS", "0.01")

    call_count = 0

    async def flaky_429(*, model, contents, config):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED: Quota exceeded")
        resp = MagicMock()
        resp.text = '{"charges": []}'
        return resp

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=flaky_429)

    with patch("backend.agents.gemini_client.get_gemini_client", return_value=fake_client):
        result = asyncio.run(
            generate_structured(
                model="gemini-2.5-flash",
                system_instruction="Sys",
                user_content="User",
                schema=ProsecutorOutput,
            )
        )
    assert isinstance(result, ProsecutorOutput)
    assert call_count == 2


def test_edge_case_all_charges_broken_emits_merge_050() -> None:
    async def _run() -> None:
        broken_prosecutor_out = ProsecutorOutput(
            charges=[
                Charge(
                    id="C1",
                    title="Broken syntax test",
                    severity="medium",
                    explanation="Broken",
                    target_symbol="shop.cart.cart_total",
                    intended_behavior="Intent",
                    test_code="def test_broken(:\n    pass\n",
                ),
                Charge(
                    id="C2",
                    title="Missing import test",
                    severity="low",
                    explanation="Broken import",
                    target_symbol="shop.cart.apply_discount",
                    intended_behavior="Intent",
                    test_code="import nonexistent_module_xyz\ndef test_m(): assert True\n",
                ),
            ]
        )
        run_id = "test_all_broken"
        saved_fixture = FIXTURES_DIR / f"{run_id}.json"
        try:
            with patch(
                "backend.orchestrator.prosecute",
                new=AsyncMock(return_value=broken_prosecutor_out),
            ):
                events = [
                    ev
                    async for ev in run_trial(
                        "https://github.com/demo/shop/pull/1", run_id=run_id
                    )
                ]
            names = [n for n, _ in events]
            assert names.count("charge_dropped") == 2
            assert names[-1] == "verdict"
            verdict_ev = events[-1][1]
            assert verdict_ev["verdict"] == "merge"
            assert verdict_ev["confidence"] == 0.50
            assert "No charges were proven" in verdict_ev["ruling"]
        finally:
            if saved_fixture.exists():
                saved_fixture.unlink()

    asyncio.run(_run())


def test_edge_case_pr_with_no_python_files() -> None:
    async def _run() -> None:
        fake_ws = {
            "title": "Update README only",
            "body": "Docs change",
            "base_sha": "a",
            "head_sha": "b",
            "changed_files": ["README.md", "docs/guide.txt"],
            "diff": "",
            "base_dir": "/tmp/base",
            "head_dir": "/tmp/head",
        }
        with patch(
            "backend.orchestrator.prepare_workspace",
            new=AsyncMock(return_value=fake_ws),
        ):
            events = [
                ev
                async for ev in run_trial(
                    "https://github.com/owner/repo/pull/2", run_id="test_no_py"
                )
            ]
        assert len(events) == 1
        assert events[0][0] == "error"
        assert "does not modify any Python (.py) files" in events[0][1]["message"]

    asyncio.run(_run())


def test_sandbox_env_contains_only_path_and_pythonpath_and_no_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSecretKey123456789")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SecretToken123456789")

    test_file = tmp_path / "test_env_check.py"
    test_file.write_text(
        "import os\n"
        "def test_env_isolated():\n"
        "    assert 'GEMINI_API_KEY' not in os.environ\n"
        "    assert 'GITHUB_TOKEN' not in os.environ\n",
        encoding="utf-8",
    )

    captured_env: dict[str, str] = {}
    import subprocess as sp

    real_run = sp.run

    def spy_run(*args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return real_run(*args, **kwargs)

    with patch("backend.sandbox.subprocess.run", side_effect=spy_run):
        res = run_test(tmp_path, test_file)

    assert res["returncode"] == 0
    assert set(captured_env.keys()) == {"PATH", "PYTHONPATH"}

    leaked = "Error with AIzaSecretKey123456789 and ghp_SecretToken123456789"
    cleaned = redact_secrets(leaked)
    assert "AIzaSecretKey123456789" not in cleaned
    assert "ghp_SecretToken123456789" not in cleaned
    assert "[REDACTED]" in cleaned


def test_fastapi_sse_endpoints() -> None:
    client = TestClient(app)

    res_fx = client.get("/api/fixtures")
    assert res_fx.status_code == 200
    assert "demo" in res_fx.json()["fixtures"]

    res_err = client.get("/api/trial?pr_url=not-a-valid-url&replay=false")
    assert res_err.status_code == 200
    assert "event: error" in res_err.text
    assert "Invalid GitHub Pull Request URL" in res_err.text

    res_index = client.get("/")
    assert res_index.status_code == 200
    assert "PR Courtroom" in res_index.text
    assert "AI reviewers give opinions. PR Courtroom gives evidence." in res_index.text
    assert "onclick=" not in res_index.text.lower()

    res_css = client.get("/style.css")
    assert res_css.status_code == 200
    assert "--prosecutor: #ef4444" in res_css.text

    res_js = client.get("/app.js")
    assert res_js.status_code == 200
    assert "new EventSource" in res_js.text
    assert "escapeHtml" in res_js.text
