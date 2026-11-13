#!/usr/bin/env bash
# One-call orcaslicer-mcp release. Proven by hand 5x (0.1.1-0.1.6); this is that ladder.
# Needs: a dated CHANGELOG [VER] section and a released plugin-v<LATEST_PLUGIN_VERSION> (both checked first).
#
#   UV_PUBLISH_TOKEN=pypi-... scripts/cut-release.sh 0.1.7
#
# The token must be project-scoped and must arrive out-of-band (Taildrop a plain
# .token file) - never paste it into a chat or commit it.
set -euo pipefail
VER="${1:?version, e.g. 0.1.7}"
cd "$(dirname "$0")/.."
: "${UV_PUBLISH_TOKEN:?set UV_PUBLISH_TOKEN (project-scoped PyPI token)}"

echo "== 0/8 release notes and plugin file (checked before anything is published) =="
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
# The GitHub release body is the changelog's [$VER] section: --generate-notes gave only a compare link (and once
# @-mentioned a contributor), so every release since 0.1.18 had its body replaced by hand.
awk -v v="$VER" 'index($0, "## [" v "]") == 1 {f = 1; next} f && /^## \[/ {exit} f' CHANGELOG.md \
  | sed -e '/./,$!d' > "$WORK/notes.md"
[ -s "$WORK/notes.md" ] || { echo "FAIL: CHANGELOG.md has no dated '## [$VER]' section with text" >&2; exit 1; }
echo "   notes: $(wc -l < "$WORK/notes.md") lines from CHANGELOG.md [$VER]"
# README links /releases/latest/download/orcaslicer_mcp_plugin.py, so this release (it becomes Latest) carries the
# plugin version the connector announces. Release the plugin first (cut-plugin-release.sh), then the connector.
PLUGIN_VER=$(sed -n 's/^LATEST_PLUGIN_VERSION = "\(.*\)"/\1/p' src/orcaslicer_mcp/plugin_update.py)
[ -n "$PLUGIN_VER" ] || { echo "FAIL: LATEST_PLUGIN_VERSION not found in src/orcaslicer_mcp/plugin_update.py" >&2; exit 1; }
gh release download "plugin-v$PLUGIN_VER" -D "$WORK" -p orcaslicer_mcp_plugin.py -p orcaslicer_mcp_plugin.py.sha256 \
  || { echo "FAIL: no plugin-v$PLUGIN_VER release to attach (cut-plugin-release.sh $PLUGIN_VER first)" >&2; exit 1; }
(cd "$WORK" && sha256sum -c --quiet orcaslicer_mcp_plugin.py.sha256)
echo "   plugin $PLUGIN_VER from plugin-v$PLUGIN_VER, checksum OK"

echo "== 1/8 bumping the version markers to $VER =="
sed -i "s/^version = \".*\"/version = \"$VER\"/" pyproject.toml
sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"$VER\"/g" packaging/server.json packaging/mcpb/manifest.json packaging/lhm.plugin.json

# All MUST agree or the extension/registry/marketplace ship mismatched metadata.
# Anchored so fields like manifest_version don't leak in. lhm.plugin.json carries
# two version fields (top-level + artifacts.pypi.version); both bump to $VER.
mapfile -t found < <({ grep -ho '^version = "[^"]*"' pyproject.toml; \
                       grep -ho '^ *"version": "[^"]*"' packaging/server.json packaging/mcpb/manifest.json packaging/lhm.plugin.json; } \
                     | grep -o '[0-9][^"]*' | sort -u)
if [ "${#found[@]}" -ne 1 ] || [ "${found[0]}" != "$VER" ]; then
  echo "FAIL: version markers disagree: ${found[*]}" >&2; exit 1
fi
echo "   all markers at $VER"

echo "== 2/8 tests =="
uv run pytest -q

echo "== 3/8 build =="
rm -rf dist && uv build

echo "== 3b: cold-install gate (a fresh uvx sees published deps, not the locked venv) =="
# Step 2 tests the LOCKED venv; a bad dependency spec (e.g. mcp 2.0.0 dropping
# mcp.server.fastmcp) only breaks a clean install. Import the just-built wheel in an
# isolated env with ONLY published deps -- this is what directory validators do.
WHEEL=$(ls dist/*.whl)
env -u ORCA_API_TOKEN -u ORCA_API_URL VIRTUAL_ENV= timeout 180 \
  uvx --isolated --refresh --from "$WHEEL" python -c "from orcaslicer_mcp import server; assert server.mcp" \
  || { echo "FAIL: wheel does not import on a clean install (the mcp<2 class of bug)" >&2; exit 1; }
echo "   clean import OK"

echo "== 4/8 publish to PyPI =="
uv publish

echo "== 5/8 commit + tag =="
git add pyproject.toml packaging/server.json packaging/mcpb/manifest.json packaging/lhm.plugin.json uv.lock
git commit -m "chore: release $VER" || echo "   (nothing to commit)"
git tag "v$VER" && git push origin HEAD "v$VER"

echo "== 6/8 pack the Claude Desktop extension =="
npx --yes @anthropic-ai/mcpb pack packaging/mcpb "orcaslicer-mcp-$VER.mcpb"
sha256sum "orcaslicer-mcp-$VER.mcpb" > "orcaslicer-mcp-$VER.mcpb.sha256"

echo "== 7/8 GitHub release (notes = changelog section, plugin $PLUGIN_VER attached) =="
gh release create "v$VER" --title "v$VER" --notes-file "$WORK/notes.md" --latest \
  "orcaslicer-mcp-$VER.mcpb" "orcaslicer-mcp-$VER.mcpb.sha256" \
  "$WORK/orcaslicer_mcp_plugin.py" "$WORK/orcaslicer_mcp_plugin.py.sha256"

echo "== 8/8 MCP registry =="
# .github/workflows/publish-mcp-registry.yml publishes packaging/server.json through GitHub OIDC when the release
# above is created; nothing to log in to here.
echo "   check it with: gh run list -w publish-mcp-registry.yml -L 1"

rm -f "orcaslicer-mcp-$VER.mcpb" "orcaslicer-mcp-$VER.mcpb.sha256"
echo "DONE: $VER on PyPI and GitHub (Latest, with plugin $PLUGIN_VER); the registry workflow follows the release"
echo "PyPI's simple index lags ~5 min: a cold 'uvx orcaslicer-mcp@$VER' may say the version is missing until then."
echo "LobeHub does NOT rescan by itself: npx -y @lobehub/market-cli plugin update --dir packaging (needs its login)"
