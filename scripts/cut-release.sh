#!/usr/bin/env bash
# One-call orcaslicer-mcp release. Proven by hand 5x (0.1.1-0.1.6); this is that ladder.
#
#   UV_PUBLISH_TOKEN=pypi-... scripts/cut-release.sh 0.1.7
#
# The token must be project-scoped and must arrive out-of-band (Taildrop a plain
# .token file) - never paste it into a chat or commit it.
set -euo pipefail
VER="${1:?version, e.g. 0.1.7}"
cd "$(dirname "$0")/.."
: "${UV_PUBLISH_TOKEN:?set UV_PUBLISH_TOKEN (project-scoped PyPI token)}"

echo "== 1/8 bumping the version markers to $VER =="
sed -i "s/^version = \".*\"/version = \"$VER\"/" pyproject.toml
sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"$VER\"/g" server.json mcpb/manifest.json lhm.plugin.json

# All MUST agree or the extension/registry/marketplace ship mismatched metadata.
# Anchored so fields like manifest_version don't leak in. lhm.plugin.json carries
# two version fields (top-level + artifacts.pypi.version); both bump to $VER.
mapfile -t found < <({ grep -ho '^version = "[^"]*"' pyproject.toml; \
                       grep -ho '^ *"version": "[^"]*"' server.json mcpb/manifest.json lhm.plugin.json; } \
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
git add pyproject.toml server.json mcpb/manifest.json lhm.plugin.json uv.lock
git commit -m "chore: release $VER" || echo "   (nothing to commit)"
git tag "v$VER" && git push origin HEAD "v$VER"

echo "== 6/8 pack the Claude Desktop extension =="
npx --yes @anthropic-ai/mcpb pack mcpb
mv mcpb.mcpb "orcaslicer-mcp-$VER.mcpb"
sha256sum "orcaslicer-mcp-$VER.mcpb" > "orcaslicer-mcp-$VER.mcpb.sha256"

echo "== 7/8 GitHub release =="
gh release create "v$VER" --title "v$VER" --generate-notes \
  "orcaslicer-mcp-$VER.mcpb" "orcaslicer-mcp-$VER.mcpb.sha256"

echo "== 8/8 MCP registry =="
# The registry JWT expires in minutes, so login+publish must be one step.
# login is an INTERACTIVE device flow - headless runs stop here, deliberately.
if command -v mcp-publisher >/dev/null; then
  mcp-publisher login github && mcp-publisher publish
else
  echo "SKIPPED: mcp-publisher not installed - registry still advertises the previous version." >&2
fi

rm -f "orcaslicer-mcp-$VER.mcpb" "orcaslicer-mcp-$VER.mcpb.sha256"
echo "DONE: $VER on PyPI, GitHub, and the MCP registry"
echo "Reminder: LobeHub marketplace does NOT auto-rescan. To re-validate the listing, run"
echo "  npx -y @lobehub/market-cli plugin publish   (from repo root, on a machine with a browser login)"
