"""Does OrcaSlicer's profile match the printer it will print on? Pure comparisons of the live
OrcaSlicer config (string values, as GET /config returns them) against Klipper's configfile
settings. Warnings only: nothing is ever blocked. Missing data gives 'unknown', never a guess."""
from __future__ import annotations

# Only plain-number settings: the percentage-based ones (bridge/sparse-infill/internal-solid-infill
# acceleration, small-perimeter speed, ...) are relative to these and add nothing.
ACCEL_KEYS = ("default_acceleration", "outer_wall_acceleration", "inner_wall_acceleration",
              "top_surface_acceleration", "travel_acceleration", "initial_layer_acceleration")
SPEED_KEYS = ("outer_wall_speed", "inner_wall_speed", "sparse_infill_speed", "internal_solid_infill_speed",
              "top_surface_speed", "gap_infill_speed", "support_speed", "support_interface_speed",
              "bridge_speed", "travel_speed", "initial_layer_speed", "initial_layer_infill_speed",
              "ironing_speed", "skirt_speed")
# curr_bed_type (compared case-insensitively) -> the bed temperature setting OrcaSlicer prints with.
BED_TEMP_KEYS = {"cool plate": "cool_plate_temp", "engineering plate": "eng_plate_temp",
                 "high temp plate": "hot_plate_temp", "textured pei plate": "textured_plate_temp",
                 "textured cool plate": "textured_cool_plate_temp", "supertack plate": "supertack_plate_temp"}
PROFILE_KEYS = ("nozzle_diameter", "printable_area", "printable_height", *ACCEL_KEYS, *SPEED_KEYS,
                "nozzle_temperature", "nozzle_temperature_initial_layer", "curr_bed_type",
                *BED_TEMP_KEYS.values(), *(key + "_initial_layer" for key in BED_TEMP_KEYS.values()),
                "use_firmware_retraction", "retraction_length")
TOLERANCE_MM = 0.5


def floats(v) -> list[float]:
    """'0.4' -> [0.4]; '220,230' (a per-filament list) -> [220.0, 230.0]; 'nil' or '' -> []."""
    if v is None:
        return []
    if isinstance(v, (int, float)):
        return [float(v)]
    if isinstance(v, (list, tuple)):
        return [f for x in v for f in floats(x)]
    out = []
    for part in str(v).split(","):
        try:
            out.append(float(part.strip()))
        except ValueError:
            pass
    return out


def points(v) -> list[tuple[float, float]]:
    """'0x0,235x0,235x235' -> [(0, 0), (235, 0), (235, 235)]."""
    pts = []
    for part in str(v or "").split(","):
        a, sep, b = part.partition("x")
        if not sep:
            continue
        try:
            pts.append((float(a), float(b)))
        except ValueError:
            pass
    return pts


def _check(name, status, profile, printer, why) -> dict:
    return {"check": name, "status": status, "profile": profile, "printer": printer, "why": why}


def _unknown(name, why="Not enough data to compare.") -> dict:
    return _check(name, "unknown", None, None, why)


def check_nozzle(cfg: dict, settings: dict) -> dict:
    p, k = floats(cfg.get("nozzle_diameter")), floats((settings.get("extruder") or {}).get("nozzle_diameter"))
    if not p or not k:
        return _unknown("nozzle")
    if abs(p[0] - k[0]) < 0.01:
        return _check("nozzle", "ok", p[0], k[0], f"Both say {p[0]:g} mm.")
    return _check("nozzle", "warn", p[0], k[0],
                  f"OrcaSlicer slices for a {p[0]:g} mm nozzle but Klipper's config says {k[0]:g} mm. Update "
                  "whichever is stale (Klipper's value is often left at the old size after a nozzle change).")


def check_bed(cfg: dict, settings: dict) -> dict:
    pts = points(cfg.get("printable_area"))
    sx, sy, sz = ((settings.get(k) or {}) for k in ("stepper_x", "stepper_y", "stepper_z"))
    lims = [floats(sx.get("position_min")), floats(sx.get("position_max")),
            floats(sy.get("position_min")), floats(sy.get("position_max"))]
    if not pts or not all(lims):
        return _unknown("bed", "Not enough data to compare (Klipper reports no X/Y travel limits, "
                               "for example on a delta printer).")
    xmin, xmax = min(p[0] for p in pts), max(p[0] for p in pts)
    ymin, ymax = min(p[1] for p in pts), max(p[1] for p in pts)
    kx0, kx1, ky0, ky1 = (lim[0] for lim in lims)
    height, zmax = floats(cfg.get("printable_height")), floats(sz.get("position_max"))
    issues = []
    if xmin < kx0 - TOLERANCE_MM or xmax > kx1 + TOLERANCE_MM:
        issues.append(f"X {xmin:g} to {xmax:g} mm vs travel {kx0:g} to {kx1:g} mm")
    if ymin < ky0 - TOLERANCE_MM or ymax > ky1 + TOLERANCE_MM:
        issues.append(f"Y {ymin:g} to {ymax:g} mm vs travel {ky0:g} to {ky1:g} mm")
    if height and zmax and height[0] > zmax[0] + TOLERANCE_MM:
        issues.append(f"height {height[0]:g} mm vs Z travel {zmax[0]:g} mm")
    profile = {"x": [xmin, xmax], "y": [ymin, ymax], "height": height[0] if height else None}
    printer = {"x": [kx0, kx1], "y": [ky0, ky1], "z_max": zmax[0] if zmax else None}
    if issues:
        return _check("bed", "warn", profile, printer,
                      "The profile's printable space goes past the printer's travel: " + "; ".join(issues)
                      + ". Moves there fail with 'Move out of range'.")
    return _check("bed", "ok", profile, printer, "The profile's printable space fits the printer's travel.")


def _highest(cfg: dict, keys) -> tuple[str | None, float | None]:
    best_key, best = None, None
    for key in keys:
        vals = [v for v in floats(cfg.get(key)) if v > 0]
        if vals and (best is None or max(vals) > best):
            best_key, best = key, max(vals)
    return best_key, best


def _accel_warning(best: float, key: str, lim: float, unit: str) -> str:
    # Klipper accepts a higher acceleration when the G-code asks for it (M204, SET_VELOCITY_LIMIT), so
    # the risk is the printer accelerating harder than its configured limit, not slower prints.
    return (f"The profile asks for up to {best:g} {unit} ({key}) but Klipper's max_accel is {lim:g} {unit}. "
            "Klipper accepts the higher value when the G-code sets it (M204 or SET_VELOCITY_LIMIT), so the "
            "printer will accelerate harder than its configured limit. Check it can take that, or lower the "
            "profile's acceleration.")


def _speed_warning(best: float, key: str, lim: float, unit: str) -> str:
    return (f"Klipper caps speed at {lim:g} {unit} but the profile asks for up to {best:g} {unit} ({key}). "
            "Unless the G-code raises the limit with SET_VELOCITY_LIMIT, Klipper slows those moves down, "
            "so prints take longer than OrcaSlicer estimates.")


_LIMIT_WARNINGS = {"acceleration": _accel_warning, "speed": _speed_warning}


def check_limit(name: str, cfg: dict, keys, printer_value, unit: str) -> dict:
    key, best = _highest(cfg, keys)
    lim = floats(printer_value)
    if best is None or not lim:
        return _unknown(name)
    profile = {"highest": best, "setting": key}
    if best > lim[0] + 1:
        return _check(name, "warn", profile, lim[0], _LIMIT_WARNINGS[name](best, key, lim[0], unit))
    return _check(name, "ok", profile, lim[0], f"Every {name} in the profile is within Klipper's {lim[0]:g} {unit}.")


def _temp_check(name: str, temps: list[float], lim: list[float]) -> dict:
    temps = [t for t in temps if t > 0]
    if not temps or not lim:
        return _unknown(name)
    hi = max(temps)
    if hi > lim[0]:
        return _check(name, "warn", hi, lim[0], f"The profile asks for {hi:g} °C but Klipper's max_temp is "
                                                f"{lim[0]:g} °C. Klipper refuses that, so the print would stop.")
    return _check(name, "ok", hi, lim[0], f"{hi:g} °C is within Klipper's {lim[0]:g} °C limit.")


def check_nozzle_temp(cfg: dict, settings: dict) -> dict:
    temps = floats(cfg.get("nozzle_temperature")) + floats(cfg.get("nozzle_temperature_initial_layer"))
    return _temp_check("nozzle temperature", temps, floats((settings.get("extruder") or {}).get("max_temp")))


def check_bed_temp(cfg: dict, settings: dict) -> dict:
    key = BED_TEMP_KEYS.get(str(cfg.get("curr_bed_type") or "").strip().lower())
    if key is None:
        return _unknown("bed temperature", "The bed type isn't one OrcaSlicer maps to a temperature setting.")
    temps = floats(cfg.get(key)) + floats(cfg.get(key + "_initial_layer"))
    return _temp_check("bed temperature", temps, floats((settings.get("heater_bed") or {}).get("max_temp")))


def check_firmware_retraction(cfg: dict, settings: dict) -> dict:
    name = "firmware retraction"
    fw = floats(cfg.get("use_firmware_retraction"))
    if not fw:
        return _unknown(name)
    section = settings.get("firmware_retraction")  # presence is the key, so an empty section still counts
    if not fw[0]:
        return _check(name, "ok", "off", "absent" if section is None else "present",
                      "OrcaSlicer does the retraction itself.")
    if "printer" not in settings:  # Klipper's settings were never read, so a missing section proves nothing
        return _unknown(name)
    if section is None:
        return _check(name, "warn", "on", "absent", "OrcaSlicer sends G10/G11 for retraction but Klipper has no "
                                                    "[firmware_retraction] section, so those commands fail.")
    p, k = floats(cfg.get("retraction_length")), floats(section.get("retract_length"))
    if not p or not k:
        return _unknown(name)
    if abs(p[0] - k[0]) > 0.01:
        return _check(name, "warn", p[0], k[0], f"Retraction is done by Klipper, so its {k[0]:g} mm is used, not "
                                                f"the profile's {p[0]:g} mm.")
    return _check(name, "ok", p[0], k[0], "Klipper does the retraction with the same length as the profile.")


def compare(cfg: dict, settings: dict) -> dict:
    printer = settings.get("printer") or {}
    checks = [
        check_nozzle(cfg, settings),
        check_bed(cfg, settings),
        check_limit("acceleration", cfg, ACCEL_KEYS, printer.get("max_accel"), "mm/s²"),
        check_limit("speed", cfg, SPEED_KEYS, printer.get("max_velocity"), "mm/s"),
        check_nozzle_temp(cfg, settings),
        check_bed_temp(cfg, settings),
        check_firmware_retraction(cfg, settings),
    ]
    warns = [c["check"] for c in checks if c["status"] == "warn"]
    unknown = sum(1 for c in checks if c["status"] == "unknown")
    ok = sum(1 for c in checks if c["status"] == "ok")
    if warns:
        head = f"{len(warns)} warning{'s' if len(warns) != 1 else ''}: {', '.join(warns)}."
        if unknown:
            head += f" {unknown} couldn't be checked."
    elif unknown == len(checks):
        head = "Nothing could be checked: the profile or the printer's settings weren't available."
    elif unknown:
        head = f"No mismatches found: {ok} check{'s' if ok != 1 else ''} passed, {unknown} couldn't be checked."
    else:
        head = f"The profile matches the printer on {len(checks)} of {len(checks)} checks."
    return {"headline": head, "checks": checks}
