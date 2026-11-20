# Keep keyboard and scroll limits in one place so the prompt-facing validator and the actual desktop functions use the same rules.
# Learning edition: comments explain the code; runtime behavior is unchanged.

# This docstring describes the purpose of the module, class, or function. It is readable through
# Python help() and is kept unchanged.
"""Shared action limits used by both model validation and desktop dispatch."""
# Store the allowed single-key names in a set, which makes membership checks straightforward.
KEYS = {"enter", "tab", "esc", "backspace", "delete", "left", "right", "up", "down",
        # Include navigation, space, rename, and refresh key names in the allowed single-key set.
        "home", "end", "pageup", "pagedown", "space", "f2", "f5"}
# Build a set of allowed key tuples. The comprehensions create related combinations; | combines
# the sets.
SHORTCUTS = {
    # Build or filter this collection with a comprehension; only items meeting its condition are
    # retained.
    ("ctrl", key) for key in ("a", "c", "v", "x", "z", "y", "s", "o", "n", "f", "l", "t", "w", "home", "end", "tab")
# Combine these allowed-shortcut sets; membership in any one of them is sufficient.
} | {("ctrl", "shift", key) for key in ("s", "z", "tab", "t")} | {
    # Allow these explicit navigation combinations as ordered key tuples.
    ("alt", "left"), ("alt", "right"), ("alt", "up"), ("shift", "tab")}


# Reject key combinations outside our explicit shortcut list before any key is pressed.
def validate_shortcut(keys):
    # Enter this branch only when not isinstance(keys, list) or any((not isinstance(k, str) for k
    # in keys)) or tuple(keys) not in SHORTCUTS. The indented statements below are conditional.
    if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys) or tuple(keys) not in SHORTCUTS:
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("Shortcut is not in the configured shortcut list")


# Address-bar text for navigate_url: a web address, never a script, file, or browser-internal URL.
def validate_url(url):
    if (not isinstance(url, str) or not 1 <= len(url) <= 500
            or any(not 33 <= ord(c) <= 126 for c in url)):
        raise ValueError("URL must be 1-500 printable ASCII characters without spaces")
    lowered = url.lower()
    if "://" in lowered:
        if not lowered.startswith(("http://", "https://")):
            raise ValueError("Only http and https URLs are allowed")
    elif lowered.split(":", 1)[0] in ("javascript", "data", "file", "vbscript", "about",
                                       "chrome", "edge", "opera", "view-source", "blob"):
        raise ValueError("Only web addresses are allowed")


# Accept only four supported directions and a small positive integer notch count.
def validate_scroll(direction, amount):
    # Enter this branch only when direction not in ('up', 'down', 'left', 'right'). The indented
    # statements below are conditional.
    if direction not in ("up", "down", "left", "right"):
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("Invalid scroll direction")
    # Check actual integer types and bounds. Python booleans behave like integers, so type(x) is
    # int deliberately excludes True and False.
    if type(amount) is not int or not 1 <= amount <= 10:
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("Scroll amount must be 1-10 wheel notches")
