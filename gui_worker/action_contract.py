# Describe each tool's arguments once, so the model schema and local validator agree.
"""Shared action field names and the structured model response schema."""
# Keyboard policy remains the same allowlist used by the actual input tools.
from input_policy import KEYS

# Each tuple lists only the arguments accepted by that action.
ACTION_ARGUMENTS = {
    # Prefer a named control from the current observation when one is available.
    "click_element": ("element_id",),
    # Desktop mode also allows a single click at a visible point in the screenshot.
    "click_at": ("x", "y"),
    # Typing goes to the focused control; selecting a control is a separate action.
    "type_text": ("text",),
    # Click an input, optionally replace its contents, type, and optionally press Enter, in one step.
    "type_into": ("element_id", "text", "clear", "submit"),
    # Browser only: Ctrl+L, type the address, Enter. Replaces a fragile 3-4 step sequence.
    "navigate_url": ("url",),
    # A single key and a shortcut have different argument shapes.
    "press_key": ("key",), "hotkey": ("keys",),
    # A loading page sometimes needs time before another screenshot is useful.
    "wait": ("seconds",),
    # Scrolling needs an observed target area, direction, and bounded wheel count.
    "scroll": ("element_id", "direction", "amount"),
    # Both launch routes accept only a registered application ID.
    "launch_app": ("app_id",), "launch_app_gui": ("app_id",),
    # Window switching uses a handle from the current permitted-window list.
    "focus_window": ("window_handle",),
    # Done has no arguments; blocked requires a short explanation.
    "done": (), "blocked": ("reason",),
}

# These schemas constrain values; the local validator still checks context and permissions.
ARGUMENT_SCHEMAS = {
    # A short pause remains bounded by the worker's step and overall time limits.
    "seconds": {"type": "integer", "minimum": 1, "maximum": 3},
    # Pixel positions start at the primary screen's top-left corner; local code checks the maximum.
    "x": {"type": "integer", "minimum": 0},
    "y": {"type": "integer", "minimum": 0},
    # The ID's presence in the current observation is checked locally.
    "element_id": {"type": "string"},
    # Printable characters (no newlines/tabs) are enforced by the local typing validator.
    "text": {"type": "string", "minLength": 1, "maxLength": 500},
    # Scheme and character checks happen locally; this only bounds the size.
    "url": {"type": "string", "minLength": 1, "maxLength": 500},
    # clear=true selects all existing text in the field first; submit=true presses Enter afterwards.
    "clear": {"type": "boolean"}, "submit": {"type": "boolean"},
    # The provider and dispatcher use the same allowed single-key names.
    "key": {"type": "string", "enum": sorted(KEYS)},
    # Exact allowed shortcut combinations are checked locally, not guessed from this array type.
    "keys": {"type": "array", "items": {"type": "string"}},
    # Only these wheel directions are supported by the desktop tool.
    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
    # Keep each scroll operation small enough to observe what changed afterward.
    "amount": {"type": "integer", "minimum": 1, "maximum": 10},
    # The local validator checks the app against this task's allowed_apps.
    "app_id": {"type": "string"},
    # The local validator checks the handle against permitted current windows.
    "window_handle": {"type": "integer"},
    # A blocked action needs a reason; this is not an argument of other actions.
    "reason": {"type": "string", "minLength": 1, "maxLength": 500},
}

# The validator uses these exact field sets without maintaining a second handwritten copy.
ACTION_FIELDS = {name: frozenset(("action", *arguments))
                 # Keep the name-to-arguments relationship defined above.
                 for name, arguments in ACTION_ARGUMENTS.items()}

# Typed working memory the model rewrites every step (Lecture 5 "typed scratchpad").
SCRATCHPAD_LIMITS = {"objective": 200, "text": 160, "items": 6}
SCRATCHPAD_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["objective", "known_facts", "pending_todos"],
    "properties": {
        "objective": {"type": "string", "maxLength": SCRATCHPAD_LIMITS["objective"]},
        "known_facts": {"type": "array", "maxItems": SCRATCHPAD_LIMITS["items"],
                        "items": {"type": "string", "maxLength": SCRATCHPAD_LIMITS["text"]}},
        "pending_todos": {"type": "array", "maxItems": SCRATCHPAD_LIMITS["items"],
                          "items": {"type": "string", "maxLength": SCRATCHPAD_LIMITS["text"]}},
    },
}

# Structured Outputs requires an object at the root; alternatives go inside its action property.
ACTION_SCHEMA = {
    # The reply has one action object, explanatory text and the model's working notes.
    "type": "object", "additionalProperties": False,
    # All keys must exist; explanation may be null when no explanation is needed.
    "required": ["action", "explanation", "scratchpad", "applied_lessons"],
    # Keep metadata separate from the actual arguments sent to the desktop dispatcher.
    "properties": {
        # anyOf means the inner object must match one of the supported action shapes below.
        "action": {"anyOf": []},
        # Empty/null explanations are harmless metadata and are not executed.
        "explanation": {"type": ["string", "null"], "maxLength": 500},
        # Never executed: shown back to the model on its next step.
        "scratchpad": SCRATCHPAD_SCHEMA,
        # Ids of stored lessons that shaped this action; their run outcomes update memory.py.
        "applied_lessons": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 12}},
    },
}

# Build each allowed action shape from the same field definition used by validation.
for _name, _arguments in ACTION_ARGUMENTS.items():
    # A one-value enum ties this branch's arguments to exactly one action name.
    _properties = {"action": {"type": "string", "enum": [_name]}}
    # Add only this action's arguments; unrelated fields are absent, not nullable placeholders.
    _properties.update({field: ARGUMENT_SCHEMAS[field] for field in _arguments})
    # Each alternative is a complete strict object schema.
    ACTION_SCHEMA["properties"]["action"]["anyOf"].append({
        # Extra fields such as element_id on type_text are forbidden by the provider schema.
        "type": "object", "additionalProperties": False,
        # Include the operation name and only its supported arguments.
        "properties": _properties,
        # All arguments of the selected action must be supplied.
        "required": ["action", *_arguments],
    })
