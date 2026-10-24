import asyncio
import sys
import time
import uuid

from backend.agents.defender import defend
from backend.agents.gemini_client import GeminiClientError
from backend.agents.prosecutor import prosecute
from backend.github_client import GitHubClientError, prepare_workspace
from backend.orchestrator import execute_fix_loop
from backend.sandbox import classify_all_async


async def run_defense_pipeline(pr_url: str) -> int:
    start_ts = time.perf_counter()
    run_id = f"defense_{uuid.uuid4().hex[:8]}"

    try:
        workspace = await prepare_workspace(pr_url, run_id)
        prosecutor_output = await prosecute(workspace, workspace["head_dir"])
    except (ValueError, GitHubClientError, GeminiClientError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    charges = prosecutor_output.charges
    initial_results = await classify_all_async(
        charges,
        head_dir=workspace["head_dir"],
        base_dir=workspace["base_dir"],
    )

    print("=== STEP 1 & 2: PROSECUTOR CHARGES & SANDBOX EVIDENCE ===")
    print(f"PR: {workspace['title']} ({pr_url})\n")

    valid_charges = []
    results_by_id = {}
    for charge, res in zip(charges, initial_results):
        results_by_id[charge.id] = res
        print(
            f"[{charge.id}] ({charge.severity.upper()}) {charge.title}\n"
            f"  Target Symbol : {charge.target_symbol}\n"
            f"  Initial Status: {res.status} (new_code={res.new_code})"
        )
        if res.status == "fail_valid":
            valid_charges.append(charge)
    print()

    try:
        defender_output = await defend(
            pr=workspace,
            valid_charges=valid_charges,
            test_results=results_by_id,
            head_dir=workspace["head_dir"],
        )
    except GeminiClientError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    loop_out = await execute_fix_loop(
        valid_charges=valid_charges,
        responses=defender_output.responses,
        head_dir=workspace["head_dir"],
    )

    print("=== STEP 3: DEFENDER RESPONSES ===")
    for resp in loop_out["responses"]:
        print(f"[{resp.charge_id}] Stance: {resp.stance.upper()}")
        print(f"  Reasoning    : {resp.reasoning}")
        if resp.stance == "rebut":
            print(f"  Quoted Intent: {resp.quoted_intent!r}")
        else:
            print(f"  Fixed File   : {resp.fixed_file_path}")
        print()

    print("=== STEP 4: FIX LOOP & RE-TEST RESULTS ===")
    for fx in loop_out["fix_results"]:
        print(
            f"File: {fx['file_path']} | Charges: {', '.join(fx['charge_ids'])} | "
            f"Status: {fx['status']} | Existing Tests Passed: {fx['existing_tests_passed']}"
        )
        print("Diff:")
        print(fx["diff"])

    for after_tr in loop_out["after_fix_results"]:
        badge = "GREEN (PASS)" if after_tr.status == "pass" else after_tr.status.upper()
        print(
            f"After-Fix Test [{after_tr.charge_id}] (phase={after_tr.phase}): {badge}"
        )

    elapsed = time.perf_counter() - start_ts
    print(f"\nTotal time: {elapsed:.2f}s")
    return 0


async def main() -> int:
    pr_url = sys.argv[1] if len(sys.argv) >= 2 else "https://github.com/demo/shop/pull/1"
    return await run_defense_pipeline(pr_url)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
