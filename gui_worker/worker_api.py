# Provide the framework-facing task contract, desktop lock, window scope, and result. Both direct LangGraph calls and MCP calls should enter through this API.
# Learning edition: comments explain the code; runtime behavior is unchanged.

# This docstring describes the purpose of the module, class, or function. It is readable through
# Python help() and is kept unchanged.
"""Plain Python integration boundary, ready to wrap in a graph node or MCP tool."""
# Import contextmanager from contextlib; this makes those names available without copying their
# implementation.
from contextlib import contextmanager
# Import ctypes so we can call Windows functions with explicit C-compatible types.
import ctypes
# Import dataclass from dataclasses; this makes those names available without copying their
# implementation.
from dataclasses import dataclass
# Import json so we can convert between Python objects and JSON text.
import json
# Import msvcrt so we can lock one byte in a file so separate worker processes coordinate.
import msvcrt
# Import Path from pathlib; this makes those names available without copying their implementation.
from pathlib import Path
# Import threading so we can prevent two Python threads from controlling the desktop at once.
import threading
# Import time so we can measure elapsed time and give the desktop brief pauses.
import time

# Store the result in _thread_lock. This value is reused by the following steps.
_thread_lock = threading.Lock()


# Generate the usual data-container methods for this class. frozen=True prevents normal
# reassignment of its fields.
@dataclass(frozen=True)
# Define the task contract that the orchestrator supplies. Type hints explain the fields;
# validate() enforces runtime rules.
class GuiTask:
    # Describe task_id with a type hint. The type hint alone does not validate incoming data.
    task_id: str
    # Describe goal with a type hint. The type hint alone does not validate incoming data.
    goal: str
    # Describe window_handle with a type hint. The type hint alone does not validate incoming
    # data.
    window_handle: int
    # Describe attempt with a type hint and its default value.
    attempt: int = 1
    # Describe max_steps with a type hint and its default value.
    max_steps: int = 5
    # Describe max_tokens with a type hint and its default value.
    max_tokens: int = 16384
    # Describe call_token_cap with a type hint and its default value.
    call_token_cap: int = 4096
    # Describe timeout_seconds with a type hint and its default value.
    timeout_seconds: int = 120
    # Describe use_omni with a type hint and its default value.
    use_omni: bool = False
    # Describe allowed_apps with a type hint and its default value.
    allowed_apps: tuple[str, ...] = ()
    # Describe allowed_window_handles with a type hint and its default value.
    allowed_window_handles: tuple[int, ...] = ()
    # Window mode masks other apps; desktop mode can observe and move between visible apps.
    scope: str = "window"
    # Irreversible targets a person approved after an awaiting_approval result, e.g. "Button 'Send'".
    approved_targets: tuple[str, ...] = ()

    # Reject invalid task fields before taking screenshots, spending model tokens, or changing the
    # desktop.
    def validate(self):
        # Repeat the indented body for each item in this sequence. The loop variable holds the
        # current item.
        for value in (self.task_id, self.goal):
            # Enter this branch only when not isinstance(value, str) or not value.strip(). The
            # indented statements below are conditional.
            if not isinstance(value, str) or not value.strip():
                # Stop this operation by raising an exception; the surrounding caller decides how
                # to report it.
                raise ValueError("Task ID and goal must be nonempty strings")
        # Check actual integer types and bounds. Python booleans behave like integers, so type(x)
        # is int deliberately excludes True and False.
        # Scope is chosen by the caller, not by a model-generated action.
        if not isinstance(self.scope, str) or self.scope not in ("window", "desktop"):
            # A misspelling must not silently grant wider desktop access.
            raise ValueError("Scope must be window or desktop")
        # Desktop mode may start at zero when Windows has no foreground application.
        minimum_handle = 0 if self.scope == "desktop" else 1
        if type(self.window_handle) is not int or self.window_handle < minimum_handle:
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Window scope needs a positive handle; desktop scope also accepts zero")
        # Repeat the indented body for each item in this sequence. The loop variable holds the
        # current item.
        for value, maximum in ((self.attempt, 3), (self.max_steps, 20),
                               # Also pair the requested timeout with its maximum of 600 seconds
                               # for the same validation loop.
                               (self.timeout_seconds, 600)):
            # Check actual integer types and bounds. Python booleans behave like integers, so
            # type(x) is int deliberately excludes True and False.
            if type(value) is not int or not 1 <= value <= maximum:
                # Stop this operation by raising an exception; the surrounding caller decides how
                # to report it.
                raise ValueError("Attempt, step, or timeout limit is invalid")
        # Check actual integer types and bounds. Python booleans behave like integers, so type(x)
        # is int deliberately excludes True and False.
        if any(type(v) is not int or v <= 0 for v in (self.max_tokens, self.call_token_cap)):
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Token limits must be positive integers")
        # Enter this branch only when self.call_token_cap > self.max_tokens or type(self.use_omni)
        # is not bool. The indented statements below are conditional.
        if self.call_token_cap > self.max_tokens or type(self.use_omni) is not bool:
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Invalid per-call budget or OmniParser flag")
        # Import APP_REGISTRY from app_tools; this makes those names available without copying
        # their implementation.
        from app_tools import APP_REGISTRY
        # Enter this branch only when not isinstance(self.allowed_apps, (list, tuple)) or any((not
        # isinstance(a, str) or a not in APP_REGISTRY for a in self.allowed_apps)). The indented
        # statements below are conditional.
        if (not isinstance(self.allowed_apps, (list, tuple)) or
                # Build or filter this collection with a comprehension; only items meeting its
                # condition are retained.
                any(not isinstance(a, str) or a not in APP_REGISTRY for a in self.allowed_apps)):
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Unknown allowed application ID")
        # Enter this branch only when not isinstance(self.allowed_window_handles, (list, tuple))
        # or any((type(h) is not int or h <= 0 for h in self.allowed_window_handles)). The
        # indented statements below are conditional.
        if (not isinstance(self.allowed_window_handles, (list, tuple)) or
                # Supply this named argument or setting to the multiline call above. The name
                # before = tells the receiving function what it controls.
                any(type(h) is not int or h <= 0 for h in self.allowed_window_handles)):
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Invalid permitted window handles")
        # Approvals are exact target labels from a pending_approval result; keep them few and short.
        if (not isinstance(self.approved_targets, (list, tuple)) or len(self.approved_targets) > 10
                or any(not isinstance(t, str) or not 1 <= len(t) <= 200 for t in self.approved_targets)):
            raise ValueError("Invalid approved targets")


# Make this generator usable in a with block: setup happens before yield, and cleanup happens when
# the block exits.
@contextmanager
# Hold an in-process lock and a Windows file lock for the duration of a GUI attempt. Release both
# in finally.
def desktop_lease():
    # This docstring describes the purpose of the module, class, or function. It is readable
    # through Python help() and is kept unchanged.
    """Serialize callers sharing this installation, including separate processes."""
    # Store whether the local thread lock was obtained without waiting.
    acquired = _thread_lock.acquire(blocking=False)
    # Store the result in (stream, file_locked). This value is reused by the following steps.
    stream, file_locked = None, False
    # Attempt the following operations while allowing the nearby except/finally blocks to handle
    # failure and cleanup.
    try:
        # Enter this branch only when acquired. The indented statements below are conditional.
        if acquired:
            # Store the local filesystem location used by this operation.
            path = Path(__file__).resolve().parent / "outputs" / "desktop.lock"
            # Create the directory if needed; exist_ok avoids an error when it already exists.
            path.parent.mkdir(exist_ok=True)
            # Store the open file object.
            stream = path.open("a+b")
            # Enter this branch only when path.stat().st_size == 0. The indented statements below
            # are conditional.
            if path.stat().st_size == 0:
                # Write serialized data to the already-open file or stream.
                stream.write(b"0")
                # Push buffered bytes out before another process tries to use the lock file.
                stream.flush()
            # Position the file cursor at byte zero; every worker locks the same byte.
            stream.seek(0)
            # Attempt the following operations while allowing the nearby except/finally blocks to
            # handle failure and cleanup.
            try:
                # Lock or unlock one byte of the shared file using the stated Windows locking
                # constant.
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                # Store whether this process obtained the Windows byte lock.
                file_locked = True
            # Handle this failure path without continuing with an unsafe or incomplete operation.
            except OSError:
                # Intentionally do nothing in this branch; an inaccessible or disallowed window is
                # simply not accepted.
                pass
        # Hand the lease status to the with block, pause here, then resume into cleanup when that
        # block exits.
        yield acquired and file_locked
    # Always perform this cleanup, whether the preceding work succeeded, failed, or was
    # interrupted.
    finally:
        # Enter this branch only when stream is not None. The indented statements below are
        # conditional.
        if stream is not None:
            # Enter this branch only when file_locked. The indented statements below are
            # conditional.
            if file_locked:
                # Position the file cursor at byte zero; every worker locks the same byte.
                stream.seek(0)
                # Lock or unlock one byte of the shared file using the stated Windows locking
                # constant.
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            # Release this operating-system handle or file resource.
            stream.close()
        # Enter this branch only when acquired. The indented statements below are conditional.
        if acquired:
            # Release the in-process lock so the next caller can use the desktop.
            _thread_lock.release()


# Run one framework task with scope, timing, and evidence handling around the inner GUI loop.
def execute_gui_task(task, planner, *, execute=False, cancel_event=None, done_checker=None):
    # This docstring describes the purpose of the module, class, or function. It is readable
    # through Python help() and is kept unchanged.
    """Return JSON-serializable evidence. Never claim verified success.

    The framework owns planning, retries/VM resets, and checking. This method
    runs one GUI attempt. `execute=False` still calls the model but does not act.
    Cancellation/deadline checks are cooperative between blocking operations.
    """
    # Enforce the task contract before observations or model calls start.
    task.validate()  # No capture/model calls for invalid requests.
    # Require a real boolean execution flag; a truthy string such as "false" would otherwise be
    # dangerous.
    if type(execute) is not bool:
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("execute must be boolean")
    # Store task identity and unverified status fields shared by results.
    base = {"task_id": task.task_id, "attempt": task.attempt,
            # Include the verified field: whether an independent checker has confirmed success;
            # this worker leaves it false.
            "verified": False, "check_status": "unknown"}
    # Enter a managed resource block so its file, client, or desktop lease is released
    # automatically on exit.
    with desktop_lease() as acquired:
        # Enter this branch only when not acquired. The indented statements below are conditional.
        if not acquired:
            # Return this value to the caller and finish this function call.
            return {**base, "status": "busy", "actions_executed": 0,
                    # Include the tokens_used field: known token usage from the backend, not a
                    # model-generated estimate.
                    "tokens_used": 0, "usage_complete": True}
        # Store the result in user32. This value is reused by the following steps.
        user32 = ctypes.windll.user32
        # Describe native argument/return types for ctypes so Windows receives correctly sized
        # values, including 64-bit handles.
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        # Describe native argument/return types for ctypes so Windows receives correctly sized
        # values, including 64-bit handles.
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        # Store the prior Windows DPI-awareness context to restore later.
        previous_dpi = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        # Enter this branch only when not previous_dpi. The indented statements below are
        # conditional.
        if not previous_dpi:
            # Return this value to the caller and finish this function call.
            return {**base, "status": "error", "reason": "DPI context setup failed",
                    # Include the actions_executed field in this structured record; the receiver
                    # reads it by this key.
                    "actions_executed": 0, "tokens_used": 0, "usage_complete": True}
        # Store the monotonic start time for the whole attempt.
        started = time.monotonic()
        # Store the latest allowed monotonic time before the next cooperative stop.
        deadline = started + task.timeout_seconds
        # Store the reason a cooperative stop was requested.
        stop_reason = None
        # Import pythoncom so we can initialize and release Windows COM support on the current
        # thread.
        import pythoncom
        # Initialize COM for this thread, which is needed when UI Automation runs inside an MCP
        # worker thread.
        pythoncom.CoInitialize()
        # Attempt the following operations while allowing the nearby except/finally blocks to
        # handle failure and cleanup.
        try:
            # Import execute_action from actions; this makes those names available without copying
            # their implementation.
            from actions import execute_action
            # Import capture_observation from observations; this makes those names available
            # without copying their implementation.
            from observations import capture_observation, ObservationChanged
            # Import run_gui_task from gui_worker; this makes those names available without
            # copying their implementation.
            from gui_worker import run_gui_task
            # Import win32gui so we can inspect windows and manage which window is in front.
            import win32gui
            # Import WindowSession from app_tools; this makes those names available without
            # copying their implementation.
            from app_tools import WindowSession, DesktopSession
            # Store the task's window tracker, initialized only when needed.
            session = None

            # Create the task-scoped window tracker only when it is first needed, then reuse it.
            def window_session():
                # Modify the variable from the enclosing function rather than creating a separate
                # local variable.
                nonlocal session
                # Enter this branch only when session is None. The indented statements below are
                # conditional.
                if session is None:
                    # Store the task's window tracker, initialized only when needed.
                    # Desktop tasks deliberately follow visible windows, even across app processes.
                    session_type = DesktopSession if task.scope == "desktop" else WindowSession
                    # Both session types expose the same interface to the framework adapter.
                    session = session_type(task.window_handle, task.allowed_window_handles, task.allowed_apps)
                # Return this value to the caller and finish this function call.
                return session

            # Check cancellation, deadline, and foreground scope before the next operation.
            def gate():
                # Modify the variable from the enclosing function rather than creating a separate
                # local variable.
                nonlocal stop_reason
                # Enter this branch only when cancel_event is not None and cancel_event.is_set().
                # The indented statements below are conditional.
                if cancel_event is not None and cancel_event.is_set():
                    # Store the reason a cooperative stop was requested.
                    stop_reason = "cancelled"
                    # Stop this operation by raising an exception; the surrounding caller decides
                    # how to report it.
                    raise KeyboardInterrupt()
                # Enter this branch only when time.monotonic() >= deadline. The indented
                # statements below are conditional.
                if time.monotonic() >= deadline:
                    # Store the reason a cooperative stop was requested.
                    stop_reason = "time_limit"
                    # Stop this operation by raising an exception; the surrounding caller decides
                    # how to report it.
                    raise KeyboardInterrupt()
                # Store the foreground window at this moment.
                active = win32gui.GetForegroundWindow()
                # Enter this branch only when not window_session().accepts(active). The indented
                # statements below are conditional.
                # Desktop scope has no fixed foreground restriction; capture checks consistency.
                if task.scope == "window" and not window_session().accepts(active):
                    # Stop this operation by raising an exception; the surrounding caller decides
                    # how to report it.
                    raise RuntimeError("Foreground window is outside task scope")

            # Capture through the task gate and enrich the observation with permitted windows,
            # apps, keys, and shortcuts.
            def observe(**kwargs):
                # Profile selection may change focus while UIA is still reading the old window.
                for attempt in range(3):
                    # Check cancellation and the deadline before each read-only capture attempt.
                    gate()
                    # Retry only an inconsistent observation, never a failed model request.
                    try:
                        # Preserve the original call signature for window-only callers.
                        observation = (capture_observation(scope="desktop", **kwargs)
                                       if task.scope == "desktop" else capture_observation(**kwargs))
                        # A profile window can disappear just after the capture finishes, too.
                        gate()
                        # Retry this read if desktop focus already moved to another window.
                        if task.scope == "desktop" and (
                                win32gui.GetForegroundWindow() != observation["window_handle"]
                                or not window_session().accepts(observation["window_handle"])):
                            # No model call or action has occurred for this inconsistent capture.
                            raise ObservationChanged("Foreground changed immediately after capture")
                        # A stable screenshot/tree pair is ready for the remaining checks.
                        break
                    # App transitions can make the first capture inconsistent.
                    except ObservationChanged:
                        # Three failed reads end the task instead of spinning indefinitely.
                        if attempt == 2:
                            # Preserve the capture diagnostic in the task result.
                            raise
                        # Give Windows a brief opportunity to finish the handoff.
                        time.sleep(0.15)
                # Recheck stop conditions and foreground scope before continuing.
                gate()
                # Enter this branch only when not
                # window_session().accepts(observation['window_handle']). The indented statements
                # below are conditional.
                if not window_session().accepts(observation["window_handle"]):
                    # Stop this operation by raising an exception; the surrounding caller decides
                    # how to report it.
                    raise RuntimeError("Observation is outside task scope")
                # Set this dictionary field so later code and the caller can see the updated
                # state.
                observation["windows"] = window_session().windows()
                # Set this dictionary field so later code and the caller can see the updated
                # state.
                observation["allowed_apps"] = list(task.allowed_apps)
                # This host-supplied field also controls coordinate-click validation.
                observation["scope"] = task.scope
                # Import SHORTCUTS, KEYS from input_policy; this makes those names available
                # without copying their implementation.
                from input_policy import SHORTCUTS, KEYS
                # Set this dictionary field so later code and the caller can see the updated
                # state.
                observation["allowed_shortcuts"] = [list(keys) for keys in sorted(SHORTCUTS)]
                # Set this dictionary field so later code and the caller can see the updated
                # state.
                observation["allowed_keys"] = sorted(KEYS)
                # Return this value to the caller and finish this function call.
                return observation

            # Check the gate before calling the injected planner; the inner loop accounts for
            # returned token usage.
            def decide(*args):
                # Recheck stop conditions and foreground scope before continuing.
                gate()
                # Return this value to the caller and finish this function call.
                return planner(*args)  # Worker accounts for usage before dispatch gate.

            # Handle app/window transitions here; send ordinary input actions to the lower-level
            # dispatcher.
            def dispatch(action):
                # Recheck stop conditions and foreground scope before continuing.
                gate()
                # Return this value to the caller and finish this function call.
                return window_session().dispatch(action, execute_action)

            # Store the structured response that will be returned to the caller.
            result = run_gui_task(task.goal, decide, execute=execute,
                # Supply this named argument or setting to the multiline call above. The name
                # before = tells the receiving function what it controls.
                max_steps=task.max_steps, max_tokens=task.max_tokens,
                # Supply this named argument or setting to the multiline call above. The name
                # before = tells the receiving function what it controls.
                call_token_cap=task.call_token_cap, use_omni=task.use_omni,
                # Supply this named argument or setting to the multiline call above. The name
                # before = tells the receiving function what it controls.
                observe=observe, dispatch=dispatch,
                # Supply this named argument or setting to the multiline call above. The name
                # before = tells the receiving function what it controls.
                authorize_window=lambda h: window_session().accepts(h),
                # A person's approvals for irreversible clicks, from an earlier paused run.
                approved_targets=tuple(task.approved_targets),
                # A different model checks the screen against the goal before done is accepted.
                verify_done=done_checker)
            # Enter this branch only when stop_reason. The indented statements below are
            # conditional.
            if stop_reason:
                # Set this dictionary field so later code and the caller can see the updated
                # state.
                result["status"] = stop_reason
            # Enter this branch only when time.monotonic() >= deadline. The indented statements
            # below are conditional.
            elif time.monotonic() >= deadline:
                # Set this dictionary field so later code and the caller can see the updated
                # state.
                result["status"] = "time_limit"
            # Enter this branch only when cancel_event is not None and cancel_event.is_set(). The
            # indented statements below are conditional.
            elif cancel_event is not None and cancel_event.is_set():
                # Set this dictionary field so later code and the caller can see the updated
                # state.
                result["status"] = "cancelled"
            # Merge these fields into the result dictionary without discarding its other fields.
            result.update(base)
            # Make the scope visible to the orchestrator and in the printed result.
            result["scope"] = task.scope
            # Set this dictionary field so later code and the caller can see the updated state.
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            # Store the latest screenshot and structured screen information.
            observation = result.pop("last_observation", None)
            # Set this dictionary field so later code and the caller can see the updated state.
            result["evidence"] = {
                # Include the log_path field in this structured record; the receiver reads it by
                # this key.
                "log_path": result.get("log_path"),
                # Include the last_screenshot field in this structured record; the receiver reads
                # it by this key.
                "last_screenshot": (observation or {}).get("screenshot_path"),
                # Include the last_observation_id field in this structured record; the receiver
                # reads it by this key.
                "last_observation_id": (observation or {}).get("observation_id"),
            # Finish the multiline call or collection started above; the commas keep the separate
            # arguments or items distinct.
            }
            # Enter this branch only when result.get('log_path'). The indented statements below
            # are conditional.
            if result.get("log_path"):
                # Store the final summary JSON file stored beside the event log.
                result_path = Path(result["log_path"]).with_name("result.json")
                # Save the text using the stated encoding so it can be read consistently later.
                result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            # Return this value to the caller and finish this function call.
            return result
        # Always perform this cleanup, whether the preceding work succeeded, failed, or was
        # interrupted.
        finally:
            # Release this thread's COM initialization when the task exits.
            pythoncom.CoUninitialize()
            # Set or restore the current thread's DPI context so UI coordinates and screenshot
            # pixels are comparable.
            user32.SetThreadDpiAwarenessContext(previous_dpi)
