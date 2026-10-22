import ast
import asyncio
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from backend.models import Charge, TestResult

load_dotenv()

MAX_OUTPUT_CHARS = 2000
BROKEN_TEST_RE = re.compile(
    r"\b(ImportError|ModuleNotFoundError|SyntaxError|IndentationError|NameError)\b"
)

_FILE_BACKUPS: dict[tuple[str, str], Optional[str]] = {}


def _default_timeout(timeout: Optional[float] = None) -> float:
    if timeout is not None:
        return float(timeout)
    return float(os.getenv("SANDBOX_TIMEOUT_SECONDS", "10"))


def _sanitize_output(text: str, *paths_to_strip: str | Path) -> str:
    """Strip absolute paths from pytest output and truncate to 2000 chars."""
    cleaned = text or ""
    candidates: list[str] = []
    for p in paths_to_strip:
        if not p:
            continue
        for variant in (str(Path(p).resolve()), str(Path(p))):
            if variant and variant not in candidates:
                candidates.append(variant.rstrip("/"))
    # Sort longest first so nested paths are stripped cleanly
    candidates.sort(key=len, reverse=True)
    for prefix in candidates:
        cleaned = cleaned.replace(prefix + "/", "")
        cleaned = cleaned.replace(prefix, ".")
    cleaned = re.sub(r"/(?:tmp|var|home|private)[^\s:]*?/(tests_generated/)", r"\1", cleaned)
    if len(cleaned) > MAX_OUTPUT_CHARS:
        cleaned = cleaned[:MAX_OUTPUT_CHARS]
    return cleaned


def run_test(
    version_dir: str | Path,
    test_file: str | Path,
    timeout: Optional[float] = None,
) -> dict:
    """
    Run pytest in a subprocess against `version_dir` with an isolated environment
    containing only PATH and PYTHONPATH.
    Returns dict {returncode, stdout, stderr, timed_out}.
    """
    ver_path = Path(version_dir).resolve()
    abs_test_path = str(Path(test_file).resolve())
    effective_timeout = _default_timeout(timeout)

    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONPATH": str(ver_path),
    }
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        abs_test_path,
        "-x",
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
    ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ver_path),
            env=env,
            timeout=effective_timeout,
            capture_output=True,
            text=True,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return {
            "returncode": -1,
            "stdout": stdout,
            "stderr": stderr or f"Test timed out after {effective_timeout}s",
            "timed_out": True,
        }


def symbol_exists(version_dir: str | Path, target_symbol: str) -> bool:
    """
    Map dotted `target_symbol` (e.g. 'shop.cart.apply_discount') to a Python file
    under `version_dir`, parse it with Python's `ast`, and check that a top-level
    function or class with that name is defined. Return False if file does not exist.
    """
    ver_path = Path(version_dir).resolve()
    parts = [p for p in target_symbol.strip().split(".") if p]
    if len(parts) < 2:
        return False

    mod_parts = parts[:-1]
    symbol_name = parts[-1]

    candidates = [
        ver_path.joinpath(*mod_parts).with_suffix(".py"),
        ver_path.joinpath(*mod_parts) / "__init__.py",
    ]
    target_file: Optional[Path] = None
    for cand in candidates:
        if cand.is_file():
            target_file = cand
            break

    if target_file is None:
        return False

    try:
        source = target_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(target_file))
    except (OSError, SyntaxError):
        return False

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol_name:
                return True
    return False


def _is_broken_test(returncode: int, combined_output: str) -> bool:
    """Return True if exit code or output indicates collection/import/syntax/name/fixture error."""
    if returncode == 2 or returncode not in (0, 1):
        return True
    if BROKEN_TEST_RE.search(combined_output):
        return True
    if ("fixture '" in combined_output and "not found" in combined_output) or (
        "FixtureLookupError" in combined_output
    ):
        return True
    if "ERROR at setup of" in combined_output:
        return True
    return False


def _resolve_generated_test_file(
    charge: Charge,
    head_dir: str | Path,
    tests_dir: Optional[str | Path] = None,
) -> Path:
    if tests_dir is None:
        gen_dir = Path(head_dir).resolve() / "tests_generated"
    else:
        provided = Path(tests_dir).resolve()
        gen_dir = (
            provided
            if provided.name == "tests_generated"
            else provided / "tests_generated"
        )
    gen_dir.mkdir(parents=True, exist_ok=True)
    test_file = gen_dir / f"test_{charge.id}.py"
    test_file.write_text(charge.test_code, encoding="utf-8")
    return test_file


def classify(
    charge: Charge,
    head_dir: str | Path,
    base_dir: str | Path,
    tests_dir: Optional[str | Path] = None,
    timeout: Optional[float] = None,
) -> TestResult:
    """
    Write the charge test to tests_generated/test_<charge_id>.py and classify it
    according to the Sandbox classification rules in AGENTS.md.
    """
    head_path = Path(head_dir).resolve()
    base_path = Path(base_dir).resolve()
    test_file = _resolve_generated_test_file(charge, head_path, tests_dir)

    # Step 1: Run on HEAD
    head_res = run_test(head_path, test_file, timeout=timeout)
    raw_head_out = (head_res["stdout"] + "\n" + head_res["stderr"]).strip()
    clean_head_out = _sanitize_output(
        raw_head_out, head_path, base_path, test_file.parent.parent, test_file.parent
    )

    if head_res["timed_out"]:
        return TestResult(
            charge_id=charge.id,
            status="timeout",
            new_code=False,
            output=clean_head_out,
            phase="initial",
        )

    if head_res["returncode"] == 0:
        return TestResult(
            charge_id=charge.id,
            status="pass",
            new_code=False,
            output=clean_head_out,
            phase="initial",
        )

    if _is_broken_test(head_res["returncode"], raw_head_out):
        return TestResult(
            charge_id=charge.id,
            status="error",
            new_code=False,
            output=clean_head_out,
            phase="initial",
        )

    # Step 2: Candidate failure on HEAD (exit code 1 with AssertionError or runtime exception)
    if not symbol_exists(base_path, charge.target_symbol):
        return TestResult(
            charge_id=charge.id,
            status="fail_valid",
            new_code=True,
            output=clean_head_out,
            phase="initial",
        )

    # Target symbol exists on BASE: run the same test on BASE
    base_res = run_test(base_path, test_file, timeout=timeout)
    if not base_res["timed_out"] and base_res["returncode"] == 0:
        return TestResult(
            charge_id=charge.id,
            status="fail_valid",
            new_code=False,
            output=clean_head_out,
            phase="initial",
        )

    return TestResult(
        charge_id=charge.id,
        status="fail_preexisting",
        new_code=False,
        output=clean_head_out,
        phase="initial",
    )


def apply_fix(
    head_dir: str | Path,
    file_path: str,
    new_content: str,
) -> str:
    """
    Write replacement file content to `head_dir/file_path`, keep a backup copy so it
    can be reverted, and return a unified diff string (difflib) of the change.
    """
    head_path = Path(head_dir).resolve()
    target = (head_path / file_path).resolve()
    if not str(target).startswith(str(head_path) + os.sep):
        raise ValueError(f"Unsafe file_path outside head_dir: {file_path}")

    key = (str(head_path), file_path)
    old_content = target.read_text(encoding="utf-8") if target.is_file() else ""
    if key not in _FILE_BACKUPS:
        _FILE_BACKUPS[key] = old_content if target.is_file() else None
        backup_path = target.with_suffix(target.suffix + ".bak")
        if target.is_file():
            backup_path.write_text(old_content, encoding="utf-8")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_content, encoding="utf-8")

    diff = "".join(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
    )
    return diff


def revert_fix(head_dir: str | Path, file_path: str) -> None:
    """Restore a fixed file in `head_dir` from its backup copy."""
    head_path = Path(head_dir).resolve()
    target = (head_path / file_path).resolve()
    backup_path = target.with_suffix(target.suffix + ".bak")
    key = (str(head_path), file_path)

    if backup_path.is_file():
        target.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
        backup_path.unlink(missing_ok=True)
        _FILE_BACKUPS.pop(key, None)
    elif key in _FILE_BACKUPS:
        original = _FILE_BACKUPS.pop(key)
        if original is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(original, encoding="utf-8")


def run_existing_tests(
    head_dir: str | Path,
    timeout: Optional[float] = None,
) -> tuple[bool, str]:
    """
    Run pytest on the repo's own `tests/` folder only (never on `tests_generated`).
    Returns (passed: bool, output: str).
    """
    head_path = Path(head_dir).resolve()
    repo_tests_dir = head_path / "tests"
    res = run_test(head_path, repo_tests_dir, timeout=timeout)
    raw_out = (res["stdout"] + "\n" + res["stderr"]).strip()
    clean_out = _sanitize_output(raw_out, head_path)
    passed = (not res["timed_out"]) and (res["returncode"] == 0)
    return passed, clean_out


def run_after_fix(
    charge: Charge,
    head_dir: str | Path,
    tests_dir: Optional[str | Path] = None,
    timeout: Optional[float] = None,
) -> TestResult:
    """
    Run the charge's test on `head_dir` after a fix has been applied.
    Returns TestResult with phase='after_fix' (status 'pass' means the fix works).
    """
    head_path = Path(head_dir).resolve()
    test_file = _resolve_generated_test_file(charge, head_path, tests_dir)
    res = run_test(head_path, test_file, timeout=timeout)
    raw_out = (res["stdout"] + "\n" + res["stderr"]).strip()
    clean_out = _sanitize_output(raw_out, head_path, test_file.parent.parent, test_file.parent)

    if res["timed_out"]:
        status = "timeout"
    elif res["returncode"] == 0:
        status = "pass"
    elif _is_broken_test(res["returncode"], raw_out):
        status = "error"
    else:
        status = "fail_valid"

    return TestResult(
        charge_id=charge.id,
        status=status,  # type: ignore[arg-type]
        new_code=False,
        output=clean_out,
        phase="after_fix",
    )


# Async wrappers using asyncio.to_thread so multiple tests can run with asyncio.gather
async def run_test_async(
    version_dir: str | Path,
    test_file: str | Path,
    timeout: Optional[float] = None,
) -> dict:
    return await asyncio.to_thread(run_test, version_dir, test_file, timeout)


async def classify_async(
    charge: Charge,
    head_dir: str | Path,
    base_dir: str | Path,
    tests_dir: Optional[str | Path] = None,
    timeout: Optional[float] = None,
) -> TestResult:
    return await asyncio.to_thread(classify, charge, head_dir, base_dir, tests_dir, timeout)


async def classify_all_async(
    charges: list[Charge],
    head_dir: str | Path,
    base_dir: str | Path,
    tests_dir: Optional[str | Path] = None,
    timeout: Optional[float] = None,
) -> list[TestResult]:
    tasks = [
        classify_async(c, head_dir, base_dir, tests_dir, timeout) for c in charges
    ]
    return list(await asyncio.gather(*tasks))


async def apply_fix_async(
    head_dir: str | Path,
    file_path: str,
    new_content: str,
) -> str:
    return await asyncio.to_thread(apply_fix, head_dir, file_path, new_content)


async def run_existing_tests_async(
    head_dir: str | Path,
    timeout: Optional[float] = None,
) -> tuple[bool, str]:
    return await asyncio.to_thread(run_existing_tests, head_dir, timeout)


async def run_after_fix_async(
    charge: Charge,
    head_dir: str | Path,
    tests_dir: Optional[str | Path] = None,
    timeout: Optional[float] = None,
) -> TestResult:
    return await asyncio.to_thread(run_after_fix, charge, head_dir, tests_dir, timeout)
