"""Deterministic profile checks. Formulas are normative in knowledge/physics/."""
from __future__ import annotations
import math
from dataclasses import dataclass

@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # pass | warn | fail
    detail: str

def _f(cfg: dict, key: str) -> float | None:
    v = cfg.get(key)
    if v in (None, ""):
        return None
    s = str(v)
    if "," in s:  # per-extruder vector like "80,80" -> use first element
        s = s.split(",", 1)[0].strip()
    try:
        return float(s.rstrip("%"))
    except ValueError:
        return None


def _width(cfg: dict, key: str, nozzle: float | None) -> float | None:
    """Line-width keys are float-or-percent; percent means percent OF NOZZLE
    DIAMETER, and 0 means "auto" (not a literal zero-width line). Returns
    None for missing/unparseable/auto values so callers treat them as
    missing data rather than nonsensical widths."""
    v = cfg.get(key)
    if v in (None, ""):
        return None
    s = str(v).strip()
    if s.endswith("%"):
        if nozzle is None:
            return None
        try:
            pct = float(s[:-1])
        except ValueError:
            return None
        value = pct / 100 * nozzle
    else:
        try:
            value = float(s)
        except ValueError:
            return None
    return value if value > 0 else None

def cross_section(line_width: float, layer_height: float) -> float:
    return (line_width - layer_height * (1 - math.pi / 4)) * layer_height

_FEATURES = [  # (speed key, line-width key)
    ("outer_wall_speed", "outer_wall_line_width"),
    ("inner_wall_speed", "inner_wall_line_width"),
    ("sparse_infill_speed", "sparse_infill_line_width"),
    ("internal_solid_infill_speed", "internal_solid_infill_line_width"),
    ("top_surface_speed", "top_surface_line_width"),
    ("initial_layer_speed", "initial_layer_line_width"),
    ("gap_infill_speed", "line_width"),
    ("bridge_speed", "line_width"),
]

_TEMP_RULES = {"PLA": (195.0, 1.2), "PETG": (220.0, 1.4), "ABS": (225.0, 1.4), "ASA": (225.0, 1.4)}

def predicted_flows(cfg: dict) -> dict[str, float]:
    """Per-feature demanded volumetric flow (mm3/s): speed * cross_section(width, layer_height).
    Key is the speed key without its '_speed' suffix. Skips features missing speed/width/layer_height."""
    lh = _f(cfg, "layer_height")
    nd = _f(cfg, "nozzle_diameter")
    default_w = _width(cfg, "line_width", nd)
    if lh is None:
        return {}
    out: dict[str, float] = {}
    for speed_k, width_k in _FEATURES:
        sp = _f(cfg, speed_k)
        w = _width(cfg, width_k, nd)
        w = default_w if w is None else w
        if sp is None or w is None:
            continue
        out[speed_k.removesuffix("_speed")] = sp * cross_section(w, lh)
    return out

def run_checks(cfg: dict[str, str]) -> list[CheckResult]:
    out: list[CheckResult] = []
    lh, nd = _f(cfg, "layer_height"), _f(cfg, "nozzle_diameter")
    lw = _width(cfg, "line_width", nd)
    ceil = _f(cfg, "filament_max_volumetric_speed")

    # flow_ceiling
    if lh is None or ceil is None:
        out.append(CheckResult("flow_ceiling", "warn", "insufficient data (layer_height / filament_max_volumetric_speed)"))
        max_flow = None
    else:
        flows = predicted_flows(cfg)
        if not flows:
            out.append(CheckResult("flow_ceiling", "warn", "insufficient data (no feature speeds)"))
            max_flow = None
        else:
            worst_name = max(flows, key=flows.get)
            max_flow = flows[worst_name]
            sp = _f(cfg, worst_name + "_speed")
            w = _width(cfg, dict(_FEATURES)[worst_name + "_speed"], nd)
            w = lw if w is None else w
            worst = f"{worst_name} {sp:g}mm/s x {w:g}mm = {max_flow:.1f}mm3/s" if max_flow > 0 else "zero flow demand (all found feature speeds are 0)"
            status = "fail" if max_flow > ceil else "pass"
            out.append(CheckResult("flow_ceiling", status, f"peak demand {worst}; ceiling {ceil:g}mm3/s"))

    # temp_vs_flow
    mat = (cfg.get("filament_type") or "").upper()
    temp = _f(cfg, "nozzle_temperature")
    if mat in _TEMP_RULES and temp is not None and max_flow is not None:
        base, slope = _TEMP_RULES[mat]
        sustainable = max(0.0, (temp - base) / slope)
        status = "fail" if max_flow > sustainable else "pass"
        out.append(CheckResult("temp_vs_flow", status,
            f"{mat}@{temp:g}C sustains ~{sustainable:.1f}mm3/s; profile demands {max_flow:.1f}mm3/s"))
    else:
        out.append(CheckResult("temp_vs_flow", "warn", "insufficient data (filament_type/temp/flow)"))

    # layer_height_ratio: fail outside (0.1mm .. 0.8*nozzle), warn outside (0.25..0.65)*nozzle
    if lh is not None and nd is not None:
        r = lh / nd
        status = "fail" if (lh < 0.1 or r > 0.8) else ("warn" if (r < 0.25 or r > 0.65) else "pass")
        out.append(CheckResult("layer_height_ratio", status, f"layer {lh:g}mm = {r:.0%} of {nd:g}mm nozzle (sane 25-65%, hard max 80%)"))
    else:
        out.append(CheckResult("layer_height_ratio", "warn", "insufficient data"))

    # line_width_ratio: warn outside 0.85..1.5x nozzle, fail outside 0.6..2.0x
    if lw is not None and nd is not None:
        r = lw / nd
        status = "fail" if (r < 0.6 or r > 2.0) else ("warn" if (r < 0.85 or r > 1.5) else "pass")
        out.append(CheckResult("line_width_ratio", status, f"line {lw:g}mm = {r:.2f}x nozzle (sane 0.85-1.5x)"))
    else:
        out.append(CheckResult("line_width_ratio", "warn", "insufficient data"))

    # retraction_range: warn >3mm (bowden-length on likely-DD) or ==0; fail >8mm.
    # F10: when use_firmware_retraction is on, Orca's retraction_length is IGNORED at
    # slice time - the printer firmware value governs - so the Orca number is not
    # authoritative and must not read as pass (this masked a real 0.3mm-firmware vs
    # 1mm-Orca mismatch last run).
    ret = _f(cfg, "retraction_length")
    fw_ret = _f(cfg, "use_firmware_retraction")
    if fw_ret is not None and fw_ret >= 1:
        orca = f"{ret:g}mm" if ret is not None else "unset"
        out.append(CheckResult("retraction_range", "warn",
            f"use_firmware_retraction=on: Orca retraction_length ({orca}) is IGNORED; effective "
            f"retraction is set in printer firmware - verify there (e.g. Klipper SET_RETRACTION)"))
    elif ret is not None:
        status = "fail" if ret > 8 else ("warn" if (ret > 3 or ret == 0) else "pass")
        out.append(CheckResult("retraction_range", status, f"retraction {ret:g}mm (DD sane 0.2-2, bowden 2-7)"))
    else:
        out.append(CheckResult("retraction_range", "warn", "insufficient data"))

    # cooling_sanity
    fmin, fmax, sdlt = _f(cfg, "fan_min_speed"), _f(cfg, "fan_max_speed"), _f(cfg, "slow_down_layer_time")
    if fmin is not None and fmax is not None:
        if fmin > fmax:
            out.append(CheckResult("cooling_sanity", "fail", f"fan_min {fmin:g} > fan_max {fmax:g}"))
        elif sdlt is not None and sdlt < 3:
            out.append(CheckResult("cooling_sanity", "warn", f"slow_down_layer_time {sdlt:g}s < 3s risks molten stacking on small parts"))
        else:
            out.append(CheckResult("cooling_sanity", "pass", "fan range and layer-time slowdown sane"))
    elif sdlt is not None:
        if sdlt < 3:
            out.append(CheckResult("cooling_sanity", "warn", f"slow_down_layer_time {sdlt:g}s < 3s risks molten stacking on small parts"))
        else:
            out.append(CheckResult("cooling_sanity", "pass", "layer-time slowdown sane (fan data missing)"))
    else:
        out.append(CheckResult("cooling_sanity", "warn", "insufficient data"))

    # first_layer: initial height must be <= 0.8*nozzle and >= layer_height*0.75 is NOT required; just ratio cap
    ilh = _f(cfg, "initial_layer_print_height")
    if ilh is not None and nd is not None:
        status = "fail" if ilh / nd > 0.8 else "pass"
        out.append(CheckResult("first_layer_height", status, f"first layer {ilh:g}mm vs nozzle {nd:g}mm (max 80%)"))
    else:
        out.append(CheckResult("first_layer_height", "warn", "insufficient data"))

    # initial_layer_temp (F11): a first-layer nozzle temp BELOW the bulk temp hurts
    # first-layer adhesion (the 215-first / 230-bulk mismatch a human caught last run).
    t_bulk = _f(cfg, "nozzle_temperature")
    t_init = _f(cfg, "nozzle_temperature_initial_layer")
    if t_bulk is not None and t_init is not None:
        if t_init < t_bulk:
            out.append(CheckResult("initial_layer_temp", "warn",
                f"first-layer nozzle {t_init:g}C < bulk {t_bulk:g}C; first layer is usually equal-or-hotter for adhesion"))
        else:
            out.append(CheckResult("initial_layer_temp", "pass",
                f"first-layer nozzle {t_init:g}C >= bulk {t_bulk:g}C"))
    else:
        out.append(CheckResult("initial_layer_temp", "warn", "insufficient data (nozzle_temperature/initial_layer)"))
    return out
