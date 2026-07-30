#!/usr/bin/env bash
#
# Migrate workspace-mcp-fixed -> Otblakee/otb-workspace-mcp (§7.2 of
# SECURITY_AUDIT_AND_ROLLOUT_PLAN.md).
#
# Run this locally, from a clone of workspace-mcp-fixed, with your own git
# credentials. It does two things, as two separate commits-worth of change:
#
#   1. Pushes the full existing history to the new remote — a pure move, no
#      content change, so blame and history survive intact.
#   2. Makes ONE cleanup commit removing the upstream distribution artifacts
#      that have no place in a private internal repo.
#
# It is deliberately conservative: it refuses to run if the new remote already
# has commits, and it stops on the first error.
#
# Usage:
#   bash scripts/migrate-to-otb-workspace-mcp.sh            # do it
#   DRY_RUN=1 bash scripts/migrate-to-otb-workspace-mcp.sh  # show, don't touch

set -euo pipefail

NEW_REMOTE_URL="https://github.com/Otblakee/otb-workspace-mcp"
NEW_REMOTE_NAME="otb"
DRY_RUN="${DRY_RUN:-}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
run() {
    if [[ -n "$DRY_RUN" ]]; then
        printf '   [dry-run] %s\n' "$*"
    else
        printf '   + %s\n' "$*"
        eval "$@"
    fi
}

# --- sanity checks --------------------------------------------------------

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
    echo "Not inside a git work tree. cd into your workspace-mcp-fixed clone first." >&2
    exit 1
}

if [[ -n "$(git status --porcelain)" ]]; then
    echo "Working tree is dirty. Commit or stash first — this script makes a commit." >&2
    exit 1
fi

say "Checking the destination is reachable and empty"
# Distinguish "unreachable" from "empty" explicitly. Piping straight to grep
# would treat a 403/404/typo as an empty repo and happily proceed, which is
# exactly the wrong call — an auth failure must stop the run, not look like
# a green light.
if ! remote_heads="$(git ls-remote --heads "$NEW_REMOTE_URL" 2>&1)"; then
    echo "ERROR: cannot reach $NEW_REMOTE_URL" >&2
    echo "" >&2
    echo "$remote_heads" >&2
    echo "" >&2
    echo "Check the URL, that the repo exists, and that you are authenticated" >&2
    echo "(gh auth status). NOT proceeding — an unreachable remote is not an" >&2
    echo "empty one." >&2
    exit 1
fi
if [[ -n "$remote_heads" ]]; then
    echo "ERROR: $NEW_REMOTE_URL already has branches:" >&2
    echo "$remote_heads" >&2
    echo "" >&2
    echo "This script is for the initial migration only. If you initialised the" >&2
    echo "repo with a README/licence, delete and recreate it empty." >&2
    exit 1
fi
echo "   reachable, and empty — good"

# --- step 1: move the history --------------------------------------------

say "Step 1 — push full history (pure move, no content change)"
git remote remove "$NEW_REMOTE_NAME" 2>/dev/null || true
run "git remote add $NEW_REMOTE_NAME $NEW_REMOTE_URL"
run "git push $NEW_REMOTE_NAME --all"
run "git push $NEW_REMOTE_NAME --tags"

# --- step 2: cleanup commit ----------------------------------------------
#
# Everything below is upstream's public-distribution machinery. On a private
# repo holding this organisation's security configuration it is at best noise,
# and publish-mcp-registry.yml is an active footgun: it publishes to PyPI and
# the MCP Registry. It has never run in this fork, but dormant is not removed.

say "Step 2 — remove upstream distribution artifacts"

ARTIFACTS=(
    ".github/workflows/publish-mcp-registry.yml"  # publishes to PyPI + MCP Registry
    "smithery.yaml"                               # Smithery listing
    "glama.json"                                  # Glama listing
    "manifest.json"                               # DXT manifest
    "google_workspace_mcp.dxt"                    # 1.4 MB prebuilt bundle
    "server.json"                                 # MCP registry metadata
    "README_NEW.md"                               # stale duplicate README
)

for f in "${ARTIFACTS[@]}"; do
    if [[ -e "$f" ]]; then
        run "git rm -q --ignore-unmatch '$f'"
    else
        echo "   (already absent: $f)"
    fi
done

cat <<'NOTE'

   NOTE — left in place, decide separately:
     .github/workflows/docker-publish.yml
        Pushes images to GHCR. Keep it if you want private image builds;
        retarget or delete it if not. Not removed here because it may be
        load-bearing for your Render deploy.
     helm-chart/
        Unused on Render, but harmless and possibly useful later.

NOTE

if [[ -z "$DRY_RUN" ]]; then
    git commit -q -F - <<'MSG'
chore: remove upstream distribution artifacts

This fork is now a private internal deployment, not a published package.
The removed files are upstream's public-distribution machinery and have no
role here.

publish-mcp-registry.yml is the one that matters: it publishes to PyPI and
the MCP Registry. It has never executed in this fork, but a workflow that
publishes a repository containing an organisation's security configuration
should not be sitting in the tree waiting for someone to enable Actions.

Also drops google_workspace_mcp.dxt, a 1.4 MB prebuilt bundle checked into
source control, and README_NEW.md, a stale duplicate.

Left alone deliberately: docker-publish.yml, which may be load-bearing for
the Render deploy, and helm-chart/, which is unused but harmless.
MSG
    echo "   committed"
fi

say "Step 3 — push the cleanup"
run "git push $NEW_REMOTE_NAME HEAD"

# --- next steps -----------------------------------------------------------

cat <<'NEXT'

Done. Remaining, by hand:

  1. Rename the upstream remote so nobody merges from it by accident:
       git remote rename origin upstream
       git remote set-url --push upstream DISABLED
     Upstream changes must be reviewed individually — BLOCKED_TOOLS is a
     denylist, so a routine merge can silently register new destructive tools.

  2. Update pyproject.toml [project].name and the README title to
     otb-workspace-mcp. Left to you because the package name affects
     get_package_version() in core/server.py, which falls back through
     "workspace-mcp-fixed" then "workspace-mcp" — add the new name there in
     the same change or /health starts reporting "dev".

  3. Point Render at the new repo, redeploy, confirm /health.

  4. Archive Otblakee/workspace-mcp-fixed once the new repo is proven.

NEXT
