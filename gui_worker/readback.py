# After typing, read the field's real contents back through UI Automation and compare them with
# what the model asked to type (Lecture 3: "test runner > LLM opinion"; Lecture 4: tool results
# are fed back). A pixel change only proves *something* happened; this proves *what* arrived.
"""Read the focused control's text and check that typed text actually arrived."""
import time

# Values shown to the model and logs are bounded; the comparison itself uses the full value.
SHOW_CHARS = 200
READ_CHARS = 5000


def _normal(text):
    # Rich editors use non-breaking spaces and may wrap lines; compare words, not layout.
    return " ".join(str(text).replace("\xa0", " ").split())


def read_focused_text():
    """{"control", "value"} for the control with keyboard focus; password fields are never read.

    Returns None when nothing readable has focus (no Value or Text pattern).
    """
    from pywinauto.uia_defines import IUIA, get_elem_interface, NoPatternInterfaceError
    element = IUIA().iuia.GetFocusedElement()
    if element is None:
        return None
    try:
        if element.CurrentIsPassword:
            return {"control": "password field", "value": None, "password": True}
        label = f"{element.CurrentLocalizedControlType or 'control'} '{(element.CurrentName or '')[:60]}'"
    except Exception:
        label = "focused control"
    value = None
    try:
        value = get_elem_interface(element, "Value").CurrentValue
    except (NoPatternInterfaceError, Exception):
        value = None
    if not value:
        # Documents and rich editors (e.g. an e-mail body) expose their text through TextPattern.
        try:
            value = get_elem_interface(element, "Text").DocumentRange.GetText(READ_CHARS)
        except (NoPatternInterfaceError, Exception):
            pass
    if value is None:
        return None
    return {"control": label, "value": str(value)[:READ_CHARS]}


def check_typed(expected, settle=0.15, reader=read_focused_text):
    """Compare intended text with what the focused field now contains.

    status: "match", "mismatch", or "unavailable" (no readable field, or a password field).
    Containment, not equality: the field may hold earlier text, autocomplete or a signature.
    """
    time.sleep(settle)
    try:
        field = reader()
    except Exception:
        field = None
    if not field or field.get("password") or field.get("value") is None:
        return {"status": "unavailable", "expected": expected[:SHOW_CHARS],
                "reason": "password field" if field and field.get("password") else "field text not readable"}
    actual = field["value"]
    status = "match" if _normal(expected) in _normal(actual) else "mismatch"
    shown = actual if len(actual) <= SHOW_CHARS else "..." + actual[-SHOW_CHARS:]
    return {"status": status, "expected": expected[:SHOW_CHARS], "actual": shown,
            "field": field.get("control")}
