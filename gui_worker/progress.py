# Give the planner a memory of what each step did, in words, and whether the screen reacted.
# Element IDs (uia_N) are renumbered on every observation, so history must name targets instead.
"""Semantic step history, visible-effect checks, and repeated no-effect detection."""
from PIL import Image, ImageChops

# Changed-pixel fraction (after downscaling) below which a step counts as "no visible change".
# 0.0002 of 384x216 is about 17 downscaled pixels: typed text registers, a blinking caret does not.
MIN_CHANGED_FRACTION = 0.0002
# How many no-effect runs of the same action on the same screen are allowed before refusing it.
NO_EFFECT_LIMIT = 2
# These actions have no target whose repetition would indicate a loop.
UNGUARDED_ACTIONS = {"wait", "done", "blocked"}


# Short human-readable name for an element, e.g. "Hyperlink 'PSY - GANGNAM STYLE'".
def element_label(element):
    if not element:
        return None
    name = " ".join(str(element.get("name") or "").split())
    if len(name) > 60:
        name = name[:57] + "..."
    kind = element.get("type") or "element"
    return f"{kind} '{name}'" if name else f"unnamed {kind}"


def _area(element):
    left, top, right, bottom = element["bbox"]
    return (right - left) * (bottom - top)


# Smallest observed element whose rectangle contains the point: what a click there most likely hit.
def element_at(observation, x, y):
    hits = [e for e in observation.get("elements", [])
            if isinstance(e.get("bbox"), list) and len(e["bbox"]) == 4
            and e["bbox"][0] <= x < e["bbox"][2] and e["bbox"][1] <= y < e["bbox"][3]]
    return min(hits, key=_area) if hits else None


# Browsers report focus on the document and the field inside it; the smallest one is the real focus.
def focused_element(observation):
    focused = [e for e in observation.get("elements", [])
               if e.get("focused") is True and isinstance(e.get("bbox"), list) and len(e["bbox"]) == 4]
    return min(focused, key=_area) if focused else None


# Fraction of downscaled pixels that changed noticeably; None when either image is unavailable.
def screen_change_fraction(path_a, path_b, size=(384, 216), tolerance=12):
    if not path_a or not path_b:
        return None
    try:
        with Image.open(path_a) as first, Image.open(path_b) as second:
            if first.size != second.size:
                return 1.0
            first, second = first.convert("L").resize(size), second.convert("L").resize(size)
    except OSError:
        return None
    changed = ImageChops.difference(first, second).point(lambda v: 255 if v > tolerance else 0)
    return changed.histogram()[255] / (size[0] * size[1])


# Compare the observation before an action with the one after it.
def describe_effect(before, after):
    fraction = screen_change_fraction(before.get("screenshot_path"), after.get("screenshot_path"))
    title_before, title_after = before.get("window_title"), after.get("window_title")
    focus_before = element_label(focused_element(before))
    focus_after = element_label(focused_element(after))
    window_changed = before.get("window_handle") != after.get("window_handle")
    effect = {"changed_fraction": None if fraction is None else round(fraction, 4),
              "title_changed": title_before != title_after,
              "window_changed": window_changed,
              "focus_changed": focus_before != focus_after}
    effect["no_effect"] = (fraction is not None and fraction < MIN_CHANGED_FRACTION
                           and not effect["title_changed"] and not window_changed
                           and not effect["focus_changed"])
    if effect["no_effect"]:
        effect["summary"] = "NO visible change"
    else:
        parts = []
        if fraction is not None:
            parts.append(f"screen changed {fraction:.1%}" if fraction >= MIN_CHANGED_FRACTION
                         else "screen pixels unchanged")
        if window_changed:
            parts.append("foreground window changed")
        if effect["title_changed"]:
            parts.append(f"title now '{str(title_after)[:80]}'")
        if effect["focus_changed"]:
            parts.append(f"focus now {focus_after or 'nothing'}")
        effect["summary"] = "; ".join(parts) or "unknown"
    return effect


# Identify "the same action on the same screen" without relying on per-observation IDs.
def action_signature(record, window_title):
    action = record.get("action") if isinstance(record.get("action"), dict) else {}
    name = action.get("action")
    if name in UNGUARDED_ACTIONS or name is None:
        return None
    detail = {key: action[key] for key in ("text", "key", "keys", "url", "direction", "app_id",
                                           "window_handle", "clear", "submit") if key in action}
    resolved = record.get("resolved") or {}
    # Pixel targets are bucketed so a click two pixels away still counts as the same click.
    point = ((resolved["x"] // 25, resolved["y"] // 25)
             if isinstance(resolved.get("x"), int) and isinstance(resolved.get("y"), int) else None)
    return (window_title, name, record.get("target"), point,
            tuple(sorted((k, str(v)) for k, v in detail.items())))


class LoopGuard:
    """Refuse an action that already ran NO_EFFECT_LIMIT times on this screen with no visible effect."""

    def __init__(self, limit=NO_EFFECT_LIMIT):
        self.limit, self.counts = limit, {}

    def check(self, signature):
        if signature is not None and self.counts.get(signature, 0) >= self.limit:
            return (f"This exact action already ran {self.counts[signature]} times on this screen and "
                    "caused NO visible change. Do not repeat it: choose a different element, "
                    "scroll, click the visible link text, or use another route.")
        return None

    def record(self, signature, effect):
        if signature is None or not effect:
            return
        # Anything visible resets the count: the action did something on this screen.
        self.counts[signature] = self.counts.get(signature, 0) + 1 if effect.get("no_effect") else 0


# Model-facing summary of one step. Never includes uia_N IDs, which mean nothing next step.
def summarize_record(record):
    action = dict(record["action"]) if isinstance(record.get("action"), dict) else {"action": "invalid"}
    action.pop("element_id", None)
    if record.get("target"):
        action["target"] = record["target"]
    if record.get("clicked") and record["clicked"] != record.get("target"):
        action["clicked_instead"] = record["clicked"]
    outcome = record.get("outcome") or {}
    entry = {"step": record.get("step"), "action": action, "result": outcome.get("status")}
    if outcome.get("error_code"):
        entry["error_code"] = outcome["error_code"]
    if outcome.get("reason"):
        entry["reason"] = str(outcome["reason"])[:200]
    if outcome.get("partial_effects_possible"):
        entry["partial_effects_possible"] = True
    if record.get("intent"):
        entry["intent"] = str(record["intent"])[:200]
    if record.get("effect"):
        entry["effect"] = record["effect"]["summary"]
    # Lets the reflector see which stored lessons the agent followed at which step.
    if record.get("applied_lessons"):
        entry["applied_lessons"] = record["applied_lessons"]
    return entry


# Plain-language warnings computed from the whole attempt, not only the recent window.
def progress_warnings(history):
    warnings = []
    stalled = 0
    for record in reversed(history):
        effect = record.get("effect") or {}
        if (record.get("outcome") or {}).get("status") == "executed" and effect.get("no_effect"):
            stalled += 1
        else:
            break
    if stalled >= 2:
        warnings.append(f"The last {stalled} executed actions caused NO visible change. The current "
                        "approach is not working; pick a different target or route.")
    dead = []
    for record in history:
        if (record.get("effect") or {}).get("no_effect") and record.get("target"):
            label = record.get("clicked") or record["target"]
            if label not in dead:
                dead.append(label)
    if dead:
        warnings.append("Targets that did nothing when used: " + "; ".join(dead[-8:]))
    # Typed text that did not arrive exactly: the field must be corrected before going on.
    # A field counts as fixed once a later typing step into the same target was confirmed.
    unresolved = {}
    for record in history:
        effect, target = record.get("effect") or {}, record.get("target") or "focused field"
        if effect.get("text_mismatch"):
            unresolved[target] = True
        elif effect.get("text_confirmed"):
            unresolved.pop(target, None)
    if unresolved:
        warnings.append("Typed text did not arrive as intended in: " + "; ".join(unresolved)
                        + ". Fix it (type_into with clear=true) before continuing or returning done.")
    return warnings
