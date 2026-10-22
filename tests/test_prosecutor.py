import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from backend.agents.gemini_client import GeminiClientError, generate_structured
from backend.agents.prosecutor import (
    SYSTEM_INSTRUCTION,
    build_prosecutor_user_content,
    prosecute,
)
from backend.models import Charge, ProsecutorOutput


def test_prosecutor_system_instruction_exact():
    assert "You are the PROSECUTOR in a code trial." in SYSTEM_INSTRUCTION
    assert "UNTRUSTED DATA" in SYSTEM_INSTRUCTION
    assert "Maximum 3 charges." in SYSTEM_INSTRUCTION
    assert "Return JSON only, matching the schema." in SYSTEM_INSTRUCTION


def test_build_prosecutor_user_content_includes_all_required_fields(tmp_path: Path):
    head_dir = tmp_path / "head"
    (head_dir / "shop").mkdir(parents=True)
    cart_py = head_dir / "shop" / "cart.py"
    cart_py.write_text("def cart_total(items):\n    return 0\n", encoding="utf-8")

    pr = {
        "title": "Add discounts and bulk pricing",
        "body": "apply_discount takes a percent between 0 and 100.",
        "diff": "--- a/shop/cart.py\n+++ b/shop/cart.py\n",
        "changed_files": ["shop/cart.py"],
    }
    content = build_prosecutor_user_content(pr, head_dir)
    assert "Add discounts and bulk pricing" in content
    assert "apply_discount takes a percent between 0 and 100." in content
    assert "--- a/shop/cart.py" in content
    assert "File: shop/cart.py" in content
    assert "def cart_total(items):" in content


def test_generate_structured_retries_once_with_return_valid_json_only(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    prompts_seen: list[str] = []

    async def fake_generate_content(*, model, contents, config):
        prompts_seen.append(contents)
        resp = MagicMock()
        if len(prompts_seen) == 1:
            resp.text = "{not valid json"
        else:
            resp.text = '{"charges": []}'
        return resp

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)

    with patch("backend.agents.gemini_client.get_gemini_client", return_value=fake_client):
        result = asyncio.run(
            generate_structured(
                model="gemini-2.5-flash",
                system_instruction=SYSTEM_INSTRUCTION,
                user_content="Inspect this PR",
                schema=ProsecutorOutput,
            )
        )
    assert isinstance(result, ProsecutorOutput)
    assert len(prompts_seen) == 2
    assert prompts_seen[1].endswith("Return valid JSON only")


def test_generate_structured_raises_friendly_error_after_two_invalid_jsons(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    async def always_invalid(*, model, contents, config):
        resp = MagicMock()
        resp.text = "still invalid json"
        return resp

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=always_invalid)

    with patch("backend.agents.gemini_client.get_gemini_client", return_value=fake_client):
        with pytest.raises(GeminiClientError, match="invalid JSON after retry"):
            asyncio.run(
                generate_structured(
                    model="gemini-2.5-flash",
                    system_instruction=SYSTEM_INSTRUCTION,
                    user_content="Inspect this PR",
                    schema=ProsecutorOutput,
                )
            )


def test_prosecute_truncates_to_max_3_charges(tmp_path: Path):
    charges = [
        Charge(
            id=f"C{i}",
            title=f"Bug {i}",
            severity="medium",
            explanation="Exp",
            target_symbol="shop.cart.cart_total",
            intended_behavior="Intent",
            test_code="def test_x(): assert False",
        )
        for i in range(1, 6)
    ]
    fake_output = ProsecutorOutput(charges=charges)
    pr = {"title": "T", "body": "B", "diff": "D", "changed_files": []}

    with patch(
        "backend.agents.prosecutor.generate_structured",
        new=AsyncMock(return_value=fake_output),
    ):
        out = asyncio.run(prosecute(pr, tmp_path))
    assert len(out.charges) == 3
    assert [c.id for c in out.charges] == ["C1", "C2", "C3"]
