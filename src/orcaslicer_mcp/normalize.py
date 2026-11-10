"""Read what agents commonly send in place of a tool's own argument names, before validation.

An unknown argument name is dropped without a word by the SDK, and a missing one fails the call:
`get_config(key="layer_height")` used to come back with the whole config (the `key` was ignored), and
`describe_setting(name="brim_width")` failed with "key: Field required". OrcaSlicer's own MCP server
(upstream branch feature/mcp, `normalize_arguments`) reads these from its agents' transcripts; this is
the same idea for our argument names.

Rules, each only when the call leaves the tool's own name out:
- an argument under a name the tool does not take is read as one it does: camelCase as snake_case
  (`objectId` -> `object_id`), a known alias (`settings` -> `changes`, `id` -> `object_id`, ...), or
  the singular/plural of the name (`key` -> `keys`);
- a single value given where the tool takes a list becomes a one-item list, and a comma-separated
  string becomes a list of its parts (`"layer_height, wall_loops"` -> two keys).
Anything that still does not fit is left alone, for the validator to report.
"""
from __future__ import annotations

import json
import re

# What agents send -> names our tools take. Several targets per alias: the first one the tool takes wins.
ALIASES: dict[str, tuple[str, ...]] = {
    "settings": ("changes",), "values": ("changes",), "config": ("changes",), "updates": ("changes",),
    "overrides": ("changes",), "edits": ("changes",), "params": ("changes",),
    "setting": ("key", "keys"), "setting_key": ("key", "keys"), "name": ("key",), "keys": ("key",),
    "setting_keys": ("keys",), "settings_keys": ("keys",),
    "id": ("object_id",), "object": ("object_id",), "obj": ("object_id",), "obj_id": ("object_id",),
    "q": ("query",), "text": ("query",), "search": ("query",), "term": ("query",), "keyword": ("query",),
    "keywords": ("query",), "filter": ("query",),
    "plate": ("index",), "plate_index": ("index", "plate"), "plate_id": ("index", "plate"),
    "file": ("path",), "filepath": ("path",), "file_path": ("path",),
    "model_path": ("path", "model_name"), "project": ("path",), "project_path": ("path",),
    "model": ("path", "model_name"),
    "preset": ("name",), "preset_name": ("name",), "preset_type": ("type",), "kind": ("type",),
    "group": ("type",),
    # Not offset/rotation: list_objects reports those as absolute values, and translate/rotate are relative.
    "translation": ("translate",), "move": ("translate",),
    "scaling": ("scale",), "scale_factor": ("scale",),
    "timeout_seconds": ("timeout", "timeout_s"), "timeout": ("timeout_s",),
    "max_results": ("limit",), "count": ("limit",), "n": ("limit",),
    "destination": ("to",), "dest": ("to",), "target": ("to",), "new_path": ("to",),
    "directory": ("dir",), "folder": ("dir",),
    "message": ("note",), "content": ("note",),
}

_CAMEL = re.compile(r"(?<=[a-z0-9])([A-Z])")


def _snake(name: str) -> str:
    return _CAMEL.sub(r"_\1", name).lower()


def _types(prop: dict) -> set[str]:
    """The JSON types a schema property takes (anyOf/oneOf flattened)."""
    out: set[str] = set()
    t = prop.get("type")
    if isinstance(t, str):
        out.add(t)
    elif isinstance(t, list):
        out.update(x for x in t if isinstance(x, str))
    for alt in (prop.get("anyOf") or []) + (prop.get("oneOf") or []):
        if isinstance(alt, dict):
            out |= _types(alt)
    return out


def _item_types(prop: dict) -> set[str]:
    items = prop.get("items")
    if isinstance(items, dict):
        return _types(items)
    for alt in (prop.get("anyOf") or []) + (prop.get("oneOf") or []):
        if isinstance(alt, dict) and isinstance(alt.get("items"), dict):
            return _types(alt["items"])
    return set()


def _fits(value, prop: dict) -> bool:
    """Whether a value's JSON type is one the target argument takes, counting the forms the second pass turns
    into it (a single value or a comma list for a list argument, a one-item list for a single value). A rename
    that would change the meaning (slice_and_wait(wait=true) read as timeout=1) is refused this way."""
    types = _types(prop)
    if not types:
        return True
    if value is None:
        return "null" in types
    if isinstance(value, bool):
        kinds = {"boolean"}
    elif isinstance(value, int):
        kinds = {"integer", "number"}
    elif isinstance(value, float):
        kinds = {"number"}
    elif isinstance(value, str):
        kinds = {"string"}
    elif isinstance(value, list):
        kinds = {"array"} | ({"string", "integer", "number", "boolean"} if len(value) == 1 else set())
    elif isinstance(value, dict):
        kinds = {"object"}
    else:
        return False
    if "array" in types and not isinstance(value, (list, dict)):
        kinds = kinds | {"array"}
    if isinstance(value, str) and "string" not in types:
        try:  # a number, list or object written as JSON text: the SDK parses those
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return "array" in types  # a word or a comma list still becomes a list
        return not isinstance(parsed, str) and _fits(parsed, prop)
    return bool(kinds & types)


def _candidates(name: str) -> list[str]:
    snake = _snake(name)
    cands = [snake]
    cands.extend(ALIASES.get(snake, ()))
    if snake.endswith("s"):
        cands.append(snake[:-1])
    else:
        cands.append(snake + "s")
    return cands


def normalize_arguments(schema: dict | None, arguments: dict) -> tuple[dict, list[str]]:
    """Return (arguments in the tool's own form, what was changed in words). Never raises."""
    if not isinstance(arguments, dict) or not isinstance(schema, dict):
        return arguments, []
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return arguments, []
    args = dict(arguments)
    notes: list[str] = []
    for sent in list(args):
        if sent in props:
            continue
        for cand in _candidates(sent):
            if cand in props and cand not in args and _fits(args[sent], props[cand]):
                args[cand] = args.pop(sent)
                notes.append(f"read {sent} as {cand}")
                break
    for name, value in list(args.items()):
        prop = props.get(name)
        if not isinstance(prop, dict):
            continue
        types = _types(prop)
        if "array" not in types and isinstance(value, list) and len(value) == 1 \
                and not isinstance(value[0], (list, dict)) and types & {"string", "integer", "number", "boolean"}:
            args[name] = value[0]  # describe_setting(keys=["brim_width"]) read as key="brim_width"
            notes.append(f"read {name} from a one-item list")
            continue
        if "array" not in types or isinstance(value, (list, dict)) or value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                continue  # a JSON list in a string: the SDK parses that itself
            if "," in text and _item_types(prop) <= {"string"}:
                args[name] = [part.strip() for part in text.split(",") if part.strip()]
                notes.append(f"split {name} at its commas")
                continue
        args[name] = [value]
        notes.append(f"read {name} as a one-item list")
    return args, notes
