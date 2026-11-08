"""Merge two extracted settings schemas (scripts/extract_settings_schema.py output) into the
bundled one, so describe_setting and search_settings serve users of both fork lines.

The newer schema is the base. A key only the older one has stays, marked "removed_in"; a key
only the newer one has is marked "since"; when a choice list differs, the older list is kept
as "enum_values_<older>" next to the newer one.

    python scripts/merge_settings_schemas.py OLD.json 2.4.2 NEW.json 2.5.0 [--out PATH]
"""
import argparse
import json
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src/orcaslicer_mcp/data/print_settings_schema.json"


def merge(old: dict, old_ver: str, new: dict, new_ver: str) -> dict:
    ko = {k for k in old if not k.startswith("_")}
    kn = {k for k in new if not k.startswith("_")}
    out = {}
    for k in sorted(ko | kn):
        if k in kn:
            rec = dict(new[k])
            if k not in ko:
                rec["since"] = new_ver
            elif (old[k].get("enum_values") or []) != (rec.get("enum_values") or []):
                rec["enum_values_" + old_ver.replace(".", "_")] = old[k].get("enum_values") or []
        else:
            rec = dict(old[k])
            rec["removed_in"] = new_ver
        out[k] = rec
    meta = {
        "source": "PrintConfig.cpp",
        "versions": {old_ver: old.get("_meta", {}), new_ver: new.get("_meta", {})},
        "setting_count": len(out),
        "with_label": sum(1 for r in out.values() if r.get("label")),
        "with_label_and_tooltip": sum(1 for r in out.values() if r.get("label") and r.get("tooltip")),
        "since_count": sum(1 for r in out.values() if "since" in r),
        "removed_count": sum(1 for r in out.values() if "removed_in" in r),
    }
    return {"_meta": meta, **out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old", type=Path)
    ap.add_argument("old_version")
    ap.add_argument("new", type=Path)
    ap.add_argument("new_version")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    doc = merge(json.loads(a.old.read_text(encoding="utf-8")), a.old_version,
                json.loads(a.new.read_text(encoding="utf-8")), a.new_version)
    a.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    m = doc["_meta"]
    print(f"{m['setting_count']} settings ({m['since_count']} since {a.new_version}, "
          f"{m['removed_count']} removed in {a.new_version}) -> {a.out}")


if __name__ == "__main__":
    main()
