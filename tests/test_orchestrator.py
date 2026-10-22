import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from backend.models import (
    Charge,
    DefenderOutput,
    DefenseResponse,
    JudgeOutput,
    ProsecutorOutput,
    RebuttalRuling,
)
from backend.orchestrator import combine_file_fixes, run_trial_stream


def _parse_sse_frames(frames: list[str]) -> list[tuple[str, dict]]:
    parsed = []
    for frame in frames:
        lines = [line for line in frame.strip().splitlines() if line]
        event_name = ""
        data_dict = {}
        for line in lines:
            if line.startswith("event: "):
                event_name = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data_dict = json.loads(line[len("data: ") :])
        if event_name:
            parsed.append((event_name, data_dict))
    return parsed


@pytest.mark.asyncio
async def test_replay_mode_needs_no_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with patch("backend.orchestrator.stream_replay_events") as mock_stream:
        # Use real stream_replay_events with delay_scale=0.0 for fast test
        from backend.replay import stream_replay_events as real_stream

        mock_stream.side_effect = lambda pr_url=None: real_stream(
            pr_url=pr_url, delay_scale=0.0
        )
        frames = [
            f
            async for f in run_trial_stream(
                "https://github.com/demo/shop-cart/pull/42", replay=True
            )
        ]

    events = _parse_sse_frames(frames)
    event_names = [e[0] for e in events]
    assert "trial_started" in event_names
    assert "charge_filed" in event_names
    assert "test_result" in event_names
    assert "charge_dropped" in event_names
    assert "defense" in event_names
    assert "fix_applied" in event_names
    assert event_names[-1] == "verdict"
    verdict_data = events[-1][1]
    assert verdict_data["verdict"] == "merge_with_fixes"
    assert verdict_data["confidence"] == 0.90


@pytest.mark.asyncio
async def test_live_orchestrator_end_to_end_with_sandbox(tmp_path: Path):
    prosecutor_mock = AsyncMock(
        return_value=ProsecutorOutput(
            charges=[
                Charge(
                    id="C1",
                    title="FLAT50 returns negative total on $30 cart",
                    severity="high",
                    explanation="Subtracts 50 without clamping at 0.0",
                    target_symbol="shop.cart.apply_discount",
                    intended_behavior="Total after discount cannot be negative (minimum 0.0).",
                    test_code=(
                        "from shop.cart import apply_discount\n"
                        "def test_flat50_clamp():\n"
                        "    assert apply_discount(30.0, 'FLAT50') == 0.0\n"
                    ),
                ),
                Charge(
                    id="C2",
                    title="VIP discount is compound",
                    severity="medium",
                    explanation="Returns 76.0 instead of 75.0",
                    target_symbol="shop.cart.apply_discount",
                    intended_behavior="Additive discount",
                    test_code=(
                        "from shop.cart import apply_discount\n"
                        "def test_vip_additive():\n"
                        "    assert apply_discount(100.0, 'SAVE20', is_vip=True) == 75.0\n"
                    ),
                ),
            ]
        )
    )

    fixed_cart_code = (
        "def calculate_subtotal(items: list[dict]) -> float:\n"
        "    for item in items:\n"
        "        if item.get('price', 0) < 0 or item.get('quantity', 1) < 0:\n"
        "            raise ValueError('Item price and quantity must be non-negative')\n"
        "    return round(sum(float(item['price']) * int(item.get('quantity', 1)) for item in items), 2)\n\n"
        "def apply_discount(subtotal: float, promo_code: str | None, is_vip: bool = False) -> float:\n"
        "    if subtotal < 0:\n"
        "        raise ValueError('Subtotal cannot be negative')\n"
        "    discounted = float(subtotal)\n"
        "    code = (promo_code or '').strip().upper()\n"
        "    if code == 'SAVE10':\n"
        "        discounted = discounted * 0.90\n"
        "    elif code == 'SAVE20':\n"
        "        discounted = discounted * 0.80\n"
        "    elif code == 'FLAT50':\n"
        "        discounted = max(0.0, discounted - 50.0)\n"
        "    elif code != '':\n"
        "        raise ValueError(f'Unknown promo code: {promo_code}')\n"
        "    if is_vip:\n"
        "        discounted = discounted * 0.95\n"
        "    return round(max(0.0, discounted), 2)\n\n"
        "def format_receipt(subtotal: float, final_total: float) -> str:\n"
        "    saved = round(subtotal - final_total, 2)\n"
        "    return f'Subtotal: ${subtotal:.2f} | Saved: ${saved:.2f} | Total: ${final_total:.2f}'\n"
    )

    defender_mock = AsyncMock(
        return_value=DefenderOutput(
            responses=[
                DefenseResponse(
                    charge_id="C1",
                    stance="fix",
                    reasoning="Clamped FLAT50 at 0.0",
                    fixed_file_path="shop/cart.py",
                    fixed_code=fixed_cart_code,
                ),
                DefenseResponse(
                    charge_id="C2",
                    stance="rebut",
                    reasoning="Compound VIP discount is documented in the spec",
                    quoted_intent="VIP members receive an additional 5% compound discount applied AFTER any promo code",
                ),
            ]
        )
    )

    judge_mock = AsyncMock(
        return_value=JudgeOutput(
            rebuttal_rulings=[
                RebuttalRuling(
                    charge_id="C2",
                    upheld=True,
                    reason="Quoted intent confirms compound VIP calculation.",
                )
            ],
            ruling="Charge C1 was fixed and verified in the sandbox. Charge C2 was dismissed via upheld rebuttal.",
            summary="1 fixed, 1 dismissed.",
        )
    )

    with (
        patch("backend.orchestrator.run_prosecutor", prosecutor_mock),
        patch("backend.orchestrator.run_defender", defender_mock),
        patch("backend.orchestrator.run_judge", judge_mock),
        patch("backend.orchestrator.save_fixture"),
    ):
        frames = [
            f
            async for f in run_trial_stream(
                "https://github.com/demo/shop-cart/pull/42",
                replay=False,
                workdir_root=tmp_path,
            )
        ]

    events = _parse_sse_frames(frames)
    verdict_event = events[-1][1]
    assert verdict_event["verdict"] == "merge_with_fixes"
    assert verdict_event["confidence"] == 0.90
    assert verdict_event["stats"]["charges_filed"] == 2
    assert verdict_event["stats"]["charges_valid"] == 2
    assert verdict_event["stats"]["fixes_applied"] == 1
    assert verdict_event["stats"]["existing_tests_passed"] is True


def test_combine_file_fixes_non_overlapping():
    orig = "def a():\n    return 1\n\ndef b():\n    return 2\n"
    fix1 = "def a():\n    return 10\n\ndef b():\n    return 2\n"
    fix2 = "def a():\n    return 1\n\ndef b():\n    return 20\n"
    combined = combine_file_fixes(orig, [fix1, fix2])
    assert "return 10" in combined
    assert "return 20" in combined


@pytest.mark.asyncio
async def test_fastapi_endpoints_and_sse_replay():
    import httpx
    from backend.main import app
    from backend.replay import stream_replay_events as real_stream

    transport = httpx.ASGITransport(app=app)
    with patch("backend.orchestrator.stream_replay_events") as mock_stream:
        mock_stream.side_effect = lambda pr_url=None: real_stream(
            pr_url=pr_url, delay_scale=0.0
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            health = await client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

            index_res = await client.get("/")
            assert index_res.status_code == 200
            assert "PR Courtroom" in index_res.text

            sse_res = await client.get(
                "/api/trial",
                params={
                    "pr_url": "https://github.com/demo/shop-cart/pull/42",
                    "replay": "true",
                },
            )
            assert sse_res.status_code == 200
            assert "event: trial_started" in sse_res.text
            assert "event: verdict" in sse_res.text

