"""Natural-language corrections -> typed, validated Patch -> new Scene.

Two parsers produce the same `schema.Patch`:
* `parse_rules`: deterministic regex grammar for common phrasings (no network).
* `parse_llm`: OpenAI structured output restricted to existing ids and allowed fields.
Both go through `apply_patch`, which re-validates the whole scene and changes nothing but the
requested fields (marked provenance "user_confirmed").
"""

from __future__ import annotations

import json
import math
import os
import re

from pydantic import ValidationError

from .schema import Patch, PatchOp, Scene

FIELDS = {
    "object": {"x", "y", "yaw_deg", "size", "mass", "friction", "wall_thickness", "label"},
    "table": {"width", "depth", "height", "friction"},
    "robot": {"x", "y", "yaw_deg"},
    "task": {"target_id"},
}
_TEXT_FIELDS = {"label", "target_id"}
_UNITS_OF = {"x": "m", "y": "m", "size": "m", "width": "m", "depth": "m", "height": "m",
             "wall_thickness": "m", "yaw_deg": "°", "mass": "kg"}


class CorrectionError(ValueError):
    """The correction was not understood."""


class InvalidCorrection(CorrectionError):
    """The correction was understood but would produce an invalid scene."""


# ---- applying patches ----------------------------------------------------------------------

def _container(data: dict, target: str) -> tuple[dict, str]:
    if target in ("table", "robot", "task"):
        return data[target], target
    for o in data["objects"]:
        if o["id"] == target:
            return o, "object"
    raise InvalidCorrection(f"unknown target '{target}' (objects: {', '.join(o['id'] for o in data['objects'])})")


def apply_patch(scene: Scene, patch: Patch) -> Scene:
    """Apply `patch` to a deep copy of `scene`; raises InvalidCorrection if the result is invalid."""
    data = scene.model_dump()
    touched: set[str] = set()
    for op in patch.ops:
        c, kind = _container(data, op.target)
        if op.field not in FIELDS[kind]:
            raise InvalidCorrection(f"'{op.field}' cannot be edited on {op.target} (allowed: {', '.join(sorted(FIELDS[kind]))})")
        cur, v = c.get(op.field), op.value
        if op.field in _TEXT_FIELDS:
            if not isinstance(v, str) or op.op != "set":
                raise InvalidCorrection(f"{op.target}.{op.field} takes a text value with op 'set'")
            new = v.strip()
        elif op.field == "size":
            if not (isinstance(v, list) and len(v) == 3):
                raise InvalidCorrection("size takes a full [sx, sy, sz] list in metres")
            new = [round(a + b, 6) for a, b in zip(cur, v)] if op.op == "add" else [round(float(b), 6) for b in v]
        else:
            if isinstance(v, (str, list)) or not math.isfinite(v):
                raise InvalidCorrection(f"{op.target}.{op.field} takes a single number")
            if op.op == "add" and cur is None:
                raise InvalidCorrection(f"{op.target} has no {op.field} to add to")
            new = round(cur + v if op.op == "add" else float(v), 6)
        c[op.field] = new
        if "provenance" in c:
            c["provenance"][op.field] = "user_confirmed"
        touched.add("*" if kind == "table" else op.target)
    try:
        out = Scene.model_validate(data)
    except ValidationError as e:
        msgs = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'scene'}: {err['msg'].removeprefix('Value error, ')}" for err in e.errors())
        raise InvalidCorrection(f"correction rejected, the scene would be invalid: {msgs}") from None
    for o in out.objects:
        if ("*" in touched or o.id in touched) and not (0 <= o.x <= out.table.width and 0 <= o.y <= out.table.depth):
            raise InvalidCorrection(
                f"correction rejected: {o.id} would sit off the table (centre {o.x:.2f}, {o.y:.2f} m; "
                f"table is {out.table.width:.2f} x {out.table.depth:.2f} m)")
    return out


def _fmt(v, field: str) -> str:
    u = _UNITS_OF.get(field, "")
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(f"{a:.3f}" for a in v) + "]" + (f" {u}" if u else "")
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") + ({"m": " m", "°": "°", "kg": " kg"}.get(u, ""))
    return str(v)


def describe_changes(before: Scene, after: Scene, patch: Patch) -> str:
    """'x 0.72 → 0.52 m; y ...' for every field the patch touched."""
    seen, parts = set(), []
    multi = len({op.target for op in patch.ops}) > 1
    for op in patch.ops:
        if (op.target, op.field) in seen:
            continue
        seen.add((op.target, op.field))
        b, _ = _container(before.model_dump(), op.target)
        a, _ = _container(after.model_dump(), op.target)
        parts.append(f"{op.target + '.' if multi else ''}{op.field} {_fmt(b.get(op.field), op.field)} → {_fmt(a.get(op.field), op.field)}")
    return "; ".join(parts)


# ---- rule-based parser ---------------------------------------------------------------------

_NUM = r"(?<![\w.])(-?\d+(?:\.\d+)?)"
_LEN_UNITS = {"mm": 0.001, "millimet": 0.001, "cm": 0.01, "centimet": 0.01, "in": 0.0254, '"': 0.0254, "m": 1.0, "met": 1.0}
_LEN_U = r"(millimet(?:er|re)s?|mm|centimet(?:er|re)s?|cms?|met(?:er|re)s?|m|inch(?:es)?|in|\")"
_LEN = re.compile(_NUM + r"\s*" + _LEN_U + r"(?![a-z])")
_ANGLE = re.compile(_NUM + r"\s*(?:°|deg(?:rees?)?\b)")
_MASS = re.compile(_NUM + r"\s*(kg|kilograms?|kilos?|g|grams?|grammes?)\b")
_ROBOT_DIR = re.compile(r"\b(?:(towards?|closer to|nearer to|to)|(away from|further from|farther from))\s+(?:the\s+)?"
                        r"(?:robot|arm|panda)(?:'?s base)?\b")
_EDGE_DIR = re.compile(r"\b(?:(?:towards?|to|closer to|nearer to|in the direction of)\s+)?(?:the\s+)?(?:table'?s\s+)?"
                       r"(left|right|front|back|rear)(?:wards?)?\b(?:\s+(?:edge|side|end))?(?:\s+of\s+the\s+table)?")
_CAMERA_DIR = re.compile(r"\b(towards?|closer to|away from)\s+(?:the\s+)?(?:camera|viewer|me)\b")
_EDGES = {"left": (-1.0, 0.0), "right": (1.0, 0.0), "front": (0.0, -1.0), "back": (0.0, 1.0), "rear": (0.0, 1.0)}
_BLOCK_WORDS = r"\b(?:block|cube|box|brick|part|object|item)\b"
_BIN_WORDS = r"\b(?:bin|tray|container|basket|crate)s?\b"
_GENERIC = {"the", "a", "an", "block", "cube", "box", "brick", "part", "object", "item", "bin", "tray", "container",
            "basket", "crate", "placeholder", "not", "detected", "of"}
_AXES = {"wide": 0, "width": 0, "deep": 1, "depth": 1, "tall": 2, "high": 2, "height": 2}
_TABLE_AXES = {0: "width", 1: "depth", 2: "height"}


def _metres(num: str, unit: str) -> float:
    unit = unit.lower()
    for k in ("millimet", "centimet", "met", "mm", "cm", "in", '"', "m"):
        if unit.startswith(k):
            return float(num) * _LEN_UNITS[k]
    raise CorrectionError(f"unknown unit '{unit}'")


def _roles(scene: Scene) -> dict[str, str]:
    other = next(o.id for o in scene.objects if o.kind == "bin" and o.id != scene.task.target_id)
    return {scene.task.object_id: "block", scene.task.target_id: "target bin", other: "other bin"}


def _resolve(text: str, scene: Scene) -> str | None:
    """Which object/table/robot the text talks about; None if nothing; raises if ambiguous."""
    scores: dict[str, int] = {}
    roles = _roles(scene)

    def bump(oid, s):
        scores[oid] = max(scores.get(oid, 0), s)

    for o in scene.objects:
        if re.search(rf"\b{re.escape(o.id)}\b", text):
            bump(o.id, 100)
        label = o.label.lower().strip()
        if label and label in text:
            bump(o.id, 60)
        words = [w for w in re.findall(r"[a-z]+", label) if w not in _GENERIC]
        if words and any(re.search(rf"\b{w}\b", text) for w in words):
            kind_word = _BLOCK_WORDS if o.kind == "block" else _BIN_WORDS
            bump(o.id, 50 if re.search(kind_word, text) else 40)
    if re.search(r"\b(?:target|destination|goal|drop[- ]?off)\s+" + _BIN_WORDS[2:], text):
        bump(scene.task.target_id, 70)
    if re.search(r"\b(?:source|other|non[- ]?target|pick(?:up|ing)?|second)\s+" + _BIN_WORDS[2:], text):
        bump(next(k for k, r in roles.items() if r == "other bin"), 70)
    if re.search(_BLOCK_WORDS, text):
        bump(scene.task.object_id, 30)
    if not scores:
        if re.search(r"\b(?:robot|arm|panda)\b", text):
            return "robot"
        if re.search(r"\btable\b", text):
            return "table"
        if re.search(_BIN_WORDS, text):
            raise CorrectionError("which bin? say 'target bin', 'other bin', its id (bin_a/bin_b) or its colour")
        return None
    best = max(scores.values())
    top = [k for k, s in scores.items() if s == best]
    if len(top) > 1:
        raise CorrectionError(f"ambiguous object reference ({', '.join(top)})")
    return top[0]


def _name(oid: str, scene: Scene) -> str:
    role = _roles(scene).get(oid)
    return f"{oid} ({role})" if role and role != "block" else oid


def _strip(text: str, *patterns: re.Pattern) -> str:
    for p in patterns:
        text = p.sub(" ", text)
    return text


def _xy(target: str, scene: Scene) -> tuple[float, float]:
    if target == "robot":
        return scene.robot.x, scene.robot.y
    o = scene.obj(target)
    return o.x, o.y


def _parse_move(t: str, scene: Scene) -> Patch | None:
    if not re.search(r"\b(?:move|shift|slide|nudge|push|pull|translate|drag|bring|put|mount|place|relocate|"
                     r"further|farther|actually|should be|sits?)\b", t):
        return None
    m = _LEN.search(t)
    setm = re.findall(r"\b([xy])\s*(?:=|to|:)\s*" + _NUM + r"\s*" + _LEN_U + "?", t)
    if not (m or setm):
        return None
    target = _resolve(_strip(t, _LEN, _ROBOT_DIR, _CAMERA_DIR, _EDGE_DIR, re.compile(r"\b[xy]\s*(?:=|to|:)")), scene)
    if setm:
        if target in (None, "table"):
            raise CorrectionError("which object should be placed at that coordinate?")
        ops = [PatchOp(target=target, field=f, op="set", value=_metres(n, u) if u else float(n)) for f, n, u in setm]
        return Patch(ops=ops, summary=f"Placed {_name(target, scene)} at " + ", ".join(f"{op.field} = {op.value:.3f} m" for op in ops))
    d = _metres(*m.groups())
    if target in (None, "table"):
        raise CorrectionError("could not tell which object to move")
    if rm := _ROBOT_DIR.search(t):
        if target == "robot":
            raise CorrectionError("cannot move the robot toward itself")
        (ox, oy), (rx, ry) = _xy(target, scene), (scene.robot.x, scene.robot.y)
        n = math.hypot(rx - ox, ry - oy)
        if n < 1e-6:
            raise CorrectionError(f"{target} is at the robot base; direction undefined")
        sgn = 1.0 if rm.group(1) else -1.0
        dx, dy = sgn * d * (rx - ox) / n, sgn * d * (ry - oy) / n
        where = "toward the robot" if sgn > 0 else "away from the robot"
    elif cm := _CAMERA_DIR.search(t):
        (dx, dy), where = ((0.0, -d), "toward the front edge") if cm.group(1) != "away from" else ((0.0, d), "toward the back edge")
    elif em := _EDGE_DIR.search(t):
        ux, uy = _EDGES[em.group(1)]
        dx, dy, where = ux * d, uy * d, f"toward the {'back' if em.group(1) == 'rear' else em.group(1)} edge"
    else:
        raise CorrectionError("move needs a direction: left/right/front/back edge, toward/away from the robot")
    ops = [PatchOp(target=target, field=f, op="add", value=round(v, 6)) for f, v in (("x", dx), ("y", dy)) if abs(v) > 1e-9]
    return Patch(ops=ops, summary=f"Moved {_name(target, scene)} {d:.2f} m {where}")


def _parse_rotate(t: str, scene: Scene) -> Patch | None:
    if not re.search(r"\b(?:rotate|turn|spin|yaw)\b", t):
        return None
    m = _ANGLE.search(t) or re.search(r"\b(?:by|to)\s+" + _NUM + r"\b(?!\s*" + _LEN_U + ")", t)
    if not m:
        raise CorrectionError("rotation needs an angle, e.g. 'rotate bin_a by 30 degrees'")
    ang = float(m.group(1))
    if re.search(r"\b(?:clockwise|cw)\b", t) and not re.search(r"\b(?:counter|anti)[- ]?clockwise\b", t):
        ang = -ang
    absolute = re.search(r"\bto\s+-?\d", t) is not None
    target = _resolve(_strip(t, _ANGLE, _ROBOT_DIR, _EDGE_DIR), scene)
    if target in (None, "table"):
        raise CorrectionError("could not tell which object to rotate")
    sense = "counter-clockwise" if ang >= 0 else "clockwise"
    verb = (f"Set yaw of {_name(target, scene)} to {ang:g}°" if absolute
            else f"Rotated {_name(target, scene)} {abs(ang):g}° {sense} (seen from above)")
    return Patch(ops=[PatchOp(target=target, field="yaw_deg", op="set" if absolute else "add", value=ang)], summary=verb)


def _parse_size(t: str, scene: Scene) -> Patch | None:
    full = re.search(_NUM + r"\s*[x×]\s*" + _NUM + r"\s*[x×]\s*" + _NUM + r"\s*" + _LEN_U, t)
    cube = re.search(_NUM + r"\s*" + _LEN_U + r"\s+cube\b", t)
    pairs = [(_AXES[a], _metres(n, u)) for n, u, a in re.findall(_NUM + r"\s*" + _LEN_U + r"\s+(wide|deep|tall|high)\b", t)]
    pairs += [(_AXES[a], _metres(n, u)) for a, n, u in
              re.findall(r"\b(width|depth|height)\b(?:\s+of\s+[\w' ]+?)?\s*(?:is|to|=|:|of)?\s*" + _NUM + r"\s*" + _LEN_U, t)]
    if not (full or cube or pairs):
        return None
    rest = _strip(t, _LEN, re.compile(r"\b(?:wide|deep|tall|high|width|depth|height|cube)\b"))
    target = _resolve(rest, scene)
    if target is None:
        raise CorrectionError("could not tell which object the size applies to")
    if target == "robot":
        raise CorrectionError("the robot's size cannot be edited")
    if target == "table":
        if full or cube:
            raise CorrectionError("give table width/depth/height separately")
        ops = [PatchOp(target="table", field=_TABLE_AXES[a], value=v) for a, v in pairs]
        return Patch(ops=ops, summary="Set table " + ", ".join(f"{op.field} to {op.value:.3f} m" for op in ops))
    size = list(scene.obj(target).size)
    if full:
        size = [_metres(n, full.group(4)) for n in full.groups()[:3]]
    elif cube:
        size = [_metres(*cube.groups())] * 3
    for a, v in pairs:
        size[a] = v
    return Patch(ops=[PatchOp(target=target, field="size", value=size)],
                 summary=f"Resized {_name(target, scene)}")


def _parse_physical(t: str, scene: Scene) -> Patch | None:
    if (m := _MASS.search(t)) and re.search(r"\b(?:mass|weighs?|weight|heavy)\b", t):
        kg = float(m.group(1)) * (1.0 if m.group(2).startswith("k") else 0.001)
        target = _resolve(_strip(t, _MASS), scene) or scene.task.object_id
        return Patch(ops=[PatchOp(target=target, field="mass", value=kg)], summary=f"Set mass of {_name(target, scene)} to {kg:g} kg")
    if (m := re.search(r"\bfriction\b(?:\s+(?:coefficient|of\s+[\w' ]+?))?\s*(?:is|to|=|:)?\s*" + _NUM, t)):
        target = _resolve(_strip(t, re.compile(r"\bfriction\b.*")), scene) or scene.task.object_id
        return Patch(ops=[PatchOp(target=target, field="friction", value=float(m.group(1)))],
                     summary=f"Set friction of {_name(target, scene)} to {float(m.group(1)):g}")
    if re.search(r"\bwalls?\b", t) and (m := _LEN.search(t)):
        target = _resolve(_strip(t, _LEN), scene)
        if target is None or target in ("table", "robot"):
            raise CorrectionError("which bin's wall thickness?")
        v = _metres(*m.groups())
        return Patch(ops=[PatchOp(target=target, field="wall_thickness", value=v)],
                     summary=f"Set wall thickness of {_name(target, scene)} to {v * 1000:.0f} mm")
    return None


def _parse_task(t: str, scene: Scene) -> Patch | None:
    if re.search(r"\d", t):
        return None
    m = (re.search(r"\b(?:place|put|drop|deliver|move|transfer)\s+(.+?)\s+(?:in|into|inside|onto)\s+(.+)", t)
         or re.search(r"\b(?:make|set|use)\s+(?:the\s+)?(.+?)\s+(?:as\s+)?(?:the\s+)?(?:target|destination|goal)\b(.*)", t)
         or re.search(r"\b(?:target|destination|goal)(?:\s+bin)?\s+(?:is|should be|=|:)\s+(.+)", t))
    if not m:
        return None
    if len(m.groups()) == 2 and m.group(0).split()[0] in ("make", "set", "use"):
        bin_text, obj_text = m.group(1), ""
    elif len(m.groups()) == 2:
        obj_text, bin_text = m.group(1), m.group(2)
    else:
        obj_text, bin_text = "", m.group(1)
    if obj_text and _resolve(obj_text, scene) not in (scene.task.object_id, None):
        raise CorrectionError("only the block can be placed")
    target = _resolve(bin_text, scene)
    if target is None or target in ("table", "robot") or scene.obj(target).kind != "bin":
        raise CorrectionError("the destination must be one of the bins")
    return Patch(ops=[PatchOp(target="task", field="target_id", value=target)],
                 summary=f"Task target set to {target}" + (f" ({scene.obj(target).label})" if scene.obj(target).label else ""))


def _parse_label(t: str, scene: Scene) -> Patch | None:
    m = re.search(r"\b(?:rename|relabel|call|label)\s+(.+?)\s+(?:to|as)\s+[\"']?([^\"']+?)[\"']?$", t)
    if not m:
        return None
    target = _resolve(m.group(1), scene)
    if target in (None, "table", "robot"):
        raise CorrectionError("which object should be renamed?")
    return Patch(ops=[PatchOp(target=target, field="label", value=m.group(2).strip())],
                 summary=f"Renamed {target} to '{m.group(2).strip()}'")


_RULES = (_parse_label, _parse_task, _parse_rotate, _parse_size, _parse_physical, _parse_move)


def _normalise(text: str) -> str:
    t = text.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", t).strip().rstrip(".!")


def parse_rules(text: str, scene: Scene) -> Patch:
    """Deterministic parser. Raises CorrectionError if no rule matches exactly one intent."""
    t = _normalise(text)
    if not t:
        raise CorrectionError("empty correction")
    hits = [p for p in (rule(t, scene) for rule in _RULES) if p is not None]
    if not hits:
        raise CorrectionError("no rule matched (supported: move/shift, rotate, size, mass, friction, wall thickness, target bin, rename)")
    if len(hits) > 1:
        raise CorrectionError("the sentence mixes several kinds of change; give one change at a time")
    return hits[0]


# ---- LLM parser ---------------------------------------------------------------------------

_LLM_INSTRUCTIONS = """Convert the user's correction of a robot workcell scene into a minimal patch.
Table frame: origin at the FRONT-LEFT tabletop corner, +x toward the RIGHT edge, +y toward the BACK
edge, z up, metres. Yaw in degrees, counter-clockwise seen from above. Sizes are FULL extents [sx, sy, sz].
Rules:
- Change ONLY what the user asks; never touch other objects or fields.
- Relative moves use op "add" with metre deltas on x and/or y. "Toward the robot" means along the unit
  vector from the object's (x, y) to the robot base (x, y); compute both components.
- size is always a complete [sx, sy, sz] list with op "set" (copy unchanged components).
- To change which bin the block goes to, use target "task", field "target_id", text = the bin id.
- Use existing ids only. "target bin" = task.target_id; "other"/"source" bin = the remaining bin.
- If the request is unclear or asks for something unsupported, return ok=false with a reason and no ops."""


def _llm_schema(scene: Scene) -> dict:
    targets = [o.id for o in scene.objects] + ["table", "robot", "task"]
    fields = sorted(set().union(*FIELDS.values()))
    op = {"type": "object", "additionalProperties": False, "required": ["target", "field", "op", "number", "vector", "text"],
          "properties": {
              "target": {"type": "string", "enum": targets},
              "field": {"type": "string", "enum": fields},
              "op": {"type": "string", "enum": ["set", "add"]},
              "number": {"type": ["number", "null"]},
              "vector": {"anyOf": [{"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}, {"type": "null"}]},
              "text": {"type": ["string", "null"]}}}
    return {"type": "object", "additionalProperties": False, "required": ["ok", "reason", "summary", "ops"],
            "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"},
                           "summary": {"type": "string", "description": "One sentence describing the change"},
                           "ops": {"type": "array", "items": op}}}


def parse_llm(text: str, scene: Scene, *, model: str | None = None, timeout: float = 60.0) -> Patch:
    """OpenAI structured-output parser; the result is a Patch validated exactly like rule output."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise CorrectionError("LLM parser unavailable (OPENAI_API_KEY not set)")
    from openai import OpenAI, OpenAIError

    from .vision import vision_model

    model = model or os.environ.get("OPENAI_CORRECTION_MODEL") or vision_model()
    roles = _roles(scene)
    ctx = {"scene": scene.model_dump(exclude={"notes"}), "roles": roles}
    try:
        resp = OpenAI(timeout=timeout).responses.create(
            model=model, instructions=_LLM_INSTRUCTIONS,
            input=f"Scene:\n{json.dumps(ctx)}\n\nCorrection: {text}",
            text={"format": {"type": "json_schema", "name": "scene_patch", "schema": _llm_schema(scene), "strict": True}})
        out = json.loads(resp.output_text or "{}")
    except (OpenAIError, json.JSONDecodeError) as e:
        raise CorrectionError(f"LLM parser failed ({type(e).__name__})") from None
    if not out.get("ok") or not out.get("ops"):
        raise CorrectionError(f"LLM could not interpret it: {out.get('reason') or 'no changes proposed'}")
    ops = []
    for o in out["ops"]:
        value = o["number"] if o["number"] is not None else o["vector"] if o["vector"] is not None else o["text"]
        if value is None:
            raise CorrectionError(f"LLM proposed {o['target']}.{o['field']} without a value")
        ops.append(PatchOp(target=o["target"], field=o["field"], op=o["op"], value=value))
    return Patch(ops=ops, summary=out.get("summary", ""))


# ---- entry point ---------------------------------------------------------------------------

def interpret(text: str, scene: Scene, prefer: str = "rules") -> tuple[Patch, str, str]:
    """Return (patch, explanation, parser_used). The patch is already validated against `scene`.

    prefer="rules": deterministic rules first, LLM only if they do not match.
    prefer="llm": LLM first when configured, rules as fallback.
    InvalidCorrection (understood but invalid, e.g. size out of bounds) is raised immediately.
    """
    order = ("rules", "llm") if prefer == "rules" else ("llm", "rules")
    reasons = []
    for name in order:
        try:
            patch = parse_rules(text, scene) if name == "rules" else parse_llm(text, scene)
            after = apply_patch(scene, patch)
        except InvalidCorrection:
            raise
        except CorrectionError as e:
            reasons.append(f"{name}: {e}")
            continue
        changes = describe_changes(scene, after, patch)
        return patch, f"{patch.summary}: {changes}" if patch.summary else changes, name
    raise CorrectionError(f"Could not interpret {text!r}. " + " | ".join(reasons))
