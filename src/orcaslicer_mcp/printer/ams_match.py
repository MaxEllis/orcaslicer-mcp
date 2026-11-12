"""Does each filament of the plate have a Bambu AMS tray holding what its slot preset says? Pure: takes the
fork's GET /printer/slot_mapping (kind "bambu") and GET /printer/slots replies and adds a `match` per filament,
warnings and a headline. OrcaSlicer's own mapping takes a tray only when the AMS's type word equals the slot
preset's type, so the interesting case is the slot it left without a tray: a tray of the same base material
(LW-ASA / ASA-Aero for an ASA slot, PLA-CF for PLA) is a soft match worth naming, with both names shown."""
from __future__ import annotations
import re

# Support filaments stand in for nothing and nothing stands in for them.
_SUPPORT = frozenset({"PLA-S", "PA-S", "ABS-S"})
# Base materials, longest first so a prefix match takes PETG before PET and PAHT before PA.
_BASES = sorted(("PLA", "PETG", "PCTG", "PET", "ABS", "ASA", "TPU", "TPE", "PA6", "PA11", "PA12", "PAHT", "PPA",
                 "PA", "PC", "PVA", "BVOH", "HIPS", "PPS", "PP", "PE", "PEEK", "PEKK", "PEI", "PVB", "SBS", "EVA"),
                key=len, reverse=True)


def base_material(filament_type) -> str:
    """'LW-ASA', 'ASA-Aero', 'ASA-CF' -> 'ASA'; 'PETG-HF' -> 'PETG'; 'PA6-CF' -> 'PA6'. A support type
    ('PLA-S', 'Sup.PLA') stays its own. Unknown words come back upper-cased; '' stays ''."""
    s = str(filament_type or "").strip().upper()
    if not s:
        return ""
    if s in _SUPPORT or s.startswith("SUP"):
        return s
    tokens = [t for t in re.split(r"[^A-Z0-9]+", s) if t]
    for tok in tokens:
        if tok in _BASES:
            return tok
    for tok in tokens:
        for base in _BASES:
            if tok.startswith(base) and len(base) >= 3:
                return base
    return s


def _tray_text(t: dict) -> str:
    name, ftype = t.get("filament_name") or "", t.get("filament_type") or ""
    if name and ftype and name.upper() != ftype.upper():
        return f"{name} ({ftype})"
    return name or ftype or "an unnamed filament"


def classify(mapping: dict, slots: dict | None) -> dict:
    """The mapping with, per filament, `slot` (1-based, as the sidebar numbers them) and `match`: 'exact' (the
    tray OrcaSlicer chose holds the preset's type), 'soft' (no tray holds the type, but `candidates` hold the same
    base material), 'none' (nothing close is loaded) or 'unknown' (the preset has no type); plus `warnings` and
    `headline` at the top. `slots` is GET /printer/slots (None when it could not be read: then nothing is soft)."""
    out = dict(mapping)
    rows = [dict(f) for f in mapping.get("filaments") or [] if isinstance(f, dict)]
    taken = {(f["tray"].get("ams_id"), f["tray"].get("tray_id")) for f in rows if isinstance(f.get("tray"), dict)}
    loaded = [t for t in ((slots or {}).get("trays") or []) if isinstance(t, dict) and t.get("ready")
              and (t.get("ams_id"), t.get("tray_id")) not in taken]
    warnings: list[str] = []
    counts = {"exact": 0, "soft": 0, "none": 0, "unknown": 0}
    for row in rows:
        index = row.get("index")
        slot = index + 1 if isinstance(index, int) else None
        row["slot"] = slot
        who = f"slot {slot} ({row.get('name')})" if slot is not None else f"'{row.get('name')}'"
        ftype = str(row.get("type") or "")
        tray = row.get("tray")
        if isinstance(tray, dict):
            row["match"] = "exact"
            held = tray.get("filament_name") or ""
            if held and held != row.get("name"):
                row["note"] = (f"{tray.get('label')} holds {held}; the slot's preset is {row.get('name')} "
                               f"(both {tray.get('filament_type') or ftype}).")
        elif not ftype:
            row["match"] = "unknown"
            warnings.append(f"{who} has no filament type in its preset, so no tray can be matched to it.")
        else:
            base = base_material(ftype)
            cands = [t for t in loaded if base_material(t.get("filament_type")) == base]
            if cands:
                row["match"] = "soft"
                row["candidates"] = [{"label": t.get("label"), "ams_id": t.get("ams_id"), "tray_id": t.get("tray_id"),
                                      "filament_type": t.get("filament_type"), "filament_name": t.get("filament_name"),
                                      "filament_color": t.get("filament_color")} for t in cands]
                held = ", ".join(f"{t.get('label')} holds {_tray_text(t)}" for t in cands)
                warnings.append(f"No tray holds {ftype} for {who}. {held}: the same base material ({base}), "
                                "close enough that the Send dialog will take it if you choose it, but check it "
                                "is the filament you meant.")
            else:
                row["match"] = "none"
                warnings.append(f"No tray holds {ftype}, or any other {base}, for {who}: load one, or point the "
                                "slot at a preset for a filament that is loaded.")
        counts[row["match"]] += 1
    out["filaments"] = rows
    out["warnings"] = warnings
    out["headline"] = _headline(len(rows), counts)
    return out


def _headline(n: int, counts: dict) -> str:
    if n == 0:
        return "The plate uses no filament."
    if counts["exact"] == n:
        return (f"Every filament has a tray holding its type." if n > 1
                else "The filament has a tray holding its type.")
    parts = []
    if counts["soft"]:
        parts.append(f"{counts['soft']} with only a near match (same base material, a different filament)")
    if counts["none"]:
        parts.append(f"{counts['none']} with nothing loaded that fits")
    if counts["unknown"]:
        parts.append(f"{counts['unknown']} with no type to match")
    return (f"{counts['exact']} of {n} filaments have a tray holding their type; " + ", ".join(parts)
            + ". The Send dialog will wait for a choice on those.")
