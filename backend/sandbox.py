import ast
import asyncio
import os
import re
import signal
import sys
import tempfile
from pathlib import Path
from typing import Optional

from backend.models import Charge, TestResult

BROKEN_TEST_ERROR_RE = re.compile(
    r"\b(ImportError|ModuleNotFoundError|SyntaxError|IndentationError|NameError)\b"
)


def _get_timeout(timeout: Optional[float] = None) -> float:
    if timeout is not None:
        return float(timeout)
    return float(os.getenv("SANDBOX_TIMEOUT_SECONDS", "10"))


def _build_sandbox_env(version_dir: Path) -> dict[str, str]:
    """Return an empty environment containing only PATH and PYTHONPATH."""
    python_bin_dir = str(Path(sys.executable).resolve().parent)
    system_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    path_parts = system_path.split(os.pathsep) if system_path else []
    if python_bin_dir not in path_parts:
        path_parts.insert(0, python_bin_dir)
    return {
        "PATH": os.pathsep.join(path_parts),
        "PYTHONPATH": str(version_dir.resolve()),
    }


async def run_pytest_command(
    version_dir: Path,
    target_path: Optional[Path] = None,
    timeout: Optional[float] = None,
) -> tuple[Optional[int], str, bool]:
    """
    Execute pytest in a sandboxed subprocess.
    Returns (returncode, combined_output, timed_out).
    """
    effective_timeout = _get_timeout(timeout)
    env = _build_sandbox_env(version_dir)
    cmd = ["python", "-m", "pytest"]
    if target_path is not None:
        cmd.append(str(target_path.resolve()))
    cmd.extend(["-x", "-q", "--tb=short", "-p", "no:cacheprovider"])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(version_dir.resolve()),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(), timeout=effective_timeout
        )
        output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        return proc.returncode, output, False
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            proc.kill()
        await proc.wait()
        return None, f"Test execution timed out after {effective_timeout}s", True


def _is_broken_test_output(returncode: Optional[int], output: str) -> bool:
    """Check if pytest output indicates a broken test rather than a valid runtime/assertion failure."""
    if returncode is None:
        return True
    if returncode == 2 or returncode not in (0, 1):
        return True
    if BROKEN_TEST_ERROR_RE.search(output):
        return True
    if ("fixture '" in output and "not found" in output) or "FixtureLookupError" in output:
        return True
    if "ERROR at setup of" in output:
        return True
    return False


def _find_symbol_in_nodes(nodes: list[ast.stmt], chain: list[str]) -> bool:
    if not chain:
        return False
    target = chain[0]
    rest = chain[1:]

    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == target:
                if not rest:
                    return True
                if _find_symbol_in_nodes(node.body, rest):
                    return True
        elif isinstance(node, ast.Assign) and not rest:
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == target:
                    return True
        elif isinstance(node, ast.AnnAssign) and not rest:
            if isinstance(node.target, ast.Name) and node.target.id == target:
                return True
        elif isinstance(node, ast.If):
            if _find_symbol_in_nodes(node.body, chain) or _find_symbol_in_nodes(
                node.orelse, chain
            ):
                return True
        elif isinstance(node, ast.Try):
            if (
                _find_symbol_in_nodes(node.body, chain)
                or _find_symbol_in_nodes(node.orelse, chain)
                or _find_symbol_in_nodes(node.finalbody, chain)
                or any(_find_symbol_in_nodes(h.body, chain) for h in node.handlers)
            ):
                return True
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            if _find_symbol_in_nodes(node.body, chain):
                return True

    return False


def _file_defines_symbol(py_file: Path, symbol_chain: list[str]) -> bool:
    try:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
    except (OSError, SyntaxError):
        return False
    return _find_symbol_in_nodes(tree.body, symbol_chain)


def symbol_exists_on_base(base_dir: Path, target_symbol: str) -> bool:
    """
    Check using Python's ast module whether target_symbol is defined on BASE.
    Supports dotted paths like 'shop.cart.apply_discount', 'shop.cart.Cart.apply_discount',
    file-qualified 'shop/cart.py::apply_discount', or bare symbol names.
    """
    cleaned = target_symbol.strip()
    if not cleaned:
        return False

    if ":" in cleaned:
        file_part, _, sym_part = cleaned.partition(":")
        sym_part = sym_part.lstrip(":")
        candidate_file = base_dir / file_part
        sym_chain = [p for p in sym_part.split(".") if p]
        if candidate_file.is_file() and sym_chain:
            return _file_defines_symbol(candidate_file, sym_chain)
        return False

    if cleaned.endswith(".py"):
        return (base_dir / cleaned).is_file()

    parts = [p for p in cleaned.split(".") if p]
    if not parts:
        return False

    if len(parts) >= 2:
        found_module_file = False
        for split_idx in range(len(parts) - 1, 0, -1):
            mod_parts = parts[:split_idx]
            sym_chain = parts[split_idx:]
            candidates = [
                base_dir.joinpath(*mod_parts).with_suffix(".py"),
                base_dir.joinpath(*mod_parts) / "__init__.py",
            ]
            for candidate in candidates:
                if candidate.is_file():
                    found_module_file = True
                    if _file_defines_symbol(candidate, sym_chain):
                        return True
        if found_module_file:
            return False

        # If len(parts) == 2 and first part is a class name (e.g. "Cart.apply_discount"),
        # search all python files in base_dir for that class.method.
        if len(parts) == 2 and parts[0][:1].isupper():
            for py_file in base_dir.rglob("*.py"):
                if _file_defines_symbol(py_file, parts):
                    return True
        return False

    # Bare symbol name (len(parts) == 1): search all .py files in base_dir
    for py_file in base_dir.rglob("*.py"):
        if _file_defines_symbol(py_file, parts):
            return True
    return False


async def classify_charge_test(
    charge: Charge,
    base_dir: Path,
    head_dir: Path,
    timeout: Optional[float] = None,
) -> TestResult:
    """
    Run and classify a single charge test according to the PR Courtroom sandbox rules.
    """
    with tempfile.TemporaryDirectory(prefix=f"pr_courtroom_{charge.id}_") as tmp_dir:
        test_file = Path(tmp_dir) / f"test_{charge.id.lower()}.py"
        test_file.write_text(charge.test_code, encoding="utf-8")

        # Step 1: Run on HEAD
        rc_head, out_head, timed_out_head = await run_pytest_command(
            version_dir=head_dir,
            target_path=test_file,
            timeout=timeout,
        )

        if timed_out_head:
            return TestResult(
                charge_id=charge.id,
                status="timeout",
                new_code=False,
                output=out_head,
                phase="initial",
            )

        if rc_head == 0:
            return TestResult(
                charge_id=charge.id,
                status="pass",
                new_code=False,
                output=out_head,
                phase="initial",
            )

        if _is_broken_test_output(rc_head, out_head):
            return TestResult(
                charge_id=charge.id,
                status="error",
                new_code=False,
                output=out_head,
                phase="initial",
            )

        # Candidate failure on HEAD (exit code 1 with AssertionError or runtime exception).
        # Step 2: Check if target_symbol is defined on BASE using Python ast.
        if not symbol_exists_on_base(base_dir, charge.target_symbol):
            return TestResult(
                charge_id=charge.id,
                status="fail_valid",
                new_code=True,
                output=out_head,
                phase="initial",
            )

        # Target symbol exists on BASE: run the same test on BASE.
        rc_base, out_base, timed_out_base = await run_pytest_command(
            version_dir=base_dir,
            target_path=test_file,
            timeout=timeout,
        )

        if not timed_out_base and rc_base == 0:
            return TestResult(
                charge_id=charge.id,
                status="fail_valid",
                new_code=False,
                output=out_head,
                phase="initial",
            )

        return TestResult(
            charge_id=charge.id,
            status="fail_preexisting",
            new_code=False,
            output=out_head,
            phase="initial",
        )


async def classify_charges_parallel(
    charges: list[Charge],
    base_dir: Path,
    head_dir: Path,
    timeout: Optional[float] = None,
) -> list[TestResult]:
    """Run all charge evidence checks in parallel via asyncio.gather."""
    tasks = [
        classify_charge_test(charge, base_dir=base_dir, head_dir=head_dir, timeout=timeout)
        for charge in charges
    ]
    return list(await asyncio.gather(*tasks))


async def run_charge_test_after_fix(
    charge: Charge,
    head_dir: Path,
    new_code: bool = False,
    timeout: Optional[float] = None,
) -> TestResult:
    """Re-run a charge's pytest test on HEAD after applying Defender fixes."""
    with tempfile.TemporaryDirectory(prefix=f"pr_courtroom_fix_{charge.id}_") as tmp_dir:
        test_file = Path(tmp_dir) / f"test_{charge.id.lower()}.py"
        test_file.write_text(charge.test_code, encoding="utf-8")

        rc, out, timed_out = await run_pytest_command(
            version_dir=head_dir,
            target_path=test_file,
            timeout=timeout,
        )

        if timed_out:
            status = "timeout"
        elif rc == 0:
            status = "pass"
        elif _is_broken_test_output(rc, out):
            status = "error"
        else:
            status = "fail_valid"

        return TestResult(
            charge_id=charge.id,
            status=status,  # type: ignore[arg-type]
            new_code=new_code,
            output=out,
            phase="after_fix",
        )


async def run_existing_tests(
    version_dir: Path,
    timeout: Optional[float] = None,
) -> tuple[bool, str]:
    """
    Run the repository's existing pytest suite in version_dir.
    Exit code 0 (all pass) or 5 (no existing tests collected) means no regressions.
    """
    rc, output, timed_out = await run_pytest_command(
        version_dir=version_dir,
        target_path=None,
        timeout=timeout,
    )
    if timed_out or rc is None:
        return False, output
    return rc in (0, 5), output
