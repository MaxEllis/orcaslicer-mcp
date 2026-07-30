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

echo "== 1/8 bumping the three version markers to $VER =="
sed -i "s/^version = \".*\"/version = \"$VER\"/" pyproject.toml
sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"$VER\"/g" server.json mcpb/manifest.json

# All three MUST agree or the extension/registry ship mismatched metadata.
# Anchored so fields like manifest_version don't leak in.
mapfile -t found < <({ grep -ho '^version = "[^"]*"' pyproject.toml; \
                       grep -ho '^ *"version": "[^"]*"' server.json mcpb/manifest.json; } \
                     | grep -o '[0-9][^"]*' | sort -u)
if [ "${#found[@]}" -ne 1 ] || [ "${found[0]}" != "$VER" ]; then
  echo "FAIL: version markers disagree: ${found[*]}" >&2; exit 1
fi
echo "   all markers at $VER"

echo "== 2/8 tests =="
uv run pytest -q

echo "== 3/8 build =="
rm -rf dist && uv build

echo "== 4/8 publish to PyPI =="
uv publish

echo "== 5/8 commit + tag =="
git add pyproject.toml server.json mcpb/manifest.json uv.lock
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
mcp-publisher login github && mcp-publisher publish

rm -f "orcaslicer-mcp-$VER.mcpb" "orcaslicer-mcp-$VER.mcpb.sha256"
echo "DONE: $VER on PyPI, GitHub, and the MCP registry"
