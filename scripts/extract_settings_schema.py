#!/usr/bin/env python3
"""Extract OrcaSlicer setting definitions from PrintConfig.cpp into a bundled JSON."""
from __future__ import annotations
import argparse, datetime, json, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from orcaslicer_mcp.schema_extract import parse_print_config  # noqa: E402

DEFAULT_SRC = Path("/home/max/projects/3d-printer/orca-relay/src/libslic3r/PrintConfig.cpp")
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src/orcaslicer_mcp/data/print_settings_schema.json"


def git_sha(src: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(src.parent), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--min-count", type=int, default=600)
    ap.add_argument("--date", default=None, help="override extraction date (YYYY-MM-DD)")
    args = ap.parse_args()

    text = args.src.read_text(encoding="utf-8", errors="replace")
    settings, unparsed = parse_print_config(text)
    with_label = sum(1 for r in settings.values() if r.get("label"))
    with_tip = sum(1 for r in settings.values() if r.get("label") and r.get("tooltip"))
    meta = {
        "source": args.src.name,
        "source_git_sha": git_sha(args.src),
        "extracted": args.date or datetime.date.today().isoformat(),
        "setting_count": len(settings),
        "with_label": with_label,
        "with_label_and_tooltip": with_tip,
        "unparsed_keys": sorted(unparsed),
    }
    doc = {"_meta": meta}
    doc.update(dict(sorted(settings.items())))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    cov = (with_tip / len(settings) * 100) if settings else 0.0
    print(f"parsed {len(settings)} settings; label+tooltip {with_tip} ({cov:.1f}%); "
          f"unparsed {len(unparsed)} -> {args.out}")
    if len(settings) < args.min_count:
        print(f"ERROR: only {len(settings)} settings (< {args.min_count})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
