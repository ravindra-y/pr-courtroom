import os
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.orchestrator import run_trial_stream
from backend.replay import list_fixtures

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"
FRONTEND_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="PR Courtroom",
    description="AI reviewers give opinions. PR Courtroom gives evidence.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health_check() -> dict[str, object]:
    return {
        "status": "ok",
        "has_gemini_key": bool(os.getenv("GEMINI_API_KEY", "").strip()),
    }


@app.get("/api/fixtures")
async def get_fixtures() -> dict[str, object]:
    return {
        "fixtures": list_fixtures(),
        "has_gemini_key": bool(os.getenv("GEMINI_API_KEY", "").strip()),
    }


@app.get("/api/trial")
async def stream_trial(
    pr_url: str = Query(
        default="https://github.com/demo/shop-cart/pull/42",
        description="GitHub Pull Request URL to put on trial",
    ),
    replay: bool = Query(
        default=False,
        description="Stream from saved fixture without calling Gemini API",
    ),
) -> StreamingResponse:
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        run_trial_stream(pr_url=pr_url, replay=replay),
        media_type="text/event-stream",
        headers=headers,
    )


@app.get("/")
async def index_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
