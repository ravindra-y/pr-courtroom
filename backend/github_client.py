import asyncio
import difflib
import hashlib
import io
import os
import re
import shutil
import zipfile
from pathlib import Path
import httpx
from dotenv import load_dotenv

load_dotenv()

GITHUB_API_BASE = "https://api.github.com"
PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9]\d*)(?:/.*)?$"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_REPO_DIR = REPO_ROOT / "demo-repo"
WORKDIR_ROOT = Path("workdir")
CACHE_DIR = WORKDIR_ROOT / "_cache"
SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")


class GitHubClientError(RuntimeError):
    """Friendly exception raised for GitHub API, auth, not-found, or rate-limit errors."""


def _ssl_verify() -> str | bool:
    env_cert = os.getenv("SSL_CERT_FILE")
    if env_cert and Path(env_cert).is_file():
        return env_cert
    if SYSTEM_CA_BUNDLE.is_file():
        return str(SYSTEM_CA_BUNDLE)
    return True


def parse_pr_url(url: str) -> tuple[str, str, int]:
    """
    Parse a GitHub Pull Request URL into (owner, repo, number).
    Accepts https://github.com/<owner>/<repo>/pull/<n> with optional trailing slash or extra path.
    Raises a clear ValueError for anything else.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError(
            "Invalid PR URL: URL cannot be empty. Expected https://github.com/<owner>/<repo>/pull/<number>"
        )
    cleaned = url.strip().split("?")[0].split("#")[0]
    match = PR_URL_PATTERN.match(cleaned)
    if not match:
        raise ValueError(
            f"Invalid GitHub Pull Request URL: '{url}'. "
            "Expected https://github.com/<owner>/<repo>/pull/<number>"
        )
    owner = match.group("owner")
    repo = match.group("repo")
    number = int(match.group("number"))
    return owner, repo, number


def _get_token() -> str:
    load_dotenv()
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise GitHubClientError(
            "Missing GITHUB_TOKEN. Please set GITHUB_TOKEN in your .env file."
        )
    return token


def _build_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    token = _get_token()
    return {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "User-Agent": "pr-courtroom",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _check_response(resp: httpx.Response, context: str) -> None:
    if resp.status_code == 200:
        return
    if resp.status_code == 401:
        raise GitHubClientError(
            "GitHub authentication failed (401). Please check that GITHUB_TOKEN in .env is valid."
        )
    if resp.status_code == 404:
        raise GitHubClientError(
            f"{context} not found (404). The pull request or repository may not exist or may be private."
        )
    if resp.status_code in (403, 429):
        raise GitHubClientError(
            f"GitHub API rate limit exceeded or access forbidden ({resp.status_code}) while fetching {context}. "
            "Please check your GITHUB_TOKEN permissions or wait before retrying."
        )
    raise GitHubClientError(
        f"GitHub API error ({resp.status_code}) while fetching {context}."
    )


def _build_local_demo_zip(src_dir: Path, top_folder: str) -> tuple[str, bytes]:
    """Create a GitHub-style zipball (with a single top-level folder) from demo-repo/{base,head}."""
    buf = io.BytesIO()
    hasher = hashlib.sha1()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(src_dir.rglob("*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(src_dir)
            if "__pycache__" in rel.parts or ".pytest_cache" in rel.parts:
                continue
            data = file_path.read_bytes()
            hasher.update(str(rel).encode("utf-8"))
            hasher.update(data)
            zf.writestr(f"{top_folder}/{rel.as_posix()}", data)
    sha = hasher.hexdigest()
    return sha, buf.getvalue()


def _ensure_local_demo_cache() -> dict:
    """Prepare cached zipballs and PR metadata for local demo-repo/{base,head}."""
    base_src = DEMO_REPO_DIR / "base"
    head_src = DEMO_REPO_DIR / "head"
    base_sha, base_zip = _build_local_demo_zip(base_src, "demo-shop-base")
    head_sha, head_zip = _build_local_demo_zip(head_src, "demo-shop-head")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{base_sha}.zip").write_bytes(base_zip)
    (CACHE_DIR / f"{head_sha}.zip").write_bytes(head_zip)

    base_cart = (base_src / "shop" / "cart.py").read_text(encoding="utf-8").splitlines(keepends=True)
    head_cart = (head_src / "shop" / "cart.py").read_text(encoding="utf-8").splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            base_cart,
            head_cart,
            fromfile="a/shop/cart.py",
            tofile="b/shop/cart.py",
        )
    )

    return {
        "title": "Add discounts and bulk pricing",
        "body": (
            "apply_discount takes a percent between 0 and 100. "
            "Negative quantities raise ValueError by design (refunds are handled elsewhere). "
            "cart_total applies a 5% bulk price when the average item price is over 100."
        ),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "changed_files": ["shop/cart.py"],
        "diff": diff,
    }


async def get_pr(owner: str, repo: str, number: int) -> dict:
    """
    Fetch PR metadata, unified diff, and changed filenames from GitHub REST API.
    Returns dict with: title, body, base_sha, head_sha, changed_files, diff.
    """
    if owner == "demo" and (DEMO_REPO_DIR / "base").is_dir() and (DEMO_REPO_DIR / "head").is_dir():
        return _ensure_local_demo_cache()

    pr_endpoint = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{number}"
    files_endpoint = f"{pr_endpoint}/files"
    context_label = f"PR {owner}/{repo}#{number}"

    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, verify=_ssl_verify()
    ) as client:
        try:
            meta_resp, diff_resp, files_resp = await asyncio.gather(
                client.get(pr_endpoint, headers=_build_headers("application/vnd.github+json")),
                client.get(pr_endpoint, headers=_build_headers("application/vnd.github.v3.diff")),
                client.get(
                    files_endpoint,
                    headers=_build_headers("application/vnd.github+json"),
                    params={"per_page": 100},
                ),
            )
        except httpx.RequestError as exc:
            raise GitHubClientError(
                f"Network error while contacting GitHub for {context_label}: {exc}"
            ) from exc

    _check_response(meta_resp, context_label)
    _check_response(diff_resp, f"{context_label} diff")
    _check_response(files_resp, f"{context_label} files")

    meta = meta_resp.json()
    files_data = files_resp.json()
    changed_files = [
        item["filename"]
        for item in files_data
        if isinstance(item, dict) and "filename" in item
    ]

    return {
        "title": meta.get("title") or "",
        "body": meta.get("body") or "",
        "base_sha": meta["base"]["sha"],
        "head_sha": meta["head"]["sha"],
        "changed_files": changed_files,
        "diff": diff_resp.text,
    }


def _extract_zip_flattened(zip_bytes: bytes, dest_dir: Path) -> None:
    """Unzip archive into dest_dir and flatten the single top-level folder added by GitHub."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest_dir.resolve()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        file_members = [m for m in zf.infolist() if not m.is_dir()]
        if not file_members:
            return

        top_dirs = {
            Path(m.filename).parts[0]
            for m in file_members
            if len(Path(m.filename).parts) > 1
        }
        flatten = len(top_dirs) == 1

        for member in file_members:
            parts = Path(member.filename).parts
            if not parts:
                continue
            rel_parts = parts[1:] if (flatten and len(parts) > 1) else parts
            if not rel_parts:
                continue
            target_path = (resolved_dest / Path(*rel_parts)).resolve()
            if not str(target_path).startswith(str(resolved_dest) + os.sep):
                raise GitHubClientError(f"Unsafe path in zip archive: {member.filename}")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


async def download_repo(
    owner: str,
    repo: str,
    sha: str,
    dest_dir: str | Path,
) -> Path:
    """
    Download repo zipball for `sha` (caching in workdir/_cache/<sha>.zip),
    unzip into `dest_dir`, and flatten the single top-level folder GitHub adds.
    """
    dest_path = Path(dest_dir)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{sha}.zip"

    if cache_file.is_file() and cache_file.stat().st_size > 0:
        zip_bytes = cache_file.read_bytes()
    else:
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/zipball/{sha}"
        headers = _build_headers("application/vnd.github+json")
        async with httpx.AsyncClient(
            timeout=60.0, follow_redirects=True, verify=_ssl_verify()
        ) as client:
            try:
                resp = await client.get(url, headers=headers)
            except httpx.RequestError as exc:
                raise GitHubClientError(
                    f"Network error downloading {owner}/{repo}@{sha}: {exc}"
                ) from exc
        _check_response(resp, f"repository archive {owner}/{repo}@{sha[:7]}")
        zip_bytes = resp.content
        cache_file.write_bytes(zip_bytes)

    _extract_zip_flattened(zip_bytes, dest_path)
    return dest_path


async def prepare_workspace(pr_url: str, run_id: str) -> dict:
    """
    Fetch PR metadata/diff and concurrently download base and head snapshots into
    workdir/<run_id>/base and workdir/<run_id>/head.
    """
    owner, repo, number = parse_pr_url(pr_url)
    pr_data = await get_pr(owner, repo, number)

    run_dir = WORKDIR_ROOT / str(run_id)
    base_dir = run_dir / "base"
    head_dir = run_dir / "head"

    await asyncio.gather(
        download_repo(owner, repo, pr_data["base_sha"], base_dir),
        download_repo(owner, repo, pr_data["head_sha"], head_dir),
    )

    return {
        "pr_url": pr_url,
        "owner": owner,
        "repo": repo,
        "number": number,
        **pr_data,
        "base_dir": str(base_dir),
        "head_dir": str(head_dir),
    }
