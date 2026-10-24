import asyncio
import json
import sys
from backend.agents.gemini_client import GeminiClientError
from backend.github_client import GitHubClientError
from backend.orchestrator import run_full_trial


async def run_once(pr_url: str, run_number: int | None = None) -> int:
    try:
        result = await run_full_trial(pr_url)
    except (ValueError, GitHubClientError, GeminiClientError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    header = f"=== Trial Run {run_number} ===" if run_number is not None else "=== PR Courtroom Trial ==="
    print(header)
    print(f"PR Title   : {result['pr']['title']}")
    print(f"Verdict    : {result['verdict']}")
    print(f"Confidence : {result['confidence']:.2f}")
    print(f"Ruling     : {result['judge_output'].ruling}")
    print(f"Summary    : {result['judge_output'].summary}")
    if result["judge_output"].rebuttal_rulings:
        print("Rebuttal Rulings:")
        for rr in result["judge_output"].rebuttal_rulings:
            status = "UPHELD (charge dismissed)" if rr.upheld else "OVERRULED"
            print(f"  - [{rr.charge_id}] {status}: {rr.reason}")
    print(f"Stats      : {json.dumps(result['stats'])}")
    print(f"Total Time : {result['duration_s']:.2f}s\n")
    return 0


async def main() -> int:
    pr_url = sys.argv[1] if len(sys.argv) >= 2 else "https://github.com/demo/shop/pull/1"
    runs = 1
    if "--runs" in sys.argv:
        idx = sys.argv.index("--runs")
        if idx + 1 < len(sys.argv):
            runs = int(sys.argv[idx + 1])

    for i in range(1, runs + 1):
        rc = await run_once(pr_url, run_number=i if runs > 1 else None)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
