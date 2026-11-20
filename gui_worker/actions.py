# Turn an already validated low-level action dictionary into a call to our desktop functions. Application and window actions are handled separately by WindowSession.
# Learning edition: comments explain the code; runtime behavior is unchanged.

# Import click, type_text, press_key, take_screenshot, hotkey, scroll from desktopgui; this makes
# those names available without copying their implementation.
from desktopgui import click, type_text, press_key, take_screenshot, hotkey, scroll
# A wait action gives a page time to load without clicking or typing anything.
import time
# The dispatcher re-checks URLs even though the worker validated them first.
from input_policy import validate_url
# After typing, the field's real contents are compared with the intended text.
from readback import check_typed


# Select the matching desktop function from the action name. No eval or generated Python is used.
def execute_action(action):
    # This docstring describes the purpose of the module, class, or function. It is readable
    # through Python help() and is kept unchanged.
    """Validate an action and call the corresponding desktop function."""

    # Enter this branch only when not isinstance(action, dict). The indented statements below are
    # conditional.
    if not isinstance(action, dict):
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("An action must be a dictionary.")

    # Store the operation name used to select a desktop function.
    action_name = action.get("action")

    # Enter this branch only when action_name == 'click'. The indented statements below are
    # conditional.
    if action_name == "click":
        # Store the result in x. This value is reused by the following steps.
        x = action.get("x")
        # Store the result in y. This value is reused by the following steps.
        y = action.get("y")

        # Check actual integer types and bounds. Python booleans behave like integers, so type(x)
        # is int deliberately excludes True and False.
        if type(x) is not int or type(y) is not int:
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Click coordinates must be integers.")

        # Click at the coordinates extracted and checked above; this is where the dispatcher
        # reaches the real desktop.
        click(x, y)

    # Enter this branch only when action_name == 'type_text'. The indented statements below are
    # conditional.
    elif action_name == "type_text":
        # Store the result in text. This value is reused by the following steps.
        text = action.get("text")

        # Enter this branch only when not isinstance(text, str). The indented statements below are
        # conditional.
        if not isinstance(text, str):
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("The text must be a string.")

        # Send the validated string to our typing function; keyboard focus determines which app
        # receives it.
        type_text(text)
        # Read the field back: did exactly this text arrive?
        return {"status": "executed", "action": action_name, "readback": check_typed(text)}

    # Click the validated input, optionally replace its text, type, and optionally submit.
    elif action_name == "type_into":
        x, y, text = action.get("x"), action.get("y"), action.get("text")
        if (type(x) is not int or type(y) is not int or not isinstance(text, str)
                or type(action.get("clear")) is not bool or type(action.get("submit")) is not bool):
            raise ValueError("type_into needs integer coordinates, text, and boolean clear/submit")
        click(x, y)
        # Give the field time to take focus before keystrokes arrive.
        time.sleep(0.2)
        if action["clear"]:
            # Select the field's existing text so typing replaces it.
            hotkey(["ctrl", "a"])
        type_text(text)
        # Read the field back before Enter, while it still holds the text (a search box empties
        # itself after submitting).
        readback = check_typed(text)
        if action["submit"]:
            press_key("enter")
        return {"status": "executed", "action": action_name, "readback": readback}

    # Browser address bar: the same three keystrokes a person would use.
    elif action_name == "navigate_url":
        url = action.get("url")
        validate_url(url)
        hotkey(["ctrl", "l"])
        time.sleep(0.2)
        type_text(url)
        press_key("enter")

    # Enter this branch only when action_name == 'press_key'. The indented statements below are
    # conditional.
    elif action_name == "press_key":
        # Read the keyboard key name, such as enter or esc, from the action dictionary.
        key = action.get("key")

        # Enter this branch only when not isinstance(key, str). The indented statements below are
        # conditional.
        if not isinstance(key, str):
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("The key must be a string.")

        # Send the key name to our keyboard helper for an actual press and release.
        press_key(key)

    # Enter this branch only when action_name == 'hotkey'. The indented statements below are
    # conditional.
    elif action_name == "hotkey":
        # Send the requested combination to the helper that validates it and releases all held
        # keys afterward.
        hotkey(action.get("keys"))

    # Enter this branch only when action_name == 'scroll'. The indented statements below are
    # conditional.
    elif action_name == "scroll":
        # Pass direction, notch count, and resolved target coordinates to the scrolling helper.
        scroll(action.get("direction"), action.get("amount"), action.get("x"), action.get("y"))

    # Enter this branch only when action_name == 'screenshot'. The indented statements below are
    # conditional.
    # Keep wait validation here too, since this dispatcher can be called without the model loop.
    elif action_name == "wait":
        # Use a small integer duration so the caller cannot accidentally request a huge pause.
        seconds = action.get("seconds")
        # The action must contain only its operation name and bounded duration.
        if set(action) != {"action", "seconds"} or type(seconds) is not int or not 1 <= seconds <= 3:
            # No waiting or other desktop action happens on an invalid request.
            raise ValueError("Wait must contain an integer duration from 1 to 3 seconds")
        # The worker checks its deadline again and takes a fresh screenshot after this returns.
        time.sleep(seconds)

    elif action_name == "screenshot":
        # Store the local filesystem location used by this operation.
        path = take_screenshot()
        # Return this value to the caller and finish this function call.
        return {"status": "executed", "screenshot_path": path}

    # Handle the remaining case when the preceding condition did not match.
    else:
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError(f"Unsupported action: {action_name}")

    # Return this value to the caller and finish this function call.
    return {"status": "executed", "action": action_name}
