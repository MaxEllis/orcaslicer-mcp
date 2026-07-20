"""Pure shaping + stateless prediction-check for the fork's slice breakdown (spec 2026-07-20)."""
from __future__ import annotations
from .physics_check import predicted_flows, _f

_FLOW_TOL = 0.10      # +/-10% flow tolerance
_NEAR_CEILING = 0.95  # observed within 5% of ceiling counts as clamped


def prediction_check(cfg: dict, breakdown: dict) -> list[dict]:
    preds = predicted_flows(cfg)
    ceiling = _f(cfg, "filament_max_volumetric_speed")
    out: list[dict] = []
    for role in breakdown.get("roles", []):
        name = role.get("role")
        flow = (role.get("flow_mm3_s") or {}).get("max")
        if name not in preds or flow is None:
            continue
        predicted = preds[name]
        # clamped: the profile's demanded flow exceeds the ceiling, and the observed max
        # sits at the ceiling because Orca throttled it to stay under the limit.
        clamped = (ceiling is not None and predicted > ceiling
                   and flow >= ceiling * _NEAR_CEILING)
        if clamped:
            verdict = "clamped"
            detail = (f"{name}: profile demands {predicted:.1f}mm3/s but observed max "
                      f"{flow:.1f} is pinned at ceiling {ceiling:g} - speed silently throttled")
        elif flow > predicted * (1 + _FLOW_TOL):
            verdict = "anomaly"
            detail = f"{name}: observed {flow:.1f}mm3/s exceeds predicted {predicted:.1f} (>10%)"
        else:
            verdict = "matches"
            detail = f"{name}: predicted {predicted:.1f}mm3/s vs observed {flow:.1f} (within tolerance)"
        out.append({"role": name, "verdict": verdict,
                    "predicted_mm3_s": round(predicted, 2),
                    "observed_max_mm3_s": round(flow, 2), "detail": detail})
    return out


def build_breakdown(status: dict, cfg: dict) -> dict:
    bd = status.get("breakdown")
    if not bd:
        return {"available": False,
                "reason": "no breakdown in slice status (fork build predates this feature, "
                          "or no valid slice yet)"}
    return {"available": True,
            "mode": bd.get("mode"),
            "total_time_s": bd.get("total_time_s"),
            "roles": bd.get("roles", []),
            "metrics": bd.get("metrics", {}),
            "layers": bd.get("layers", []),
            "prediction_check": prediction_check(cfg, bd)}
