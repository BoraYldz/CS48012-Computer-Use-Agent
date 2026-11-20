# Collect the current screenshot and element descriptions. Optionally ask a separate local OmniParser service to detect elements in the image.
# Learning edition: comments explain the code; runtime behavior is unchanged.

# This docstring describes the purpose of the module, class, or function. It is readable through
# Python help() and is kept unchanged.
"""Read-only observations. OmniParser is an optional, separate local service."""
# Import base64 so we can represent image bytes as text inside a JSON request.
import base64
# Import json so we can convert between Python objects and JSON text.
import json
# Import math so we can reject non-finite numbers such as NaN in coordinates.
import math
# Import Path from pathlib; this makes those names available without copying their implementation.
from pathlib import Path
# Import urllib.request so we can send the screenshot to the local OmniParser HTTP service.
import urllib.request
# Import uuid so we can create unique observation or run identifiers.
import uuid

# Import Image from PIL; this makes those names available without copying their implementation.
from PIL import Image
# Import win32gui so we can inspect windows and manage which window is in front.
import win32gui

# Import take_screenshot from desktopgui; this makes those names available without copying their
# implementation.
from desktopgui import take_screenshot
# Import inspect_active_window from ui_tree; this makes those names available without copying
# their implementation.
from ui_tree import inspect_active_window


# A changing window is retryable before any model request or input has taken place.
class ObservationChanged(RuntimeError):
    """The foreground changed while we were reading it; capture again."""


# A profile-picker window can disappear between reading its handle and its rectangle.
def foreground_bounds(handle):
    # No foreground window has no app rectangle, but a desktop screenshot can still be taken.
    if not handle:
        # None is distinct from a made-up zero-size application window.
        return None
    # Windows may destroy the old window just before this native call.
    try:
        # Keep actual desktop coordinates for the later freshness check.
        return list(win32gui.GetWindowRect(handle))
    # This wrapper only reads window geometry; it cannot catch failures from desktop input.
    except Exception:
        # Ask for a fresh observation rather than treating a stale handle as a task-scope violation.
        raise ObservationChanged("Foreground window became unavailable during capture") from None


# Desktop mode can still see and click the screen when UI Automation supplies no controls.
def capture_desktop_observation(use_omni=False):
    # Zero is meaningful: Windows has no foreground app at this instant.
    handle = win32gui.GetForegroundWindow()
    # Track the real window rectangle for freshness checks, separately from full-screen scope.
    bounds = foreground_bounds(handle)
    # Start with a usable screenshot-only observation if no UI tree can be obtained.
    tree = {"window_title": "", "window_handle": handle, "elements": [],
            "skipped_controls": 0, "controls_scanned": 0, "tree_truncated": False}
    # Record only a small diagnostic if UIA fails; never echo arbitrary exception contents.
    tree_error = None
    # The desktop screenshot remains useful even when no application is open.
    if handle:
        # Some app windows do not provide a UIA tree; that is an optional perception failure.
        try:
            # Reuse the existing bounded foreground tree scan.
            tree = inspect_active_window()
        # This try block reads UIA only; it performs no screenshot, input, or model call.
        except Exception as error:
            # A missing tree permits visual targeting; it does not pretend that elements were found.
            tree_error = type(error).__name__
    # Unlike window mode, this explicit mode captures the whole primary screen.
    screenshot = take_screenshot()
    # Read the unscaled image size so pixel coordinates stay consistent with the mouse tools.
    with Image.open(screenshot) as image:
        # The returned size is width first, then height.
        width, height = image.size
    # UIA IDs describe the foreground app only; the model also receives the full screenshot.
    elements = [dict(element, source="uia") for element in tree["elements"]]
    # Optional OmniParser can add visible elements elsewhere on the primary screen.
    if use_omni:
        # These normalized vision boxes are converted to the same pixel coordinate system.
        elements.extend(get_omni_elements(screenshot, width, height))
    # Never pair a tree from one foreground window with a screenshot of a different state.
    if (tree["window_handle"] != handle or win32gui.GetForegroundWindow() != handle
            or foreground_bounds(handle) != bounds):
        # The host can try a fresh read within its bounded observation retry count.
        raise ObservationChanged("Window changed during observation; capture again")
    # Every observation gets fresh IDs so an action cannot accidentally reuse an older screen.
    return {"observation_id": uuid.uuid4().hex, "scope": "desktop",
            "window_title": tree["window_title"], "window_handle": handle,
            "window_bounds": bounds, "screen_size": [width, height],
            "screenshot_path": screenshot, "screenshot_scope": "primary_screen",
            "elements": elements, "omniparser_used": use_omni,
            "skipped_controls": tree["skipped_controls"],
            "controls_scanned": tree.get("controls_scanned"),
            "tree_truncated": tree.get("tree_truncated", False), "uia_error": tree_error}


# Validate normalized vision boxes and convert them to pixel coordinates using the original image
# width and height.
def parse_omni_response(payload, width, height):
    # Store the result in items. This value is reused by the following steps.
    items = payload.get("parsed_content_list")
    # Enter this branch only when not isinstance(items, list). The indented statements below are
    # conditional.
    if not isinstance(items, list):
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("OmniParser response lacks parsed_content_list")
    # Store the list of element descriptions for this observation.
    elements = []
    # Repeat the indented body for each item in this sequence. The loop variable holds the current
    # item.
    for index, item in enumerate(items):
        # Store the target rectangle, in left/top/right/bottom order.
        box = item.get("bbox")
        # Enter this branch only when not isinstance(box, list) or len(box) != 4. The indented
        # statements below are conditional.
        if not isinstance(box, list) or len(box) != 4:
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Invalid OmniParser bounding box")
        # Check actual integer types and bounds. Python booleans behave like integers, so type(x)
        # is int deliberately excludes True and False.
        if any(type(v) not in (int, float) or not math.isfinite(v)
               # Supply this named argument or setting to the multiline call above. The name
               # before = tells the receiving function what it controls.
               or not 0 <= v <= 1 for v in box):
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Expected normalized OmniParser coordinates in [0, 1]")
        # Store the result in (left, top, right, bottom). This value is reused by the following
        # steps.
        left, top, right, bottom = box
        # Enter this branch only when left >= right or top >= bottom. The indented statements
        # below are conditional.
        if left >= right or top >= bottom:
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Empty/reversed OmniParser rectangle")
        # Add this item to the list, preserving the order of observations, keys, or events.
        elements.append({
            # Include the id field in this structured record; the receiver reads it by this key.
            "id": f"vision_{index}", "source": "omniparser",
            # Include the name field in this structured record; the receiver reads it by this key.
            "name": str(item.get("content", ""))[:500],
            # Include the type field: the JSON Schema type or the UI control type, according to
            # the surrounding dictionary.
            "type": str(item.get("type", "unknown")),
            # This is predicted interactability, NOT application enabled state.
            # Include the interactable field in this structured record; the receiver reads it by
            # this key.
            "interactable": item.get("interactivity") is True,
            # Include the bbox field: the rectangle coordinates used to find the target.
            "bbox": [left * width, top * height, right * width, bottom * height],
        # Finish the multiline call or collection started above; the commas keep the separate
        # arguments or items distinct.
        })
    # Return this value to the caller and finish this function call.
    return elements


# Send a base64 screenshot to localhost and translate the parser response into our common element
# format.
def get_omni_elements(screenshot_path, width, height):
    # Store the PNG bytes encoded as base64 text for JSON.
    encoded = base64.b64encode(Path(screenshot_path).read_bytes()).decode("ascii")
    # Store the HTTP request to the local OmniParser endpoint.
    request = urllib.request.Request(
        # Use the local parser endpoint on this computer; this is separate from the remote model
        # API.
        "http://127.0.0.1:8000/parse/",
        # Supply this named argument or setting to the multiline call above. The name before =
        # tells the receiving function what it controls.
        data=json.dumps({"base64_image": encoded}).encode("utf-8"),
        # Supply this named argument or setting to the multiline call above. The name before =
        # tells the receiving function what it controls.
        headers={"Content-Type": "application/json"}, method="POST",
    # Finish the multiline call or collection started above; the commas keep the separate
    # arguments or items distinct.
    )
    # Local screenshots must not be routed through an HTTP proxy.
    # Store an HTTP client that bypasses configured proxies for localhost.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # Enter a managed resource block so its file, client, or desktop lease is released
    # automatically on exit.
    with opener.open(request, timeout=60) as response:
        # Store the JSON-compatible information being sent or received.
        payload = json.load(response)
    # Return this value to the caller and finish this function call.
    return parse_omni_response(payload, width, height)


# Read the tree and screen, then ensure the foreground window and its bounds stayed consistent
# during capture.
def capture_observation(use_omni=False, scope="window"):
    # Whole-desktop capture happens only when the caller explicitly selects desktop scope.
    if scope == "desktop":
        # Keep the original masked-window path separate so its behavior stays easy to inspect.
        return capture_desktop_observation(use_omni=use_omni)
    # Reject a misspelled scope instead of accidentally broadening screenshot access.
    if scope != "window":
        # This error happens before capture, model calls, or desktop input.
        raise ValueError("Scope must be window or desktop")
    # Store the UI-tree observation before adding the screenshot.
    tree = inspect_active_window()
    # Store the Windows window identifier, called an HWND.
    handle = tree["window_handle"]
    # Store the foreground window rectangle used for consistency checks.
    bounds = list(win32gui.GetWindowRect(handle))
    # Store the saved screenshot path.
    screenshot = take_screenshot(bounds=bounds)
    # Enter a managed resource block so its file, client, or desktop lease is released
    # automatically on exit.
    with Image.open(screenshot) as image:
        # Store the result in (width, height). This value is reused by the following steps.
        width, height = image.size
    # UI Automation uses desktop coordinates, but this worker captures only the primary screen.
    left, top, right, bottom = bounds
    # A window wholly outside this image would give the model unrelated visual evidence.
    if right <= 0 or bottom <= 0 or left >= width or top >= height:
        # Stop before OmniParser or the hosted model receives this mismatched observation.
        raise RuntimeError("Target window is outside the captured primary screen; move the app to the primary monitor and retry")
    # Store the list of element descriptions for this observation.
    elements = [dict(element, source="uia") for element in tree["elements"]]
    # Enter this branch only when use_omni. The indented statements below are conditional.
    if use_omni:
        # Append the vision element records to the existing UI Automation element list.
        elements.extend(get_omni_elements(screenshot, width, height))
    # Enter this branch only when win32gui.GetForegroundWindow() != handle or
    # list(win32gui.GetWindowRect(handle)) != bounds. The indented statements below are
    # conditional.
    if (win32gui.GetForegroundWindow() != handle
            # Supply this named argument or setting to the multiline call above. The name before =
            # tells the receiving function what it controls.
            or list(win32gui.GetWindowRect(handle)) != bounds):
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ObservationChanged("Window changed during observation; capture again")
    # Return this value to the caller and finish this function call.
    return {
        # Include the observation_id field: the identity of the current screen observation.
        "observation_id": uuid.uuid4().hex,
        # Include the window_title field in this structured record; the receiver reads it by this
        # key.
        "window_title": tree["window_title"], "window_handle": handle,
        # Include the window_bounds field in this structured record; the receiver reads it by this
        # key.
        "window_bounds": bounds, "screen_size": [width, height],
        # Include the screenshot_path field in this structured record; the receiver reads it by
        # this key.
        "screenshot_path": screenshot, "elements": elements,
        # Include the omniparser_used field in this structured record; the receiver reads it by
        # this key.
        "omniparser_used": use_omni,
        # Include the skipped_controls field in this structured record; the receiver reads it by
        # this key.
        "skipped_controls": tree["skipped_controls"],
        # Outside-window pixels are black; the screenshot's dimensions and origin stay unchanged.
        "screenshot_scope": "selected_window_on_primary_screen",
        # These diagnostics help distinguish missing controls from a truncated scan.
        "controls_scanned": tree.get("controls_scanned"),
        # A partial tree can still have useful targets, but the model must not invent missing IDs.
        "tree_truncated": tree.get("tree_truncated", False),
    # Finish the multiline call or collection started above; the commas keep the separate
    # arguments or items distinct.
    }
