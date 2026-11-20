# Perform the actual mouse, keyboard, screenshot, and wheel operations. The higher layers decide what to do; this layer turns a request into desktop input.
# Learning edition: comments explain the code; runtime behavior is unchanged.

# Import Path from pathlib; this makes those names available without copying their implementation.
from pathlib import Path
# Import datetime from datetime; this makes those names available without copying their
# implementation.
from datetime import datetime
# Import ctypes so we can call Windows functions with explicit C-compatible types.
import ctypes
# Import wintypes from ctypes; this makes those names available without copying their
# implementation.
from ctypes import wintypes
# Import time so we can measure elapsed time and give the desktop brief pauses.
import time

# Import pyautogui so we can generate desktop input and capture the screen.
import pyautogui
# Image makes a blank canvas when keeping only the selected window's visible screen area.
from PIL import Image
# Import validate_shortcut, validate_scroll from input_policy; this makes those names available
# without copying their implementation.
from input_policy import validate_shortcut, validate_scroll


# Set PyAutoGUI corner-stop behavior. False is used only briefly during key releases so held
# modifiers can be cleaned up.
pyautogui.FAILSAFE = True
# Pause half a second after PyAutoGUI calls so desktop operations are not sent back-to-back too
# quickly.
pyautogui.PAUSE = 0.5

# Store the result in OUTPUT_FOLDER. This value is reused by the following steps.
OUTPUT_FOLDER = Path(__file__).resolve().parent / "outputs"


# Save a timestamped PNG and return its path. The path is evidence metadata, not the image bytes
# sent to a model.
def take_screenshot(bounds=None):
    # Capture into memory first; task observations must never save the unmasked background.
    captured = pyautogui.screenshot()
    # Standalone screenshots can omit bounds; model observations always supply the task window.
    if bounds is not None:
        # Refuse malformed rectangles instead of falling back to a full-screen image.
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
                or any(type(value) is not int for value in bounds)):
            # These bounds come from Windows, not model-proposed click coordinates.
            raise ValueError("Screenshot bounds must contain four integers")
        # Clip window borders to the actual primary-screen image dimensions.
        left, top, right, bottom = bounds
        # Only the visible part of a partly off-screen window can be copied.
        visible = (max(0, left), max(0, top), min(captured.width, right), min(captured.height, bottom))
        # A wholly off-screen window must not cause another application's pixels to be saved.
        if visible[0] >= visible[2] or visible[1] >= visible[3]:
            # Stop before creating a screenshot file or sending an observation to the model.
            raise RuntimeError("Target window is outside the captured primary screen; move the app to the primary monitor and retry")
        # Keep the original dimensions so existing click coordinates need no conversion.
        masked = Image.new("RGB", captured.size, "black")
        # Copy just the selected window rectangle back to its original desktop position.
        masked.paste(captured.crop(visible), visible[:2])
        # This masked image is the only image saved and later sent to the model or OmniParser.
        captured = masked
    # Create the directory if needed; exist_ok avoids an error when it already exists.
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

    # Store the timestamp used to avoid overwriting earlier screenshots.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    # Store the local filesystem location used by this operation.
    path = OUTPUT_FOLDER / f"screenshot_{timestamp}.png"

    # Capture the screen and write the resulting image as a PNG at the path above.
    captured.save(path)
    # Return this value to the caller and finish this function call.
    return str(path)


# Replace the old fixed 0.3 s pause: poll small in-memory frames until two in a row match, so the
# next observation shows the loaded result instead of a half-rendered page. Nothing is saved.
def wait_until_stable(timeout=2.5, interval=0.25, minimum=0.3, tolerance=12, max_changed=0.0005):
    from PIL import ImageChops
    deadline = time.monotonic() + timeout
    time.sleep(minimum)
    previous = pyautogui.screenshot().convert("L").resize((240, 135))
    while time.monotonic() < deadline:
        time.sleep(interval)
        current = pyautogui.screenshot().convert("L").resize((240, 135))
        changed = ImageChops.difference(previous, current).point(lambda v: 255 if v > tolerance else 0)
        if changed.histogram()[255] / (240 * 135) <= max_changed:
            return True
        previous = current
    # A playing video never settles; the caller simply observes after the timeout.
    return False


# Check that the coordinates are on the screen, then issue a mouse click.
def click(x, y):
    # Enter this branch only when not pyautogui.onScreen(x, y). The indented statements below are
    # conditional.
    if not pyautogui.onScreen(x, y):
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError(f"Coordinates are outside the screen: ({x}, {y})")

    # Issue the actual click at the validated desktop coordinates.
    pyautogui.click(x=x, y=y)


# Native SendInput structures. Each character is sent as a Unicode code unit (KEYEVENTF_UNICODE),
# so the result does not depend on the keyboard layout. pyautogui.write assumed a US layout:
# on Turkish Q it pressed Ctrl+Alt+SHIFT+Q for "@" (AltGr+Q is right), and "@" vanished.
_KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE, _INPUT_KEYBOARD = 0x0002, 0x0004, 1


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    # The mouse member is the largest, so the union has the size Windows expects.
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def _send_code_unit(unit):
    events = (_INPUT * 2)()
    for event, flags in zip(events, (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)):
        event.type = _INPUT_KEYBOARD
        event.union.ki = _KEYBDINPUT(0, unit, flags, 0, 0)
    sent = ctypes.windll.user32.SendInput(2, events, ctypes.sizeof(_INPUT))
    if sent != 2:
        raise RuntimeError("Windows rejected the keyboard input (another app may block input)")


# Type the supplied characters into whichever window currently owns keyboard focus.
def type_text(text, interval=0.02):
    # Characters outside the basic plane (e.g. emoji) are two UTF-16 code units.
    encoded = text.encode("utf-16-le")
    for index in range(0, len(encoded), 2):
        # Moving the mouse to a screen corner still stops the agent, as with every other action.
        pyautogui.failSafeCheck()
        _send_code_unit(int.from_bytes(encoded[index:index + 2], "little"))
        time.sleep(interval)
    time.sleep(pyautogui.PAUSE)


# Press and release one named key after checking PyAutoGUI recognizes it.
def press_key(key):
    # Enter this branch only when key not in pyautogui.KEYBOARD_KEYS. The indented statements
    # below are conditional.
    if key not in pyautogui.KEYBOARD_KEYS:
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError(f"Unknown key: {key}")

    # Press and release this one keyboard key in the currently focused application.
    pyautogui.press(key)


# Press a combination in order and release it in reverse order, including when a failure
# interrupts the shortcut.
def hotkey(keys):
    # Check the shortcut list before a key-down event can happen.
    validate_shortcut(keys)
    # Share the key-release logic with the host-controlled Start-menu helper below.
    _press_combination(keys)


# This private helper also supports fixed host shortcuts that models cannot request freely.
def _press_combination(keys):
    # Store the keys that need releasing if a shortcut is interrupted.
    pressed = []
    # Attempt the following operations while allowing the nearby except/finally blocks to handle
    # failure and cleanup.
    try:
        # Repeat the indented body for each item in this sequence. The loop variable holds the
        # current item.
        for key in keys:
            # Add this item to the list, preserving the order of observations, keys, or events.
            pressed.append(key)
            # Press this key and keep it held until the cleanup loop releases it.
            pyautogui.keyDown(key)
    # Always perform this cleanup, whether the preceding work succeeded, failed, or was
    # interrupted.
    finally:
        # Release modifiers even if FAILSAFE fires partway through the shortcut.
        # Temporarily disabling it is limited to releases, never new key presses.
        # Store the original PyAutoGUI failsafe setting.
        failsafe = pyautogui.FAILSAFE
        # Set PyAutoGUI corner-stop behavior. False is used only briefly during key releases so
        # held modifiers can be cleaned up.
        pyautogui.FAILSAFE = False
        # Attempt the following operations while allowing the nearby except/finally blocks to
        # handle failure and cleanup.
        try:
            # Repeat the indented body for each item in this sequence. The loop variable holds the
            # current item.
            for key in reversed(pressed):
                # Release a key that was held as part of the shortcut.
                pyautogui.keyUp(key)
        # Always perform this cleanup, whether the preceding work succeeded, failed, or was
        # interrupted.
        finally:
            # Set PyAutoGUI corner-stop behavior. False is used only briefly during key releases
            # so held modifiers can be cleaned up.
            pyautogui.FAILSAFE = failsafe


# Ctrl+Escape opens Start without granting arbitrary Windows shortcuts to model actions.
def open_start_menu():
    # The fixed shortcut uses the same finally-based key cleanup as ordinary hotkeys.
    _press_combination(["ctrl", "esc"])


# Move the pointer over the intended element and send a bounded number of wheel notches.
def scroll(direction, amount, x, y):
    # Check the direction and bounded wheel count before scrolling.
    validate_scroll(direction, amount)
    # Check actual integer types and bounds. Python booleans behave like integers, so type(x) is
    # int deliberately excludes True and False.
    if type(x) is not int or type(y) is not int or not pyautogui.onScreen(x, y):
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("Scroll target outside screen")
    # Move the pointer over the validated target before sending the wheel event.
    pyautogui.moveTo(x, y)
    # Store a positive or negative notch count encoding scroll direction.
    signed = amount if direction in ("up", "right") else -amount
    # Enter this branch only when direction in ('up', 'down'). The indented statements below are
    # conditional.
    if direction in ("up", "down"):
        # Send vertical wheel notches; positive means up and negative means down.
        pyautogui.scroll(signed)
    # Handle the remaining case when the preceding condition did not match.
    else:
        # Installed PyAutoGUI's Windows _hscroll delegates to vertical _scroll.
        # Send a horizontal wheel event with the signed notch count chosen above.
        horizontal_wheel(signed)


# Use a native Windows horizontal-wheel event because this installed PyAutoGUI backend maps
# hscroll to vertical scrolling.
def horizontal_wheel(notches):
    # Check the emergency mouse-corner stop before sending native input.
    pyautogui.failSafeCheck()
    # Store the native Windows function for sending the wheel event.
    mouse_event = ctypes.windll.user32.mouse_event
    # Describe native argument/return types for ctypes so Windows receives correctly sized values,
    # including 64-bit handles.
    mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                            # The last arguments are a 32-bit wheel delta and pointer-sized extra
                            # information.
                            wintypes.DWORD, ctypes.c_size_t]
    # Send MOUSEEVENTF_HWHEEL (0x01000). Windows uses 120 units per notch; the mask encodes a
    # negative delta in a 32-bit unsigned argument.
    mouse_event(0x01000, 0, 0, (notches * 120) & 0xFFFFFFFF, 0)
    # Wait briefly without changing the desktop; this gives the app time to update.
    time.sleep(pyautogui.PAUSE)
