#!/usr/bin/env bash
# Release committed code: bash scripts/release.sh 0.4.2
# Requires Git, authenticated gh, and Python 3.11+ (PYTHON=... to choose it).
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { printf 'error: %s\n' "$*" >&2; exit 1; }
[ "$#" -eq 1 ] || fail "usage: bash scripts/release.sh VERSION (for example 0.4.2)"
version="${1#v}"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "version must be major.minor.patch"
tag="v$version"
PYTHON="${PYTHON:-python}"
for program in git gh "$PYTHON"; do
  command -v "$program" >/dev/null || fail "$program not found"
done
[ -z "$(git status --porcelain)" ] || fail "commit or stash pending changes first"
branch="$(git symbolic-ref --quiet --short HEAD)" || fail "check out a branch before releasing"
gh auth status >/dev/null
repository="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"

read -r current_version launcher_version <<< "$("$PYTHON" - "$version" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path("app.version").read_text(encoding="utf-8"))
if tuple(map(int, sys.argv[1].split("."))) <= tuple(map(int, data["appVersion"].split("."))):
    sys.exit("error: release version must be newer than appVersion")
print(data["appVersion"], data["launcherVersion"])
PY
)"
[ -n "$current_version" ] || fail "could not read release versions"

# Fetch first so version comparisons use published tags and origin stays current.
git fetch origin --tags
if git show-ref --verify --quiet "refs/tags/$tag"; then
  fail "$tag already exists"
fi
git rev-parse --verify "v$launcher_version^{commit}" >/dev/null \
  || fail "recorded launcher tag v$launcher_version is unavailable"

# Keep this list aligned with launcher.pyw's project imports, including indirect ones.
launcher_files=(launcher.pyw src/configuration.py src/common.py src/app_updates.py)
if git diff --quiet "v$launcher_version" HEAD -- "${launcher_files[@]}"; then
  printf 'Launcher unchanged since v%s; keeping launcherVersion %s\n' "$launcher_version" "$launcher_version"
else
  result=$?
  [ "$result" -eq 1 ] || fail "could not compare launcher files"
  launcher_version="$version"
  printf 'Launcher code changed; setting launcherVersion %s\n' "$launcher_version"
fi

"$PYTHON" - "$version" "$launcher_version" "$repository" <<'PY'
import json
import sys
from pathlib import Path
path = Path("app.version")
data = json.loads(path.read_text(encoding="utf-8"))
data["appVersion"] = sys.argv[1]
data["launcherVersion"] = sys.argv[2]
data["directDownloadURL"] = f"https://github.com/{sys.argv[3]}/archive/refs/tags/v{sys.argv[1]}.zip"
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
PY
git add -- app.version
git commit -m "Release $tag"
commit="$(git rev-parse HEAD)"
git tag -a "$tag" -m "Release $tag"
git push --atomic origin "HEAD:refs/heads/$branch" "refs/tags/$tag"
git fetch origin

# Verify both published refs before asking GitHub to release the existing tag.
remote_branch="$(git ls-remote origin "refs/heads/$branch" | cut -f1)"
remote_tag="$(git ls-remote origin "refs/tags/$tag" | cut -f1)"
[ "$remote_branch" = "$commit" ] || fail "remote branch doesn't match release commit"
[ "$remote_tag" = "$(git rev-parse "$tag")" ] || fail "remote tag doesn't match local release tag"
gh release create "$tag" --repo "$repository" --verify-tag --title "$tag" --generate-notes
printf 'Released %s from %s (launcher %s)\n' "$tag" "$commit" "$launcher_version"
