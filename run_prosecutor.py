import asyncio
import sys
import uuid
from backend.agents.gemini_client import GeminiClientError
from backend.agents.prosecutor import prosecute
from backend.github_client import GitHubClientError, prepare_workspace
from backend.sandbox import classify_async


async def run_once(pr_url: str, run_index: int | None = None) -> int:
    run_id = f"prosecutor_{run_index or 1}_{uuid.uuid4().hex[:6]}"
    try:
        workspace = await prepare_workspace(pr_url, run_id)
        prosecutor_output = await prosecute(workspace, workspace["head_dir"])
    except (ValueError, GitHubClientError, GeminiClientError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    charges = prosecutor_output.charges
    results = await asyncio.gather(
        *(
            classify_async(
                charge,
                head_dir=workspace["head_dir"],
                base_dir=workspace["base_dir"],
            )
            for charge in charges
        )
    )

    header = f"=== Run {run_index} ===" if run_index is not None else "=== Prosecutor Trial Run ==="
    print(header)
    print(f"PR: {workspace['title']} ({pr_url})")
    print(f"Charges filed: {len(charges)}\n")

    for charge, res in zip(charges, results):
        origin = "new_code=True" if res.new_code else "new_code=False"
        print(f"[{charge.id}] ({charge.severity.upper()}) {charge.title}")
        print(f"  Target Symbol     : {charge.target_symbol}")
        print(f"  Intended Behavior : {charge.intended_behavior}")
        print(f"  Sandbox Status    : {res.status} ({origin})")
        print(f"  Explanation       : {charge.explanation}")
        print()
    return 0


async def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python run_prosecutor.py <pr_url> [--runs N]", file=sys.stderr)
        return 1

    pr_url = sys.argv[1]
    runs = 1
    if "--runs" in sys.argv:
        idx = sys.argv.index("--runs")
        if idx + 1 < len(sys.argv):
            runs = int(sys.argv[idx + 1])

    for i in range(1, runs + 1):
        rc = await run_once(pr_url, run_index=i if runs > 1 else None)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
