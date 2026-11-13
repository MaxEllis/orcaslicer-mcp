#!/usr/bin/env bash
# One-call release of the OrcaSlicer MCP plugin (plugin/orcaslicer_mcp_plugin.py), the ladder run by hand for 0.2.1 and
# 0.2.2:
#
#   scripts/cut-plugin-release.sh 0.2.3 notes.md            # DRY_RUN=1 prints every change instead of making it
#
# notes.md becomes the GitHub release text and the OrcaCloud changelog (plain text there; check the copy first).
# Steps: bump the plugin's two version markers and LATEST_PLUGIN_VERSION (tests require all three to match), run the
# tests, commit + push main, create the GitHub release plugin-vX.Y.Z with --latest=false (so /releases/latest keeps
# pointing at the orcaslicer-mcp release), wait for publish-orcacloud.yml, then put the new file on the current Latest
# orcaslicer-mcp release, which the README links for downloads. Run it from a clean main that equals origin/main.
set -euo pipefail
VER="${1:?version, e.g. 0.2.3}"
NOTES="$(realpath "${2:?release notes file}")"
REPO=MaxEllis/orcaslicer-mcp
cd "$(dirname "$0")/.."
F=plugin/orcaslicer_mcp_plugin.py
L=src/orcaslicer_mcp/plugin_update.py
run() { if [ "${DRY_RUN:-}" = 1 ]; then echo "  would run: $*"; else "$@"; fi; }
fail() { echo "FAIL: $*" >&2; exit 1; }

[[ "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "version must be X.Y.Z"
[ -s "$NOTES" ] || fail "notes file is empty"
[ "$(git branch --show-current)" = main ] || fail "not on main"
[ -z "$(git status --porcelain)" ] || fail "uncommitted changes"
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || fail "main is not origin/main (pull or push first)"
gh release view "plugin-v$VER" --repo "$REPO" >/dev/null 2>&1 && fail "plugin-v$VER already exists"

# The embedded-page test skips when it can't find the fork checkout (default ../OrcaSlicer), and a skip let 0.2.3's
# run pass without comparing the page. Run it on its own, in dry runs too, and refuse a skip.
out=$(uv run pytest -q -rs plugin/tests/test_embedded.py 2>&1) || { echo "$out" >&2; fail "the embedded panel test failed"; }
if grep -q SKIPPED <<<"$out"; then
  grep SKIPPED <<<"$out" >&2
  fail "the embedded panel test skipped: set MCP_FORK_REPO to the fork checkout (or MCP_PANEL_SRC to its panel.html)"
fi
echo "   the embedded panel page matches the fork"

echo "== 1/6 version markers to $VER =="
if grep -q "^PLUGIN_VERSION = \"$VER\"$" "$F"; then
  echo "   already at $VER"
else
  run sed -i "s/^# version = \"[^\"]*\"$/# version = \"$VER\"/; s/^PLUGIN_VERSION = \"[^\"]*\"$/PLUGIN_VERSION = \"$VER\"/" "$F"
  run sed -i "s/^LATEST_PLUGIN_VERSION = \"[^\"]*\"$/LATEST_PLUGIN_VERSION = \"$VER\"/" "$L"
fi

echo "== 2/6 tests =="
if [ "${DRY_RUN:-}" = 1 ]; then echo "  would run: uv run pytest -q"; else uv run pytest -q; fi

echo "== 3/6 commit + push main =="
if [ "${DRY_RUN:-}" != 1 ] && [ -n "$(git status --porcelain)" ]; then
  git add "$F" "$L"
  git commit -q -m "chore(plugin): $VER"
fi
run git push origin main
SHA=$(git rev-parse HEAD)

echo "== 4/6 GitHub release plugin-v$VER (not Latest) =="
DIST=$(mktemp -d)
cp "$F" "$DIST/orcaslicer_mcp_plugin.py"
(cd "$DIST" && sha256sum orcaslicer_mcp_plugin.py > orcaslicer_mcp_plugin.py.sha256)
run gh release create "plugin-v$VER" --repo "$REPO" --target "$SHA" --latest=false \
  --title "OrcaSlicer MCP plugin $VER" --notes-file "$NOTES" \
  "$DIST/orcaslicer_mcp_plugin.py" "$DIST/orcaslicer_mcp_plugin.py.sha256"

echo "== 5/6 OrcaCloud publish (publish-orcacloud.yml) =="
if [ "${DRY_RUN:-}" = 1 ]; then
  echo "  would wait for the publish-orcacloud.yml run on plugin-v$VER"
else
  run_id=""
  for _ in $(seq 1 30); do
    run_id=$(gh run list --repo "$REPO" --workflow publish-orcacloud.yml --limit 5 \
             --json databaseId,headBranch --jq ".[] | select(.headBranch == \"plugin-v$VER\") | .databaseId" | head -1)
    [ -n "$run_id" ] && break; sleep 5
  done
  [ -n "$run_id" ] || fail "no publish-orcacloud run appeared for plugin-v$VER"
  gh run watch "$run_id" --repo "$REPO" --exit-status >/dev/null || fail "OrcaCloud publish run $run_id failed"
  echo "   run $run_id succeeded"
fi

echo "== 6/6 the plugin file on the Latest orcaslicer-mcp release =="
LATEST=$(gh api "repos/$REPO/releases/latest" --jq .tag_name)
run gh release upload "$LATEST" --repo "$REPO" --clobber \
  "$DIST/orcaslicer_mcp_plugin.py" "$DIST/orcaslicer_mcp_plugin.py.sha256"
BODY=$(mktemp)
gh release view "$LATEST" --repo "$REPO" --json body --jq .body > "$BODY"
if grep -q 'The OrcaSlicer MCP plugin [0-9.]* for stock' "$BODY"; then
  sed -i "s/The OrcaSlicer MCP plugin [0-9.]* for stock/The OrcaSlicer MCP plugin $VER for stock/" "$BODY"
  run gh release edit "$LATEST" --repo "$REPO" --notes-file "$BODY"
fi
rm -rf "$DIST" "$BODY"
[ "${DRY_RUN:-}" = 1 ] && { echo "DRY RUN: nothing was changed."; exit 0; }
echo "DONE: plugin $VER on GitHub (plugin-v$VER) and OrcaCloud; $LATEST carries the file."
echo "Check the listing shows $VER: https://cloud.orcaslicer.com/p/16d116a6bc41"
