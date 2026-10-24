import asyncio
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.github_client import WORKDIR_ROOT
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

        # Validate every model payload against backend/models.py schemas
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
            # Verify workdir/<run_id> was cleaned up
            assert not (WORKDIR_ROOT / run_id).exists()
            # Verify fixture was saved with clamped delays 0.5..2.0
            assert saved_fixture.is_file()
        finally:
            if saved_fixture.exists():
                saved_fixture.unlink()

    asyncio.run(_run())


def test_fastapi_sse_endpoints() -> None:
    client = TestClient(app)

    res_fx = client.get("/api/fixtures")
    assert res_fx.status_code == 200
    assert "demo" in res_fx.json()["fixtures"]

    res_err = client.get("/api/trial?pr_url=not-a-valid-url&replay=false")
    assert res_err.status_code == 200
    assert "event: error" in res_err.text
    assert "Invalid GitHub Pull Request URL" in res_err.text

    # Verify static frontend files are served at /
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

