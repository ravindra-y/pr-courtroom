import json
import uuid
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.github_client import parse_pr_url
from backend.orchestrator import run_trial
from backend.replay import list_fixtures, replay_trial

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"

app = FastAPI(title="PR Courtroom")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _format_sse(event_name: str, data: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/fixtures")
async def get_fixtures() -> dict[str, list[str]]:
    return {"fixtures": list_fixtures()}


@app.get("/api/trial")
async def api_trial(
    pr_url: str = Query(default=""),
    replay: bool = Query(default=False),
    fixture: str = Query(default="demo"),
) -> StreamingResponse:
    async def event_stream() -> AsyncIterator[str]:
        if replay:
            async for ev_name, ev_data in replay_trial(fixture_name=fixture):
                yield _format_sse(ev_name, ev_data)
            return

        try:
            parse_pr_url(pr_url)
        except ValueError as exc:
            yield _format_sse("error", {"message": str(exc)})
            return

        run_id = f"live_{uuid.uuid4().hex[:8]}"
        async for ev_name, ev_data in run_trial(pr_url=pr_url, run_id=run_id):
            yield _format_sse(ev_name, ev_data)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=headers,
    )


if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
