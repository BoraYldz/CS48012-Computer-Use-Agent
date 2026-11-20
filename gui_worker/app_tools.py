# Launch known applications and track which Windows windows this task may use. The registry is configuration we control, not executable code supplied by the model.
# Learning edition: comments explain the code; runtime behavior is unchanged.

# This docstring describes the purpose of the module, class, or function. It is readable through
# Python help() and is kept unchanged.
"""Application registry and explicit foreground-window transitions. No shell."""
# Import ctypes so we can call Windows functions with explicit C-compatible types.
import ctypes
# Read only standard installation directories; no model supplies executable locations.
import os
# Import wintypes from ctypes; this makes those names available without copying their
# implementation.
from ctypes import wintypes
# Import Path from pathlib; this makes those names available without copying their implementation.
from pathlib import Path
# Import subprocess so we can start a configured application as a separate process.
import subprocess
# Import time so we can measure elapsed time and give the desktop brief pauses.
import time

# Import win32con so we can use named Windows constants instead of guessing numeric flags.
import win32con
# Import win32gui so we can inspect windows and manage which window is in front.
import win32gui
# Import win32process so we can look up the process that owns a window.
import win32process
# pywin32 reports a refused native activation as this exception type on some Windows versions.
import pywintypes


# Ask Windows for its installation directory so launcher paths do not assume Windows is installed
# at a guessed location.
def windows_directory():
    # Store a writable character buffer for a Windows API result.
    buffer = ctypes.create_unicode_buffer(32768)
    # Enter this branch only when not ctypes.windll.kernel32.GetWindowsDirectoryW(buffer,
    # len(buffer)). The indented statements below are conditional.
    if not ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)):
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise RuntimeError("Cannot locate Windows directory")
    # Return this value to the caller and finish this function call.
    return Path(buffer.value)


# Extend this host-controlled registry for installed apps. Models choose IDs only.
# Map approved app IDs to fixed executable arguments and expected process names. Models never edit
# this mapping.
APP_REGISTRY = {
    # Include the notepad field in this structured record; the receiver reads it by this key.
    "notepad": {"argv": [str(windows_directory() / "System32" / "notepad.exe")],
                # Include the process_names field in this structured record; the receiver reads it
                # by this key.
                "process_names": {"notepad.exe"}},
    # Include the calculator field in this structured record; the receiver reads it by this key.
    "calculator": {"argv": [str(windows_directory() / "System32" / "calc.exe")],
                   # Include the process_names field in this structured record; the receiver reads
                   # it by this key.
                   "process_names": {"calculatorapp.exe", "calculator.exe", "calc.exe"}},
    # Include the explorer field in this structured record; the receiver reads it by this key.
    "explorer": {"argv": [str(windows_directory() / "explorer.exe")],
                 # Include the process_names field in this structured record; the receiver reads
                 # it by this key.
                 "process_names": {"explorer.exe"}},
# Finish the multiline call or collection started above; the commas keep the separate arguments or
# items distinct.
}

# These are search labels typed into Start by the GUI launcher, never shell commands.
GUI_SEARCH_NAMES = {"notepad": "Notepad", "calculator": "Calculator", "explorer": "File Explorer"}
# Check standard locations so absent browsers are not advertised as available tools.
# Chromium browsers expose page content to UI Automation fully only with this flag. It applies when
# the direct launcher starts a fresh browser process; an already running browser keeps its mode.
_CHROMIUM_FLAGS = ["--force-renderer-accessibility"]
for _app_id, _label, _relative, _process, _flags in (
    # Chrome and Edge are selected by short IDs just like the original registered apps.
    ("chrome", "Google Chrome", Path("Google/Chrome/Application/chrome.exe"), "chrome.exe", _CHROMIUM_FLAGS),
    # The executable path may be under either Program Files directory.
    ("edge", "Microsoft Edge", Path("Microsoft/Edge/Application/msedge.exe"), "msedge.exe", _CHROMIUM_FLAGS),
    # Opera installs per user under LOCALAPPDATA\Programs.
    ("opera", "Opera", Path("Programs/Opera/opera.exe"), "opera.exe", _CHROMIUM_FLAGS),
    ("firefox", "Firefox", Path("Mozilla Firefox/firefox.exe"), "firefox.exe", []),
):
    # Per-user installations can also live under LOCALAPPDATA.
    for _base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        # Ignore unavailable environment entries and nonexistent installations.
        if _base and (Path(_base) / _relative).is_file():
            # The registry records a fixed host-discovered executable, not generated text.
            APP_REGISTRY[_app_id] = {"argv": [str(Path(_base) / _relative), *_flags], "process_names": {_process}}
            # Store the matching human-facing search term for the GUI route.
            GUI_SEARCH_NAMES[_app_id] = _label
            # Use the first existing installation for this app.
            break


# Return the process ID and executable path behind a window. Checking both helps detect a stale
# window handle reused by another process.
def process_identity(handle):
    # This docstring describes the purpose of the module, class, or function. It is readable
    # through Python help() and is kept unchanged.
    """PID and executable path: handle identity is rechecked before focus changes."""
    # Store the result in (_, pid). This value is reused by the following steps.
    _, pid = win32process.GetWindowThreadProcessId(handle)
    # Store the result in kernel. This value is reused by the following steps.
    kernel = ctypes.windll.kernel32
    # Describe native argument/return types for ctypes so Windows receives correctly sized values,
    # including 64-bit handles.
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    # Describe native argument/return types for ctypes so Windows receives correctly sized values,
    # including 64-bit handles.
    kernel.OpenProcess.restype = wintypes.HANDLE
    # Describe native argument/return types for ctypes so Windows receives correctly sized values,
    # including 64-bit handles.
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                # The remaining native arguments are a writable
                                                # Unicode string buffer and a pointer to its size.
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    # Describe native argument/return types for ctypes so Windows receives correctly sized values,
    # including 64-bit handles.
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # Store the native process handle that must be closed after the lookup.
    process = kernel.OpenProcess(0x1000, False, pid)
    # Enter this branch only when not process. The indented statements below are conditional.
    if not process:
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise RuntimeError("Cannot identify target process")
    # Attempt the following operations while allowing the nearby except/finally blocks to handle
    # failure and cleanup.
    try:
        # Store the native size value passed by reference to Windows.
        size = wintypes.DWORD(32768)
        # Store a writable character buffer for a Windows API result.
        buffer = ctypes.create_unicode_buffer(size.value)
        # Enter this branch only when not kernel.QueryFullProcessImageNameW(process, 0, buffer,
        # ctypes.byref(size)). The indented statements below are conditional.
        if not kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise RuntimeError("Cannot read target executable")
        # Return this value to the caller and finish this function call.
        return pid, buffer.value.lower()
    # Always perform this cleanup, whether the preceding work succeeded, failed, or was
    # interrupted.
    finally:
        # Close the native process handle acquired above, even if querying the executable failed.
        kernel.CloseHandle(process)


# navigate_url presses Ctrl+L, which only means "address bar" in a browser.
BROWSER_EXECUTABLES = {"chrome.exe", "msedge.exe", "opera.exe", "firefox.exe", "brave.exe", "vivaldi.exe"}


# True when the window belongs to a known browser process; unreadable windows are not browsers.
def is_browser_window(handle):
    try:
        return bool(handle) and Path(process_identity(handle)[1]).name in BROWSER_EXECUTABLES
    except (RuntimeError, OSError, pywintypes.error):
        return False


# Collect visible, titled windows and their process information. This reads metadata; it does not
# activate any window.
def list_windows():
    # Store the result in windows. This value is reused by the following steps.
    windows = []
    # Use this callback once for each window passed to us by Windows EnumWindows.
    def visit(handle, _):
        # Enter this branch only when win32gui.IsWindowVisible(handle) and
        # win32gui.GetWindowText(handle). The indented statements below are conditional.
        if win32gui.IsWindowVisible(handle) and win32gui.GetWindowText(handle):
            # Attempt the following operations while allowing the nearby except/finally blocks to
            # handle failure and cleanup.
            try:
                # Store the result in (pid, executable). This value is reused by the following
                # steps.
                pid, executable = process_identity(handle)
                # Add this item to the list, preserving the order of observations, keys, or
                # events.
                windows.append({"window_handle": handle, "pid": pid,
                    # Include the executable field in this structured record; the receiver reads
                    # it by this key.
                    "executable": executable, "title": win32gui.GetWindowText(handle)[:300]})
            # Handle this failure path without continuing with an unsafe or incomplete operation.
            except (RuntimeError, OSError):
                # Intentionally do nothing in this branch; an inaccessible or disallowed window is
                # simply not accepted.
                pass
    # Ask Windows to call visit for each top-level window; visit collects the usable entries.
    win32gui.EnumWindows(visit, None)
    # Return this value to the caller and finish this function call.
    return windows


# A normal Windows focus refusal should be reported to the planner, not end the whole GUI attempt.
class ForegroundNotAcquired(RuntimeError):
    """The requested window did not become foreground; a new observation is required."""

    def __init__(self, target_handle, actual_handle, *, restored=False, api_error=False):
        # Keep the human explanation fixed rather than copying an arbitrary native error message.
        super().__init__("Requested window did not become foreground")
        # These handles describe the attempted transition for logs and the caller's reflection step.
        self.target_handle, self.actual_handle = target_handle, actual_handle
        # Restoring a window can have an effect even when the overall focus tool fails.
        self.restored, self.api_error = restored, api_error


# Request foreground focus once, then briefly observe whether Windows actually grants it.
def focus_window(handle):
    # Enter this branch only when not win32gui.IsWindow(handle). The indented statements below are
    # conditional.
    if not win32gui.IsWindow(handle):
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("Window no longer exists")
    # An already focused window needs no activation request at all.
    if win32gui.GetForegroundWindow() == handle:
        # Avoid turning a harmless no-op into an unnecessary native focus failure.
        return
    # Remember whether this action restored a minimized window for failure diagnostics.
    restored = bool(win32gui.IsIconic(handle))
    # A minimized window must be visible before it can receive normal input.
    if restored:
        # Restore a minimized target so it can become visible and receive focus.
        win32gui.ShowWindow(handle, win32con.SW_RESTORE)
    # A failed API return is diagnostic; the actual foreground window decides whether focus worked.
    api_error = False
    # Windows may refuse programmatic activation while another application owns foreground input.
    try:
        # Make one request; do not repeatedly force focus or send hidden keyboard shortcuts.
        win32gui.SetForegroundWindow(handle)
    # Only expected native failures are handled here; cancellation and unrelated bugs still propagate.
    except (pywintypes.error, OSError):
        # The API can raise even when the target is becoming foreground, so check actual state below.
        api_error = True
    # A short bounded wait handles delayed activation without another model/API request.
    deadline = time.monotonic() + 1.0
    # No clicking or typing is performed in this observation-only polling loop.
    while True:
        # Read the real foreground after each pause instead of assuming the API request succeeded.
        active = win32gui.GetForegroundWindow()
        # Only the requested window counts as a successful focus result.
        if active == handle:
            # The ordinary worker will now capture a fresh screenshot before another input action.
            return
        # Stop polling after a second or if the target disappeared during activation.
        if time.monotonic() >= deadline or not win32gui.IsWindow(handle):
            # The GUI loop can log this specific failure and ask the planner for another GUI route.
            raise ForegroundNotAcquired(handle, active, restored=restored, api_error=api_error)
        # Give Windows time to process the activation request without busy-waiting.
        time.sleep(0.05)


# Start a fixed registry command with no shell, wait for a matching window, then focus it. Some
# apps reuse an existing process or window.
def launch_app(app_id, timeout=10):
    # Enter this branch only when not isinstance(app_id, str) or app_id not in APP_REGISTRY. The
    # indented statements below are conditional.
    if not isinstance(app_id, str) or app_id not in APP_REGISTRY:
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("Unknown app ID")
    # Store the fixed application registry entry for the requested app ID.
    spec = APP_REGISTRY[app_id]
    # Store the existing window handles before starting the app.
    before = {w["window_handle"] for w in list_windows()}
    # Start the fixed argument list as a process. shell=False avoids interpreting a command string
    # through a shell.
    subprocess.Popen(spec["argv"], shell=False)
    # The direct launcher and GUI launcher share the same target-window verification.
    return _wait_for_app_window(spec, before, timeout)


# Wait until the expected app appears; GUI launching requires a new or foreground match.
def _wait_for_app_window(spec, before, timeout, require_transition=False):
    # Store the latest allowed monotonic time before the next cooperative stop.
    deadline = time.monotonic() + timeout
    # Keep checking while this condition is true; this loop is bounded by the deadline.
    while time.monotonic() < deadline:
        # Store visible windows matching the registered application process names.
        candidates = [w for w in list_windows()
                      # Continue the condition from the previous line before deciding whether this
                      # branch should run.
                      if Path(w["executable"]).name in spec["process_names"]]
        # Do not mistake an old background browser for the result of a failed Start search.
        if require_transition:
            # A new target or a target that gained focus is evidence of a launch transition.
            candidates = [w for w in candidates if w["window_handle"] not in before
                          # Existing apps may reuse their window and simply bring it forward.
                          or w["window_handle"] == win32gui.GetForegroundWindow()]
        # Enter this branch only when candidates. The indented statements below are conditional.
        if candidates:
            # Store the result in foreground. This value is reused by the following steps.
            foreground = win32gui.GetForegroundWindow()
            # Order matching windows by the tuple produced by the lambda: prefer new windows, then
            # the foreground one.
            candidates.sort(key=lambda w: (w["window_handle"] not in before,
                                           # Supply this named argument or setting to the
                                           # multiline call above. The name before = tells the
                                           # receiving function what it controls.
                                           w["window_handle"] == foreground), reverse=True)
            # Store the selected element or matching application window.
            target = candidates[0]["window_handle"]
            # Request and verify foreground focus for this permitted target.
            focus_window(target)
            # Return this value to the caller and finish this function call.
            return target
        # Wait briefly without changing the desktop; this gives the app time to update.
        time.sleep(0.2)
    # Stop this operation by raising an exception; the surrounding caller decides how to report
    # it.
    raise RuntimeError("App launched but no supported window appeared before timeout")


# Recognize the actual Windows Start/search surfaces before sending them any text.
def _launcher_is_foreground():
    # A transient missing/inaccessible foreground window is not proof Start is ready.
    try:
        # Query the active process through the same identity function used for task windows.
        _, executable = process_identity(win32gui.GetForegroundWindow())
    # Let the caller's bounded wait retry while Windows is changing surfaces.
    except (RuntimeError, OSError):
        # No typing is allowed until a later observation positively identifies the launcher.
        return False
    # Windows hosts Start and Search in these system applications.
    launcher_names = {"startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe", "shellexperiencehost.exe"}
    # Require both the expected executable name and the Windows installation path.
    return (Path(executable).name in launcher_names
            # A lookalike executable outside Windows must not receive search text.
            and Path(executable).is_relative_to(str(windows_directory()).lower()))


# A model-selected tool that opens apps using visible Start/search keyboard interaction.
def launch_app_gui(app_id, timeout=10):
    # Only labels we configured locally can be typed into the Windows launcher.
    if app_id not in APP_REGISTRY or app_id not in GUI_SEARCH_NAMES:
        # Stop before opening Start if this app has no supported GUI launch configuration.
        raise ValueError("App has no configured GUI launch route")
    # Import real desktop helpers only when this route is requested.
    from desktopgui import open_start_menu, type_text, press_key
    # Remember existing windows so a background app is not mistaken for a new result.
    before = {w["window_handle"] for w in list_windows()}
    # Open the Start menu with the fixed Ctrl+Escape shortcut.
    open_start_menu()
    # Give the launcher a bounded opportunity to become foreground.
    deadline = time.monotonic() + min(timeout, 4)
    # Observe actual foreground ownership instead of typing after an assumed focus change.
    while not _launcher_is_foreground():
        # Stop if Start never became the active surface.
        if time.monotonic() >= deadline:
            # No search text has been typed on this failure path.
            raise RuntimeError("Start menu did not become foreground")
        # A small pause avoids repeatedly querying Windows without allowing it to update.
        time.sleep(0.1)
    # Type the registry's app label, not a model-generated command or arbitrary query.
    type_text(GUI_SEARCH_NAMES[app_id])
    # Allow Windows search results to populate before selecting the best match.
    time.sleep(1.0)
    # Recheck focus; user input or another app may have interrupted the search.
    if not _launcher_is_foreground():
        # Do not press Enter into an unrelated foreground window.
        raise RuntimeError("Foreground changed during Start-menu search")
    # Activate Windows' best search result for the configured application label.
    press_key("enter")
    # Return only after the expected application window actually appears or gains focus.
    return _wait_for_app_window(APP_REGISTRY[app_id], before, timeout, require_transition=True)


# Keep task-specific window identities and app permissions together.
class WindowSession:
    # This docstring describes the purpose of the module, class, or function. It is readable
    # through Python help() and is kept unchanged.
    """Task-scoped windows/processes. Unrelated foreground changes stop the task."""
    # Store the inputs on this object so later method calls can reuse them.
    def __init__(self, initial_handle, permitted_handles=(), allowed_apps=()):
        # Store the result in self.identities. This value is reused by the following steps.
        self.identities = {}
        # Store the result in self.allowed_apps. This value is reused by the following steps.
        self.allowed_apps = tuple(allowed_apps)
        # Store the result in self.current_handle. This value is reused by the following steps.
        self.current_handle = initial_handle
        # A click or key may open a new top-level window, such as Chrome's Guest browser.
        self.pending_transition = None
        # Repeat the indented body for each item in this sequence. The loop variable holds the
        # current item.
        for handle in (initial_handle, *permitted_handles):
            # Record the target's current process identity in this task session.
            self.register(handle)

    # Remember a window and its process identity as part of this task session.
    def register(self, handle):
        # Set this dictionary field so later code and the caller can see the updated state.
        self.identities[handle] = process_identity(handle)

    # Check whether a window is registered or is an owned dialog in the same accepted process.
    def accepts(self, handle):
        # Attempt the following operations while allowing the nearby except/finally blocks to
        # handle failure and cleanup.
        try:
            # Store the process ID and executable path used to identify a window.
            identity = process_identity(handle)
            # Enter this branch only when handle in self.identities. The indented statements below
            # are conditional.
            if handle in self.identities:
                # Return this value to the caller and finish this function call.
                return self.identities[handle] == identity
            # Follow only owned dialogs in an already accepted process.
            # Store the owner window of a possible dialog.
            owner = win32gui.GetWindow(handle, win32con.GW_OWNER)
            # Enter this branch only when owner in self.identities and self.identities[owner] ==
            # identity. The indented statements below are conditional.
            if owner in self.identities and self.identities[owner] == identity:
                # Set this dictionary field so later code and the caller can see the updated
                # state.
                self.identities[handle] = identity
                # Return this value to the caller and finish this function call.
                return True
            # A newly created same-process foreground window can be a result of our last input.
            pending = self.pending_transition
            # Never accept a pre-existing unrelated window merely because its executable matches.
            if (pending and time.monotonic() <= pending["deadline"]
                    and handle not in pending["existing_handles"]
                    and identity == pending["identity"]
                    and win32gui.GetForegroundWindow() == handle):
                # Register only this one verified transition, then consume the temporary allowance.
                self.identities[handle] = identity
                # Subsequent input/observations now target this accepted window.
                self.current_handle = handle
                # One input cannot authorize an unlimited sequence of new windows.
                self.pending_transition = None
                # The ordinary observation gate can now inspect the new same-process window.
                return True
        # Handle this failure path without continuing with an unsafe or incomplete operation.
        except (RuntimeError, OSError):
            # Intentionally do nothing in this branch; an inaccessible or disallowed window is
            # simply not accepted.
            pass
        # Return this value to the caller and finish this function call.
        return False

    # Return only the currently existing windows whose stored identities still match.
    def windows(self):
        # Return this value to the caller and finish this function call.
        return [w for w in list_windows() if w["window_handle"] in self.identities
                # Supply this named argument or setting to the multiline call above. The name
                # before = tells the receiving function what it controls.
                and self.identities[w["window_handle"]] == (w["pid"], w["executable"])]

    # Handle app/window transitions here; send ordinary input actions to the lower-level
    # dispatcher.
    def dispatch(self, action, fallback):
        # Every new action replaces the previous action's short transition allowance.
        self.pending_transition = None
        # Enter this branch only when action['action'] == 'launch_app'. The indented statements
        # below are conditional.
        if action["action"] in ("launch_app", "launch_app_gui"):
            # Enter this branch only when action['app_id'] not in self.allowed_apps. The indented
            # statements below are conditional.
            if action["app_id"] not in self.allowed_apps:
                # Stop this operation by raising an exception; the surrounding caller decides how
                # to report it.
                raise ValueError("App was not permitted for this task")
            # Store the Windows window identifier, called an HWND.
            # Choose the requested implementation; GUI launch is not a hidden direct launch.
            launcher = launch_app_gui if action["action"] == "launch_app_gui" else launch_app
            # Both routes must return a verified target window before it is registered.
            handle = launcher(action["app_id"])
            # Record the target's current process identity in this task session.
            self.register(handle)
            # Store the result in self.current_handle. This value is reused by the following
            # steps.
            self.current_handle = handle
            # Return this value to the caller and finish this function call.
            return {"status": "executed", "action": action["action"], "window_handle": handle}
        # Enter this branch only when action['action'] == 'focus_window'. The indented statements
        # below are conditional.
        if action["action"] == "focus_window":
            # Store the Windows window identifier, called an HWND.
            handle = action["window_handle"]
            # Enter this branch only when not self.accepts(handle). The indented statements below
            # are conditional.
            if not self.accepts(handle):
                # Stop this operation by raising an exception; the surrounding caller decides how
                # to report it.
                raise ValueError("Window is not permitted or its identity changed")
            # Request and verify foreground focus for this permitted target.
            focus_window(handle)
            # Store the result in self.current_handle. This value is reused by the following
            # steps.
            self.current_handle = handle
            # Return this value to the caller and finish this function call.
            return {"status": "executed", "action": "focus_window", "window_handle": handle}
        # Ordinary mouse/key input may create a new top-level window in the accepted process.
        # Only clicks and navigation keys may initiate this short same-process handoff.
        if action["action"] in ("click", "press_key", "hotkey", "type_into", "navigate_url"):
            # Capture the actual source window; do not rely on an old current_handle value.
            source = win32gui.GetForegroundWindow()
            # Verify it still belongs to this session before remembering its process identity.
            if not self.accepts(source):
                # Refuse input if the foreground changed outside the task scope.
                raise RuntimeError("Input source window is outside task scope")
            # Snapshot existing handles so a later manual switch cannot be mistaken for a new window.
            existing = {window["window_handle"] for window in list_windows()}
            # Include registered handles even if they are hidden or omitted from the visible list.
            existing.update(self.identities)
            # Dispatch the original validated action without changing its arguments.
            outcome = fallback(action)
            # Open a short allowance only after the input call completed successfully.
            self.pending_transition = {"identity": self.identities[source],
                                       "existing_handles": existing,
                                       "deadline": time.monotonic() + 3.0}
            # Keep the tool's usual outcome and let the next observation identify the new window.
            return outcome
        # Typing text and other actions keep the normal dispatch behavior without a handoff allowance.
        return fallback(action)


# Desktop scope follows the actual desktop instead of confining the task to one app window.
class DesktopSession(WindowSession):
    """Explicit whole-primary-screen access; the caller chooses this scope."""

    def __init__(self, initial_handle, permitted_handles=(), allowed_apps=()):
        # No foreground window is a valid starting state here, so do not register handle zero.
        self.identities = {}
        # App IDs still limit the fixed launch tools; they do not limit desktop visibility.
        self.allowed_apps = tuple(allowed_apps)
        # Keep the same interface as WindowSession for the worker and launcher tools.
        self.current_handle = initial_handle
        # Desktop transitions need no one-off same-process allowance.
        self.pending_transition = None
        # Remember the exact process identities shown in the most recent window list.
        self.focus_identities = {}

    def accepts(self, handle):
        # Windows can temporarily have no foreground window while apps open or close.
        if handle == 0:
            # Accept zero only when it genuinely describes the current desktop state.
            return win32gui.GetForegroundWindow() == 0
        # Hidden or destroyed windows cannot be selected as observed foreground targets.
        return bool(win32gui.IsWindow(handle) and win32gui.IsWindowVisible(handle))

    def windows(self):
        # Query accessible visible windows so the model can select one by its title and handle.
        windows = list_windows()
        # Match the list to identities at this instant, not to IDs remembered from an older step.
        self.focus_identities = {w["window_handle"]: (w["pid"], w["executable"]) for w in windows}
        # The active window may differ from the previous step after choosing a browser profile.
        self.current_handle = win32gui.GetForegroundWindow()
        # Use the same observation format as the narrower WindowSession.
        return windows

    def dispatch(self, action, fallback):
        # Fixed app launchers keep their existing allowlist and target verification.
        if action["action"] in ("launch_app", "launch_app_gui"):
            # The inherited launcher records the verified window before returning.
            return super().dispatch(action, fallback)
        # A window ID may be reused after its app closes; recheck its identity before focusing it.
        if action["action"] == "focus_window":
            # This ID has already been checked against the current observation by validate_action.
            handle = action["window_handle"]
            # Do not focus a replacement process that happens to receive an old numeric handle.
            if (handle not in self.focus_identities
                    or process_identity(handle) != self.focus_identities[handle]):
                # The next run must observe a fresh window list instead of using stale evidence.
                raise ValueError("Window disappeared or its identity changed since observation")
            # Restore and focus the selected existing window using the shared Windows helper.
            focus_window(handle)
            # Track the new foreground for diagnostics; it does not become a restriction.
            self.current_handle = handle
            # Return an executed action, not a claim that the overall task succeeded.
            return {"status": "executed", "action": "focus_window", "window_handle": handle}
        # Clicks and typing still go through the same validated, finite set of Python tools.
        return fallback(action)
