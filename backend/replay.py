import asyncio
import json
import re
from pathlib import Path
from typing import Any, AsyncIterator

from backend.agents.gemini_client import redact_secrets

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def clamp_delay(delay: float) -> float:
    """Clamp delay between events to [0.5, 2.0] seconds per AGENTS.md."""
    return round(min(max(float(delay), 0.5), 2.0), 2)


def sanitize_fixture_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "demo").strip()).strip("_")
    if cleaned.endswith(".json"):
        cleaned = cleaned[:-5]
    return cleaned or "demo"


def list_fixtures() -> list[str]:
    """Return sorted list of available fixture names (without .json) in backend/fixtures/."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.stem for p in FIXTURES_DIR.glob("*.json") if p.is_file())


def save_fixture(run_name: str, events: list[dict[str, Any]]) -> Path:
    """Save recorded events [{"event": str, "data": dict, "delay": float}] to backend/fixtures/<run_name>.json."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_fixture_name(run_name)
    path = FIXTURES_DIR / f"{safe_name}.json"
    normalized = [
        {
            "event": item["event"],
            "data": item["data"],
            "delay": clamp_delay(item.get("delay", 0.5)),
        }
        for item in events
    ]
    raw_json = json.dumps(normalized, indent=2, ensure_ascii=False)
    path.write_text(redact_secrets(raw_json), encoding="utf-8")
    return path


async def replay_trial(
    fixture_name: str = "demo",
    delay_scale: float = 1.0,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """
    Async generator that reads backend/fixtures/<fixture_name>.json and yields
    (event_name, data_dict) with the saved delays. No API key, no network.
    """
    safe_name = sanitize_fixture_name(fixture_name)
    path = FIXTURES_DIR / f"{safe_name}.json"
    if not path.is_file():
        fallback = FIXTURES_DIR / "demo.json"
        if fallback.is_file():
            path = fallback
        else:
            yield (
                "error",
                {"message": f"Replay fixture '{safe_name}' was not found."},
            )
            return

    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        yield (
            "error",
            {"message": f"Replay fixture '{safe_name}' contains invalid JSON."},
        )
        return

    for item in items:
        delay = clamp_delay(item.get("delay", 0.5)) * max(0.0, float(delay_scale))
        if delay > 0:
            await asyncio.sleep(delay)
        yield item["event"], item["data"]
