import asyncio
from dataclasses import dataclass
import difflib
import io
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Optional
import httpx

PR_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)(?:/.*)?$"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKDIR_ROOT = REPO_ROOT / "workdir"
DEMO_REPO_DIR = REPO_ROOT / "demo-repo"


@dataclass
class PRContext:
    pr_url: str
    pr_title: str
    pr_description: str
    diff: str
    base_sha: str
    head_sha: str
    files_changed: list[str]
    base_dir: Path
    head_dir: Path


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Extract (owner, repo, pull_number) from a GitHub Pull Request URL."""
    cleaned = pr_url.strip().split("?")[0].split("#")[0]
    if cleaned.startswith("demo://"):
        return "demo", "shop-cart", 42
    match = PR_URL_RE.match(cleaned)
    if not match:
        raise ValueError(
            f"Invalid GitHub Pull Request URL: {pr_url!r}. "
            "Expected https://github.com/<owner>/<repo>/pull/<number>"
        )
    return (
        match.group("owner"),
        match.group("repo"),
        int(match.group("number")),
    )


def _github_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": "PR-Courtroom/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def extract_zip_strip_toplevel(zip_bytes: bytes, dest_dir: Path) -> None:
    """
    Extract a GitHub zipball into dest_dir, stripping the single top-level directory
    created by GitHub archives and guarding against path traversal.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest_dir.resolve()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if not members:
            return

        first_parts = {Path(m.filename).parts[0] for m in members if Path(m.filename).parts}
        strip_first = len(first_parts) == 1

        for member in members:
            parts = Path(member.filename).parts
            if not parts:
                continue
            rel_parts = parts[1:] if (strip_first and len(parts) > 1) else parts
            if not rel_parts:
                continue
            target_path = (resolved_dest / Path(*rel_parts)).resolve()
            if not str(target_path).startswith(str(resolved_dest) + os.sep):
                raise ValueError(f"Unsafe zip member path detected: {member.filename}")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


async def _download_and_extract_sha(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    sha: str,
    dest_dir: Path,
) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{sha}"
    resp = await client.get(url, headers=_github_headers(), follow_redirects=True)
    resp.raise_for_status()
    extract_zip_strip_toplevel(resp.content, dest_dir)


def _prepare_demo_pr(run_id: str, pr_url: str, workdir_root: Path) -> PRContext:
    """Prepare workdir/<run_id>/base and workdir/<run_id>/head from local demo-repo snapshots."""
    run_dir = workdir_root / run_id
    base_dir = run_dir / "base"
    head_dir = run_dir / "head"
    if run_dir.exists():
        shutil.rmtree(run_dir)

    src_base = DEMO_REPO_DIR / "snapshots" / "base"
    src_head = DEMO_REPO_DIR / "snapshots" / "head"
    shutil.copytree(src_base, base_dir)
    shutil.copytree(src_head, head_dir)

    base_cart = (base_dir / "shop" / "cart.py").read_text(encoding="utf-8").splitlines(keepends=True)
    head_cart = (head_dir / "shop" / "cart.py").read_text(encoding="utf-8").splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            base_cart,
            head_cart,
            fromfile="a/shop/cart.py",
            tofile="b/shop/cart.py",
        )
    )

    pr_title = "Add tiered promo code discounts and refactor subtotal calculation"
    pr_description = (
        "Implements `apply_discount(subtotal, promo_code, is_vip=False)` in `shop/cart.py` "
        "supporting `SAVE10`, `SAVE20`, and `FLAT50`, and simplifies `calculate_subtotal`.\n\n"
        "Intent note (Finance Spec v2):\n"
        "VIP members receive an additional 5% compound discount applied AFTER any promo code "
        "percentage discount (compound discount, not additive: e.g. SAVE20 + VIP = 100 * 0.80 * 0.95 = 76.0). "
        "Total after discount cannot be negative (minimum 0.0)."
    )

    return PRContext(
        pr_url=pr_url,
        pr_title=pr_title,
        pr_description=pr_description,
        diff=diff,
        base_sha="a1b2c3d4base",
        head_sha="e5f6g7h8head",
        files_changed=["shop/cart.py"],
        base_dir=base_dir,
        head_dir=head_dir,
    )


async def fetch_pr_and_prepare_workdir(
    pr_url: str,
    run_id: str,
    workdir_root: Optional[Path] = None,
) -> PRContext:
    """
    Fetch PR metadata, diff, base SHA, and head SHA; download repo zips for both SHAs
    into workdir/<run_id>/base and workdir/<run_id>/head.
    """
    root = workdir_root or WORKDIR_ROOT
    owner, repo, number = parse_pr_url(pr_url)

    if owner == "demo" and repo == "shop-cart":
        return _prepare_demo_pr(run_id=run_id, pr_url=pr_url, workdir_root=root)

    run_dir = root / run_id
    base_dir = run_dir / "base"
    head_dir = run_dir / "head"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    head_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=30.0) as client:
        pr_api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"

        meta_resp, diff_resp, files_resp = await asyncio.gather(
            client.get(pr_api_url, headers=_github_headers()),
            client.get(
                pr_api_url,
                headers=_github_headers(accept="application/vnd.github.v3.diff"),
            ),
            client.get(f"{pr_api_url}/files", headers=_github_headers()),
        )

        meta_resp.raise_for_status()
        diff_resp.raise_for_status()

        meta = meta_resp.json()
        pr_title = meta.get("title") or f"PR #{number}"
        pr_description = meta.get("body") or ""
        base_sha = meta["base"]["sha"]
        head_sha = meta["head"]["sha"]
        diff_text = diff_resp.text

        files_changed: list[str] = []
        if files_resp.status_code == 200 and isinstance(files_resp.json(), list):
            files_changed = [f["filename"] for f in files_resp.json() if "filename" in f]
        if not files_changed:
            for line in diff_text.splitlines():
                if line.startswith("+++ b/"):
                    files_changed.append(line[6:].strip())

        await asyncio.gather(
            _download_and_extract_sha(client, owner, repo, base_sha, base_dir),
            _download_and_extract_sha(client, owner, repo, head_sha, head_dir),
        )

    return PRContext(
        pr_url=pr_url,
        pr_title=pr_title,
        pr_description=pr_description,
        diff=diff_text,
        base_sha=base_sha,
        head_sha=head_sha,
        files_changed=files_changed,
        base_dir=base_dir,
        head_dir=head_dir,
    )


def read_relevant_head_files(head_dir: Path, files_changed: list[str]) -> dict[str, str]:
    """Read the full content of changed Python files in head_dir for agent context."""
    contents: dict[str, str] = {}
    for rel_path in files_changed:
        if not rel_path.endswith(".py"):
            continue
        full_path = (head_dir / rel_path).resolve()
        if str(full_path).startswith(str(head_dir.resolve())) and full_path.is_file():
            try:
                contents[rel_path] = full_path.read_text(encoding="utf-8")
            except OSError:
                continue
    return contents
