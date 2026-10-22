import asyncio
import json
import re
from pathlib import Path
from typing import Any, AsyncIterator, Optional

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
DEFAULT_FIXTURE_NAME = "demo_trial.json"


def format_sse(event: str, data: Any) -> str:
    """Format an event name and payload as an SSE frame."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def slugify_pr_url(pr_url: str) -> str:
    cleaned = pr_url.strip().lower()
    cleaned = re.sub(r"^https?://(www\.)?github\.com/", "", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "trial"
    return f"{cleaned}.json"


def clamp_delay(delay: float) -> float:
    """Ensure replay delay is within [0.5, 2.0] seconds per specification."""
    return round(min(max(float(delay), 0.5), 2.0), 2)


def save_fixture(pr_url: str, events: list[dict[str, Any]]) -> Path:
    """Save a live trial's recorded SSE events to backend/fixtures/<name>.json."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    normalized = [
        {
            "event": item["event"],
            "data": item["data"],
            "delay": clamp_delay(item.get("delay", 0.8)),
        }
        for item in events
    ]
    filename = slugify_pr_url(pr_url)
    target_path = FIXTURES_DIR / filename
    content = json.dumps(normalized, indent=2, ensure_ascii=False)
    target_path.write_text(content, encoding="utf-8")
    (FIXTURES_DIR / "latest.json").write_text(content, encoding="utf-8")
    return target_path


def list_fixtures() -> list[dict[str, Any]]:
    """List available replay fixtures in backend/fixtures/."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        if path.name == "latest.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or not data:
                continue
            started = next((e["data"] for e in data if e.get("event") == "trial_started"), {})
            verdict_ev = next((e["data"] for e in data if e.get("event") == "verdict"), {})
            items.append(
                {
                    "name": path.stem,
                    "filename": path.name,
                    "pr_url": started.get("pr_url", ""),
                    "pr_title": started.get("pr_title", path.stem),
                    "verdict": verdict_ev.get("verdict"),
                    "confidence": verdict_ev.get("confidence"),
                }
            )
        except Exception:
            continue
    return items


def resolve_fixture_path(pr_url_or_name: Optional[str] = None) -> Path:
    """Resolve a PR URL or fixture name to a JSON fixture path, falling back to demo_trial.json."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    if pr_url_or_name:
        cleaned = pr_url_or_name.strip()
        direct = FIXTURES_DIR / (cleaned if cleaned.endswith(".json") else f"{cleaned}.json")
        if direct.is_file():
            return direct

        slug_path = FIXTURES_DIR / slugify_pr_url(cleaned)
        if slug_path.is_file():
            return slug_path

        for candidate in sorted(FIXTURES_DIR.glob("*.json")):
            try:
                events = json.loads(candidate.read_text(encoding="utf-8"))
                for ev in events:
                    if ev.get("event") == "trial_started":
                        if ev.get("data", {}).get("pr_url") == cleaned:
                            return candidate
            except Exception:
                continue

    default_path = FIXTURES_DIR / DEFAULT_FIXTURE_NAME
    if default_path.is_file():
        return default_path

    latest_path = FIXTURES_DIR / "latest.json"
    if latest_path.is_file():
        return latest_path

    raise FileNotFoundError("No replay fixtures found in backend/fixtures/")


def load_fixture_events(pr_url_or_name: Optional[str] = None) -> list[dict[str, Any]]:
    path = resolve_fixture_path(pr_url_or_name)
    return json.loads(path.read_text(encoding="utf-8"))


async def stream_replay_events(
    pr_url: Optional[str] = None,
    delay_scale: float = 1.0,
) -> AsyncIterator[str]:
    """Stream saved SSE events from a replay fixture with their saved delays (0.5 to 2.0 s)."""
    events = load_fixture_events(pr_url)
    for item in events:
        raw_delay = float(item.get("delay", 0.8))
        delay = clamp_delay(raw_delay) * max(0.0, delay_scale)
        if delay > 0:
            await asyncio.sleep(delay)
        data = dict(item["data"]) if isinstance(item.get("data"), dict) else item.get("data")
        if item.get("event") == "trial_started" and pr_url and isinstance(data, dict):
            if not data.get("pr_url"):
                data["pr_url"] = pr_url
        yield format_sse(item["event"], data)
