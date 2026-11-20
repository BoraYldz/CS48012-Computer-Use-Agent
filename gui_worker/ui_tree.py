# Inspect application-supplied UI controls without clicking or contacting a model.
"""Bounded UI Automation inspection that reaches nested browser controls."""
# A queue lets us inspect each level of the tree before moving to the next level.
from collections import deque
# JSON makes the standalone test readable in PowerShell.
import json
# Use a monotonic deadline and a countdown for the standalone test.
import time
# This Windows API identifies the foreground window.
import win32gui
# pywinauto wraps native UI Automation controls as Python objects.
from pywinauto import Desktop

# Web search boxes can sit below many layout containers; the YouTube scan was truncated at twelve.
# Allow deeper inspection while keeping the existing node-count and time limits below.
MAX_DEPTH = 24
# Bound the number of inspected nodes, including layout containers (fallback walker only).
MAX_NODES = 400
# Bound the amount of UI text kept in an observation. The planner sends a smaller ranked subset.
MAX_ELEMENTS = 150
# This deadline is checked between UIA calls; it cannot interrupt a blocked Windows call.
SCAN_SECONDS = 4.0
# Prefer these controls over generic layout panes when the returned list must be shortened.
INTERACTIVE_TYPES = {"Button", "Hyperlink", "Edit", "ComboBox", "CheckBox",
                     "RadioButton", "MenuItem", "ListItem", "TabItem", "TreeItem"}
# Unnamed, nonfocusable elements of these types are layout scaffolding, not targets.
LAYOUT_TYPES = {"Pane", "Group", "Custom", "Text", "Image", "Separator", "ToolBar", "Tab"}


# Cached FindAll: one cross-process call returns every on-screen descendant with its properties.
# The old one-node-at-a-time walk spent its 400-node budget on browser toolbars before reaching
# page links (the YouTube results page never exposed a single video link). This takes ~0.1-0.4 s.
def _cached_scan(handle, window_rect):
    # pywinauto already owns the IUIAutomation COM object; reuse it rather than creating another.
    from pywinauto.uia_defines import IUIA
    iuia = IUIA()
    uia, dll = iuia.iuia, iuia.UIA_dll
    # Ask the provider to send these properties with each element, avoiding per-property round trips.
    request = uia.CreateCacheRequest()
    for property_id in (dll.UIA_NamePropertyId, dll.UIA_ControlTypePropertyId,
                        dll.UIA_BoundingRectanglePropertyId, dll.UIA_IsEnabledPropertyId,
                        dll.UIA_IsKeyboardFocusablePropertyId, dll.UIA_HasKeyboardFocusPropertyId):
        request.AddProperty(property_id)
    root = uia.ElementFromHandle(handle)
    # Elements the provider marks off-screen (below the fold, hidden tabs) are never useful targets.
    condition = uia.CreatePropertyCondition(dll.UIA_IsOffscreenPropertyId, False)
    found = root.FindAllBuildCache(dll.TreeScope_Descendants, condition, request)
    left, top, right, bottom = window_rect
    candidates = []
    for index in range(found.Length):
        element = found.GetElement(index)
        rect = element.CachedBoundingRectangle
        # Keep only positive-size rectangles that overlap the window and the primary screen origin.
        if (rect.right <= rect.left or rect.bottom <= rect.top or rect.right <= max(left, 0)
                or rect.bottom <= max(top, 0) or rect.left >= right or rect.top >= bottom):
            continue
        name = (element.CachedName or "")[:300]
        control_type = iuia.known_control_type_ids.get(element.CachedControlType, "Unknown")
        focusable, focused = bool(element.CachedIsKeyboardFocusable), bool(element.CachedHasKeyboardFocus)
        # Unnamed layout nodes add tokens without giving the model anything to act on.
        if control_type in LAYOUT_TYPES and not name.strip() and not focusable and not focused:
            continue
        candidates.append({"name": name, "type": control_type,
                           "enabled": bool(element.CachedIsEnabled),
                           "keyboard_focusable": focusable, "focused": focused,
                           "bbox": [rect.left, rect.top, rect.right, rect.bottom]})
    return candidates, found.Length


# Missing focus properties should not discard an otherwise useful control.
def _focus_properties(control):
    # Ask the app whether this control accepts keyboard focus and currently has it.
    try:
        # Convert UIA values to ordinary JSON-compatible booleans.
        return bool(control.is_keyboard_focusable()), bool(control.has_keyboard_focus())
    # Controls can disappear or omit these optional properties.
    except Exception:
        # Null means unknown, rather than incorrectly asserting a focus state.
        return None, None


# Read one window (the foreground by default). Prefer the fast cached scan; fall back to the walker.
def inspect_active_window(handle=None):
    # The handle identifies the exact window being inspected.
    handle = handle or win32gui.GetForegroundWindow()
    # Do not guess a window if Windows reports no foreground application.
    if not handle:
        # The caller can report this controlled failure without executing an action.
        raise RuntimeError("No foreground window was found.")
    # Resolve the native UI Automation wrapper for that window.
    window = Desktop(backend="uia").window(handle=handle).wrapper_object()
    try:
        candidates, scanned = _cached_scan(handle, win32gui.GetWindowRect(handle))
        skipped, truncated, method = 0, False, "cached_findall"
    except Exception:
        # Some providers reject cached FindAll; the bounded walker still gives a partial tree.
        candidates, scanned, skipped, truncated = _walk_scan(window)
        method = "walk"
    # Browsers expose the same link twice (thumbnail and title share a name); keep one per box.
    unique, seen = [], set()
    for item in candidates:
        key = (item["type"], item["name"], tuple(item["bbox"]))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    truncated = truncated or len(unique) > MAX_ELEMENTS
    # Focus first, then enabled controls, then named items; ties keep on-screen reading order.
    unique.sort(key=lambda item: (
        item["focused"] is not True,
        not (item["enabled"] and (item["type"] in INTERACTIVE_TYPES or item["keyboard_focusable"])),
        not bool(item["name"]), item["bbox"][1] // 8, item["bbox"][0]))
    # Assign fresh IDs after sorting; IDs belong only to this observation.
    elements = [dict(item, id=f"uia_{index}") for index, item in enumerate(unique[:MAX_ELEMENTS])]
    return {"window_title": window.window_text(), "window_handle": window.handle,
            "elements": elements, "skipped_controls": skipped,
            "controls_scanned": scanned, "tree_truncated": truncated,
            "max_tree_depth": MAX_DEPTH, "scan_method": method}


# Original bounded breadth-first walker, kept as a fallback for providers without cached FindAll.
def _walk_scan(window):
    # Queue entries contain a control and its depth; the root starts at zero.
    pending = deque([(window, 0)])
    # Candidates stores visible controls; scanned also counts invisible nodes and containers.
    candidates, scanned, skipped = [], 0, 0
    # Report when the limits prevent a complete tree walk.
    truncated = False
    # A monotonic clock is unaffected by changes to the system time.
    deadline = time.monotonic() + SCAN_SECONDS
    # Process queued controls while the count and time limits permit more work.
    while pending and scanned < MAX_NODES and time.monotonic() < deadline:
        # Take the oldest queued node to inspect the tree breadth first.
        control, depth = pending.popleft()
        # Count every inspected node, including unavailable ones.
        scanned += 1
        # The root is already described by window_title and window_handle.
        if depth:
            # App controls may disappear between the tree walk and property queries.
            try:
                # Read the control's rectangle in desktop coordinates.
                rect = control.rectangle()
                # Only visible, positive-size controls are useful screen targets.
                if control.is_visible() and rect.width() > 0 and rect.height() > 0:
                    # Focus metadata helps the model reason about a preceding Tab key.
                    focusable, focused = _focus_properties(control)
                    # Keep labels bounded instead of copying unlimited app text into the prompt.
                    candidates.append({"name": (control.element_info.name or "")[:300],
                        # A Button and a generic Pane serve different purposes.
                        "type": control.element_info.control_type,
                        # Preserve the actual enabled state; never turn disabled into enabled.
                        "enabled": bool(control.is_enabled()),
                        # Unknown properties remain null in the observation.
                        "keyboard_focusable": focusable, "focused": focused,
                        # Coordinates keep the primary screenshot's original origin.
                        "bbox": [rect.left, rect.top, rect.right, rect.bottom]})
            # Preserve other controls if one disappears or fails to respond.
            except Exception:
                # Make incomplete inspection visible to the caller.
                skipped += 1
        # Do not descend beyond the configured depth, even if children might exist.
        if depth >= MAX_DEPTH:
            # This conservative flag indicates that deeper descendants may have been omitted.
            truncated = True
            # Continue with the next queued control.
            continue
        # Enumerate immediate children lazily instead of fetching all descendants at once.
        try:
            # Do not prune disabled containers; they can contain separately enabled children.
            for child in control.iter_children():
                # Bound queued work and check time within wide child lists as well.
                if scanned + len(pending) >= MAX_NODES or time.monotonic() >= deadline:
                    # There may be more controls than this scan can inspect.
                    truncated = True
                    # Stop enumerating this parent's children.
                    break
                # Inspect the child later, one level deeper than its parent.
                pending.append((child, depth + 1))
        # A provider can stop responding as a window renders or closes.
        except Exception:
            # Keep the successfully read portion and report the incomplete read.
            skipped += 1
    # Pending nodes also indicate incomplete coverage; sorting and IDs are applied by the caller.
    return candidates, scanned, skipped, truncated or bool(pending)


# Running this file directly is a read-only tree inspection, with no API request.
def main():
    # Give you time to focus the window whose controls you want to inspect.
    print("Switch to the window you want to inspect within 5 seconds. No API call or GUI input.")
    # Wait without moving the pointer or pressing any key.
    time.sleep(5)
    # Print the tree so you can check whether the actual buttons are exposed.
    print(json.dumps(inspect_active_window(), indent=2, ensure_ascii=False))


# Importing the module only defines functions; it does not inspect the desktop.
if __name__ == "__main__":
    # Start the standalone inspection when the file is launched directly.
    main()
