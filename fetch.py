import asyncio
import sys
import uuid
from backend.github_client import GitHubClientError, prepare_workspace


async def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python fetch.py <pr_url>", file=sys.stderr)
        return 1

    pr_url = sys.argv[1]
    run_id = sys.argv[2] if len(sys.argv) >= 3 else uuid.uuid4().hex[:8]

    try:
        result = await prepare_workspace(pr_url, run_id)
    except (ValueError, GitHubClientError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Title: {result['title']}")
    print(f"Description:\n{result['body']}")
    print(f"Changed files: {', '.join(result['changed_files'])}")
    print(f"Base SHA: {result['base_sha']}")
    print(f"Head SHA: {result['head_sha']}")
    print(f"Workspace Base Path: {result['base_dir']}")
    print(f"Workspace Head Path: {result['head_dir']}")
    print("\n--- Diff ---")
    print(result["diff"])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
