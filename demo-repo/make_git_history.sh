#!/usr/bin/env bash
set -euo pipefail

# Creates a standalone git repository (default: ./demo-shop-repo) with:
#   - Commit 1 on branch "main"          = BASE
#   - Commit 2 on branch "add-discounts" = HEAD

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-${SCRIPT_DIR}/demo-shop-repo}"

rm -rf "${TARGET_DIR}"
mkdir -p "${TARGET_DIR}"

# 1. Copy BASE files and commit on main
cp -R "${SCRIPT_DIR}/base/." "${TARGET_DIR}/"
find "${TARGET_DIR}" -name "__pycache__" -o -name ".pytest_cache" | xargs rm -rf
cd "${TARGET_DIR}"

git init -b main
if [ -z "$(git config user.name || true)" ]; then
  git config user.name "PR Courtroom Demo"
fi
if [ -z "$(git config user.email || true)" ]; then
  git config user.email "demo@pr-courtroom.local"
fi
git add .
git commit -m "Initial cart_total implementation (BASE)"

# 2. Create branch "add-discounts", copy HEAD files, and commit
git checkout -b add-discounts
cp -R "${SCRIPT_DIR}/head/." "${TARGET_DIR}/"
find "${TARGET_DIR}" -name "__pycache__" -o -name ".pytest_cache" | xargs rm -rf
git add .
git commit -m "Add discounts and bulk pricing"

echo "Created git repository at: ${TARGET_DIR}"
git log --oneline --graph --all
