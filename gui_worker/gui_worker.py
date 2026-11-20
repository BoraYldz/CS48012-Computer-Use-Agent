# Run one observe-decide-validate-act attempt. Return evidence and an honest status; deciding whether the complete user task succeeded belongs to the caller.
# Learning edition: comments explain action validation and the observe/decide/act loop.

# This docstring describes the purpose of the module, class, or function. It is readable through
# Python help() and is kept unchanged.
"""One bounded GUI attempt. The caller owns verification and overall retries."""
# Import json so we can convert between Python objects and JSON text.
import json
# Import math so we can reject non-finite numbers such as NaN in coordinates.
import math
# Import Path from pathlib; this makes those names available without copying their implementation.
from pathlib import Path
# Import time so we can measure elapsed time and give the desktop brief pauses.
import time
# Import uuid so we can create unique observation or run identifiers.
import uuid
# Extract code locations on failure without printing exception text or local variables.
import traceback

# Import pyautogui so we can generate desktop input and capture the screen.
import pyautogui
# Import win32gui so we can inspect windows and manage which window is in front.
import win32gui

# Import execute_action from actions; this makes those names available without copying their
# implementation.
from actions import execute_action
# Import capture_observation from observations; this makes those names available without copying
# their implementation.
from observations import capture_observation
# Import KEYS, validate_shortcut, validate_scroll from input_policy; this makes those names
# available without copying their implementation.
from input_policy import KEYS, validate_shortcut, validate_scroll, validate_url
# Field names have one source of truth shared with the model schema.
from action_contract import ACTION_FIELDS
# Preserve focus-specific details alongside the shared desktop-tool recovery path.
from app_tools import ForegroundNotAcquired, is_browser_window
# Semantic history, visible-effect checks and the repeated-no-effect guard.
from progress import (LoopGuard, action_signature, describe_effect, element_at, element_label,
                      focused_element, summarize_record)
# Word patterns for the irreversible-action approval gate.
import re
# Wait for the screen to stop changing instead of a fixed pause after each action.
from desktopgui import wait_until_stable
# Native Windows failures are recoverable tool outcomes, not model or authentication errors.
import pywintypes


# A disabled target is a rejected proposal, not a failure of the desktop input tool.
class UnavailableTarget(ValueError):
    # Keep a specific type for disabled targets; all rejected actions can now be corrected.
    pass


# Structural containers: clicking their centre hits whatever happens to be there (often nothing).
CONTAINER_TYPES = {"Document", "Pane", "Window", "List", "Table", "Tree", "DataGrid",
                   "ToolBar", "TitleBar", "MenuBar", "StatusBar", "Tab"}
# Cards such as search results: the centre is usually snippet text or blank space, not the link.
CARD_TYPES = {"ListItem", "DataItem", "Group", "Custom"}
# Controls that a click inside a container or card should go to instead.
INNER_CONTROL_TYPES = {"Hyperlink", "Button", "Edit", "ComboBox", "CheckBox", "RadioButton",
                       "MenuItem", "TabItem", "ListItem"}


def _centre(element):
    left, top, right, bottom = element["bbox"]
    return int((left + right) / 2), int((top + bottom) / 2)


# True when inner lies within outer (2 px tolerance) and is meaningfully smaller.
def _inside(inner, outer):
    l, t, r, b = inner["bbox"]
    L, T, R, B = outer["bbox"]
    return (l >= L - 2 and t >= T - 2 and r <= R + 2 and b <= B + 2
            and (r - l) * (b - t) < 0.9 * (R - L) * (B - T))


# Decide where a click on this element should land: ((x, y), element actually clicked).
# Run log evidence: 11 clicks on the centre of a Yandex result card landed in blank space beside
# the title link, and nothing happened.
def click_point(target, observation):
    if target.get("source") != "uia":
        return _centre(target), target
    inner = [e for e in observation["elements"]
             if e is not target and e.get("source") == "uia" and e.get("enabled") is True
             and e.get("type") in INNER_CONTROL_TYPES and isinstance(e.get("bbox"), list)
             and len(e["bbox"]) == 4 and _inside(e, target)]
    inner.sort(key=lambda e: (e["bbox"][1], e["bbox"][0]))
    if target.get("type") in CONTAINER_TYPES and inner:
        examples = ", ".join(f"{e['id']} {element_label(e)}" for e in inner[:5])
        raise ValueError(f"{target['type']} is a container; its centre is not a specific control. "
                         f"Click a control inside it, e.g. {examples}"[:300])
    if target.get("type") in CARD_TYPES:
        links = [e for e in inner if e["type"] == "Hyperlink" and str(e.get("name") or "").strip()]
        if links:
            return _centre(links[0]), links[0]
    return _centre(target), target


# Blast-radius gate (Lecture 4 "Guardrails & Authorization", tier 2): these labels mean the click
# probably cannot be undone, so it waits for a person. English and Turkish UI words.
IRREVERSIBLE_WORDS = re.compile(
    r"\b(send|submit|delete|remove|post|publish|confirm|transfer|uninstall|erase|empty trash|"
    r"permanently|place order|buy|purchase|pay|checkout|check out|sign out|log out|"
    r"gönder|sil|kaldır|yayınla|onayla|satın al|öde)\b", re.IGNORECASE)
# Pressing Enter in one of these fields usually sends something to other people.
MESSAGE_FIELD_WORDS = re.compile(r"\b(message|comment|reply|chat|tweet|mesaj|yorum|yanıt)\b",
                                 re.IGNORECASE)
# Real action controls. Other types (links, list items) only count when the whole name is a short
# command such as "Delete", so a search result titled "How to delete a file" does not pause the run.
ACTION_CONTROL_TYPES = {"Button", "MenuItem", "SplitButton"}
# After this many reviewer rejections of "done", the run ends as done_rejected.
MAX_DONE_REJECTIONS = 2


def _label_parts(label):
    kind, _, rest = (label or "").partition(" ")
    return kind, rest.strip().strip("'")


# Why this action needs a person's approval first, or None. Labels come from describe_target.
def irreversible_reason(requested, observation, record):
    name = requested.get("action")
    if name in ("click_element", "click_at"):
        label = record.get("clicked") or record.get("target") or ""
        kind, text = _label_parts(label)
        match = IRREVERSIBLE_WORDS.search(text)
        if match and (kind in ACTION_CONTROL_TYPES or len(text.split()) <= 3):
            return f"clicking {label} looks irreversible ('{match.group(0)}')"
    if name == "type_into" and requested.get("submit") is True:
        field = record.get("target")
    elif name == "press_key" and requested.get("key") == "enter":
        field = element_label(focused_element(observation))
    else:
        field = None
    if field and MESSAGE_FIELD_WORDS.search(_label_parts(field)[1]):
        return f"pressing Enter in {field} may send a message"
    return None


# Shared text rule for type_text and type_into. Typing sends Unicode characters, so Turkish
# letters and symbols such as @ work on any keyboard layout; control characters (newline, tab)
# are still refused because they act as keys, not text.
def _check_text(text):
    if (not isinstance(text, str) or not 1 <= len(text) <= 500
            or any(not c.isprintable() for c in text)):
        raise ValueError("Typing supports 1-500 printable characters (no newlines or tabs)")


# Name the target in words for history; IDs are renumbered on the next observation.
def describe_target(requested, resolved, observation):
    name = requested.get("action")
    if name in ("click_element", "scroll", "type_into"):
        target = next(e for e in observation["elements"] if e["id"] == requested["element_id"])
        info = {"target": element_label(target)}
        if name == "click_element":
            clicked = click_point(target, observation)[1]
            if clicked is not target:
                info["clicked"] = element_label(clicked)
        return info
    if name == "click_at":
        hit = element_at(observation, resolved["x"], resolved["y"])
        return {"target": element_label(hit) or f"point ({resolved['x']}, {resolved['y']})"}
    if name == "focus_window":
        titles = [w.get("title") for w in observation.get("windows", [])
                  if w.get("window_handle") == requested["window_handle"]]
        return {"target": f"window '{(titles or ['?'])[0]}'"}
    if name in ("launch_app", "launch_app_gui"):
        return {"target": f"app {requested['app_id']}"}
    return {}


# Validate the model-facing action shape and current target. Convert an element ID into
# coordinates only after checking its rectangle.
def validate_action(action, observation):
    # Enter this branch only when not isinstance(action, dict). The indented statements below are
    # conditional.
    if not isinstance(action, dict):
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("Action must be an object")
    # Store the action name selected from the dictionary.
    name = action.get("action")
    # Use the same argument names that generated the model's structured-output schema.
    fields = ACTION_FIELDS
    # Enter this branch only when not isinstance(name, str) or name not in fields or set(action)
    # != fields[name]. The indented statements below are conditional.
    if not isinstance(name, str) or name not in fields or set(action) != fields[name]:
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("Unsupported action or unexpected/missing fields")
    # Coordinate clicks are an explicit desktop-mode capability, never a window-mode escape.
    if name == "click_at":
        # The host sets scope in the observation; the model cannot change it in its action.
        if observation.get("scope", "window") != "desktop":
            raise ValueError("click_at requires desktop scope")
        # Reject booleans, fractions, negative positions, and points beyond the captured screen.
        x, y = action["x"], action["y"]
        # Screenshot coordinates and PyAutoGUI coordinates both use the unchanged primary screen.
        width, height = observation["screen_size"]
        # Python considers True an integer, so exact type checks matter here.
        if type(x) is not int or type(y) is not int or not (0 <= x < width and 0 <= y < height):
            raise ValueError("Click coordinates must be integer pixels inside the primary screen")
        # Reuse the existing mouse tool; never execute model-generated Python.
        return {"action": "click", "x": x, "y": y}
    # Enter this branch only when name == 'hotkey'. The indented statements below are conditional.
    # Waiting is a normal step followed by a fresh observation, never an unbounded sleep.
    if name == "wait":
        # Reject booleans and fractions as well as pauses outside the small allowed range.
        if type(action["seconds"]) is not int or not 1 <= action["seconds"] <= 3:
            # Stop before the dispatcher receives an invalid duration.
            raise ValueError("Wait must be an integer from 1 to 3 seconds")
    if name == "hotkey":
        # Check the shortcut list before a key-down event can happen.
        validate_shortcut(action["keys"])
    # Enter this branch only when name == 'launch_app'. The indented statements below are
    # conditional.
    if name in ("launch_app", "launch_app_gui"):
        # Enter this branch only when not isinstance(action['app_id'], str) or action['app_id']
        # not in observation.get('allowed_apps', []). The indented statements below are
        # conditional.
        if not isinstance(action["app_id"], str) or action["app_id"] not in observation.get("allowed_apps", []):
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("App is not available for this task")
    # Enter this branch only when name == 'focus_window'. The indented statements below are
    # conditional.
    if name == "focus_window":
        # Store the Windows window identifier, called an HWND.
        handle = action["window_handle"]
        # Check actual integer types and bounds. Python booleans behave like integers, so type(x)
        # is int deliberately excludes True and False.
        if type(handle) is not int or handle not in [w["window_handle"] for w in observation.get("windows", [])]:
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Window is absent from current permitted-window list")
    # Ctrl+L is "address bar" only in a browser; elsewhere it could mean something else entirely.
    if name == "navigate_url":
        validate_url(action["url"])
        if not is_browser_window(observation.get("window_handle")):
            raise ValueError("navigate_url needs a browser window in front; focus or launch one first")
        return action.copy()
    # Composite typing: check text and flags here; the target is checked with click targets below.
    if name == "type_into":
        _check_text(action["text"])
        if type(action["clear"]) is not bool or type(action["submit"]) is not bool:
            raise ValueError("clear and submit must be true or false")
    # Enter this branch only when name in ('click_element', 'scroll'). The indented statements
    # below are conditional.
    if name in ("click_element", "scroll", "type_into"):
        # Enter this branch only when name == 'scroll'. The indented statements below are
        # conditional.
        if name == "scroll":
            # Check the direction and bounded wheel count before scrolling.
            validate_scroll(action["direction"], action["amount"])
        # Enter this branch only when not isinstance(action['element_id'], str). The indented
        # statements below are conditional.
        if not isinstance(action["element_id"], str):
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("element_id must be a string")
        # Store current elements whose ID matches the model's requested target.
        matches = [e for e in observation["elements"]
                   # Continue the condition from the previous line before deciding whether this
                   # branch should run.
                   if e["id"] == action["element_id"]]
        # Enter this branch only when len(matches) != 1. The indented statements below are
        # conditional.
        if len(matches) != 1:
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Target ID absent or ambiguous in this observation")
        # Store the selected element or matching application window.
        target = matches[0]
        # Enter this branch only when target['source'] == 'uia' and target.get('enabled') is not
        # True. The indented statements below are conditional.
        if target["source"] == "uia" and target.get("enabled") is not True:
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise UnavailableTarget("UI target is disabled")
        # Enter this branch only when name == 'click_element' and target['source'] == 'omniparser'
        # and (target.get('interactable') is not True). The indented statements below are
        # conditional.
        if name in ("click_element", "type_into") and target["source"] == "omniparser" and target.get("interactable") is not True:
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Vision target is not predicted interactable")
        # type_into clicks and may select-all; restrict it to text inputs so it cannot wipe a document.
        if name == "type_into" and target["source"] == "uia" and target.get("type") not in ("Edit", "ComboBox"):
            raise ValueError("type_into needs an Edit or ComboBox; for other targets click_element, then type_text")
        # Store the target rectangle, in left/top/right/bottom order.
        box = target["bbox"]
        # Check actual integer types and bounds. Python booleans behave like integers, so type(x)
        # is int deliberately excludes True and False.
        if (len(box) != 4 or any(type(v) not in (int, float)
                # Build or filter this collection with a comprehension; only items meeting its
                # condition are retained.
                or not math.isfinite(v) for v in box)):
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Invalid target rectangle")
        # Store the result in (left, top, right, bottom). This value is reused by the following
        # steps.
        left, top, right, bottom = box
        # Enter this branch only when left >= right or top >= bottom. The indented statements
        # below are conditional.
        if left >= right or top >= bottom:
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Empty target rectangle")
        # Scrolling targets the element's centre; clicks may move to a link inside a card, and
        # clicks on the middle of a container that has controls are refused.
        x, y = _centre(target) if name == "scroll" else click_point(target, observation)[0]
        # Store the result in (width, height). This value is reused by the following steps.
        width, height = observation["screen_size"]
        # Store the result in (wl, wt, wr, wb). This value is reused by the following steps.
        # Desktop tasks can target the full image; window tasks retain their original rectangle.
        wl, wt, wr, wb = ([0, 0, width, height] if observation.get("scope") == "desktop"
                           else observation["window_bounds"])
        # Enter this branch only when not (0 <= x < width and 0 <= y < height and (wl <= x < wr)
        # and (wt <= y < wb)). The indented statements below are conditional.
        if not (0 <= x < width and 0 <= y < height and wl <= x < wr and wt <= y < wb):
            # Stop this operation by raising an exception; the surrounding caller decides how to
            # report it.
            raise ValueError("Target is outside primary screen or selected window")
        # Enter this branch only when name == 'scroll'. The indented statements below are
        # conditional.
        if name == "scroll":
            # Return this value to the caller and finish this function call.
            return {"action": "scroll", "x": x, "y": y,
                    # Include the direction field in this structured record; the receiver reads it
                    # by this key.
                    "direction": action["direction"], "amount": action["amount"]}
        if name == "type_into":
            return {"action": "type_into", "x": x, "y": y, "text": action["text"],
                    "clear": action["clear"], "submit": action["submit"]}
        # Return this value to the caller and finish this function call.
        return {"action": "click", "x": x, "y": y}
    # Enter this branch only when name == 'type_text'. The indented statements below are
    # conditional.
    if name == "type_text":
        _check_text(action["text"])
    # Enter this branch only when name == 'press_key' and (not isinstance(action['key'], str) or
    # action['key'] not in KEYS). The indented statements below are conditional.
    if name == "press_key" and (not isinstance(action["key"], str) or action["key"] not in KEYS):
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("Key not allowed in this prototype")
    # Enter this branch only when name == 'blocked' and (not isinstance(action['reason'], str) or
    # not 1 <= len(action['reason']) <= 500). The indented statements below are conditional.
    if name == "blocked" and (not isinstance(action["reason"], str)
                               # Supply this named argument or setting to the multiline call
                               # above. The name before = tells the receiving function what it
                               # controls.
                               or not 1 <= len(action["reason"]) <= 500):
        # Stop this operation by raising an exception; the surrounding caller decides how to
        # report it.
        raise ValueError("Blocked requires a short reason")
    # Return this value to the caller and finish this function call.
    return action.copy()


# Run a bounded sequence of decisions and observations. A decision counts toward max_steps even
# when it says done rather than clicking.
def run_gui_task(goal, planner, *, execute=False, max_steps=3,
                 max_tokens=4096, call_token_cap=1024, use_omni=False,
                 observe=capture_observation, dispatch=execute_action, authorize_window=None,
                 settle=wait_until_stable, approved_targets=(), verify_done=None):
    """Observe -> decide -> validate -> act -> observe, bounded by the caller.

    Failed/rejected actions become feedback for the next decision. They are never
    replayed automatically. The framework wrapper owns cancellation, deadlines and
    desktop scope. The caller still owns independent task-success verification.
    """
    # Reject invalid requests before taking a screenshot or spending model tokens.
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("Goal must be nonempty text")
    if type(max_steps) is not int or not 1 <= max_steps <= 20:
        raise ValueError("max_steps must be 1-20")
    if any(type(value) is not int or value <= 0 for value in (max_tokens, call_token_cap)):
        raise ValueError("Token limits must be positive integers")
    # Every attempt gets a separate log; the caller can inspect successes and failures alike.
    folder = Path(__file__).resolve().parent / "outputs" / f"run_{uuid.uuid4().hex}"
    folder.mkdir(parents=True)
    log_path = folder / "events.jsonl"
    history, failures, recoveries, tokens = [], [], [], 0
    # Remembers actions that ran on a screen without changing it, so they are not repeated forever.
    guard = LoopGuard()
    # Irreversible targets a person already approved for this run; each approval is used once.
    approvals = list(approved_targets or ())
    # Stored lessons the model said it followed anywhere in this run.
    lessons_applied = set()
    result = {"status": "step_limit", "verified": False, "actions_executed": 0,
              "actions_rejected": 0, "log_path": str(log_path), "usage_complete": True,
              "run_id": folder.name[len("run_"):]}

    # JSONL stores one event per line so a teammate can reconstruct what happened.
    def log(event):
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    # Recheck focus just before input; a decision based on an old screen must be reconsidered.
    def still_current(observation, began):
        if time.monotonic() - began > 30 or win32gui.GetForegroundWindow() != observation["window_handle"]:
            return False
        try:
            # Handle zero is valid in desktop mode when no application is in front.
            if observation["window_handle"] and list(win32gui.GetWindowRect(observation["window_handle"])) != observation["window_bounds"]:
                return False
        except (OSError, pywintypes.error):
            # A window can close between reading its handle and its rectangle.
            return False
        return list(pyautogui.size()) == observation["screen_size"]

    log({"event": "start", "goal": goal, "execute": execute,
         "max_steps": max_steps, "max_tokens": max_tokens, "call_token_cap": call_token_cap})
    observation, initial_handle, stage = None, None, "initial_observation"
    try:
        observation = observe(use_omni=use_omni)
        initial_handle = observation["window_handle"]
        for step in range(max_steps):
            # Both a previous success and a previous failure arrive here with a fresh observation.
            log({"event": "observation", "step": step, "data": observation})
            remaining = max_tokens - tokens
            if remaining <= 0:
                result.update(status="token_limit", reason="Total token budget exhausted")
                break
            reservation = min(call_token_cap, remaining)
            began, stage = time.monotonic(), "model_decision"
            decision = planner(goal, observation, history.copy(), reservation)
            # Usage comes from the provider adapter, never from text written by the model.
            used = decision.get("tokens_used")
            if type(used) is not int or used < 0:
                raise ValueError("Planner must report nonnegative integer tokens_used")
            tokens += used
            log({"event": "decision", "step": step, "token_reservation": reservation, "data": decision})
            if used > reservation or tokens > max_tokens:
                result.update(status="token_limit", reason="Reported usage exceeded the token allowance; action was not executed")
                break
            if decision.get("observation_id") != observation["observation_id"]:
                raise ValueError("Decision refers to a different observation")

            requested = decision.get("action")
            action, feedback, signature = None, None, None
            # One semantic record per step; the model's explanation is kept as its stated intent.
            record = {"step": step, "action": requested, "intent": decision.get("explanation")}
            # The model's working notes travel with the step and are shown back to it next time.
            if decision.get("scratchpad"):
                record["scratchpad"] = decision["scratchpad"]
            # Stored lessons the model says shaped this step; the run's outcome updates their counters.
            if decision.get("applied_lessons"):
                record["applied_lessons"] = list(decision["applied_lessons"])
                # Includes the final done/blocked decision, which never enters the history.
                lessons_applied.update(decision["applied_lessons"])
            metadata = decision.get("response_metadata") or {}
            budget = metadata.get("token_budget", {})
            # A request that still cannot fit after compaction cannot be made within this budget.
            if budget.get("generation_skipped") is True:
                result.update(status="token_limit", reason="Even the compact observation cannot fit the available token allowance",
                              next_call_budget=budget)
                break
            # Malformed replies are observations about a failed decision, not executable actions.
            if decision.get("parse_error") or metadata.get("selection_error"):
                feedback = {"status": "rejected", "error_code": "invalid_model_reply",
                            "reason": "Return exactly one valid supported action for the fresh observation",
                            "partial_effects_possible": False}
            else:
                stage = "action_validation"
                try:
                    action = validate_action(requested, observation)
                except ValueError as error:
                    # No tool has run. Explain the invalid target/arguments and let the model correct them.
                    feedback = {"status": "rejected", "error_code": "invalid_action",
                                "reason": str(error)[:300], "partial_effects_possible": False}

            if action is not None:
                record["resolved"] = action
                record.update(describe_target(requested, action, observation))
                signature = action_signature(record, observation.get("window_title"))
                # Refuse a click/key that already did nothing twice on this same screen.
                repeated = guard.check(signature)
                if repeated:
                    feedback = {"status": "rejected", "error_code": "repeated_no_effect",
                                "reason": repeated, "partial_effects_possible": False}

            # Irreversible-looking clicks pause the run until a person approves that target.
            if action is not None and feedback is None:
                risk = irreversible_reason(requested, observation, record)
                if risk:
                    label = record.get("clicked") or record.get("target") or requested.get("action")
                    record["approval"] = {"target": label, "reason": risk}
                    if label in approvals:
                        approvals.remove(label)
                        record["approval"]["status"] = "approved"
                    elif execute:
                        # Nothing ran. The caller shows this to a person and, on approval, starts a
                        # new run with approved_targets=[target]; on refusal it replans.
                        pending = {"step": step, "action": requested, "target": label, "reason": risk,
                                   "intent": record.get("intent"),
                                   "observation_id": observation["observation_id"],
                                   "window_handle": observation.get("window_handle"),
                                   "screenshot": observation.get("screenshot_path")}
                        record["approval"]["status"] = "pending"
                        record["outcome"] = {"status": "paused", "reason": risk}
                        history.append(record)
                        log({"event": "awaiting_approval", **pending})
                        result.update(status="awaiting_approval", pending_approval=pending,
                                      reason="A person must approve this irreversible action")
                        break

            # Independent done-check: a reviewer model compares the screen with the goal before
            # "done" is accepted. A failure is fed back so the agent can fix it in this run.
            if (action is not None and feedback is None and action["action"] == "done" and execute
                    and verify_done is not None):
                stage = "done_check"
                check = verify_done(goal, observation, history)
                tokens += check.get("tokens_used") or 0
                log({"event": "done_check", "step": step, **check})
                result.setdefault("done_checks", []).append(
                    {key: check.get(key) for key in ("goal_met", "missing", "error", "reviewer_model")})
                if check.get("goal_met") is False:
                    result["done_rejections"] = result.get("done_rejections", 0) + 1
                    feedback = {"status": "rejected", "error_code": "done_not_verified",
                                "reason": ("A separate reviewer compared the screen with the goal: "
                                           f"{check.get('missing')}. Fix this, then return done again.")[:300],
                                "partial_effects_possible": False}
                    if result["done_rejections"] >= MAX_DONE_REJECTIONS:
                        record["outcome"] = feedback
                        history.append(record)
                        log({"event": "rejected", "step": step, **record})
                        result.update(status="done_rejected",
                                      reason=f"The reviewer did not confirm the goal: {check.get('missing')}"[:300])
                        break

            if action is not None and feedback is None and action["action"] in ("done", "blocked"):
                result["status"] = "needs_verification" if action["action"] == "done" else "blocked"
                if "reason" in action:
                    result["reason"] = action["reason"]
                break
            if not execute and feedback is None:
                # A proposal run makes one decision and never pretends to perform its action.
                result.update(status="proposed", proposed_action=action)
                if record.get("approval"):
                    result["requires_approval"] = record["approval"]
                break

            if execute and feedback is None:
                stage = "before_action"
                if not still_current(observation, began):
                    feedback = {"status": "rejected", "error_code": "observation_changed",
                                "reason": "The screen/focus changed while deciding; choose again from the new screenshot",
                                "partial_effects_possible": False}
                else:
                    stage = "action_execution"
                    try:
                        outcome = dispatch(action)
                        # A returned failure is just as important as a raised tool exception.
                        if not isinstance(outcome, dict) or outcome.get("status") not in ("executed", "failed", "rejected"):
                            raise TypeError("Dispatcher must return an executed/failed/rejected outcome")
                        if outcome["status"] != "executed":
                            feedback = {"status": "failed", "error_code": "tool_reported_failure",
                                        "reason": str(outcome.get("reason", "Tool reported failure"))[:300],
                                        "partial_effects_possible": True}
                    except ForegroundNotAcquired as error:
                        feedback = {"status": "failed", "error_code": "foreground_not_acquired",
                                    "reason": str(error), "target_window_handle": error.target_handle,
                                    "actual_window_handle": error.actual_handle, "restored_window": error.restored,
                                    "native_api_error": error.api_error, "partial_effects_possible": True}
                    except (ValueError, RuntimeError, OSError, pywintypes.error) as error:
                        # Recover from ordinary desktop-tool failures; programming errors still surface.
                        reason = str(error)[:300] if isinstance(error, (ValueError, RuntimeError)) else "Native desktop operation failed"
                        feedback = {"status": "failed", "error_code": "desktop_tool_error",
                                    "error_type": type(error).__name__, "reason": reason,
                                    "partial_effects_possible": True}

            evidence = None
            if feedback is not None:
                # A failed action may have partially changed the desktop: inspect before doing anything else.
                feedback["recoverable"] = True
                record["outcome"] = feedback
                history.append(record)
                evidence = {"step": step, "stage": stage, **record,
                            "before_observation_id": observation["observation_id"],
                            "before_screenshot": observation.get("screenshot_path")}
                recoveries.append(evidence)
                if feedback["status"] == "failed":
                    failures.append(evidence)
                    event, after_event = "action_failed", "after_action_failure"
                else:
                    result["actions_rejected"] += 1
                    event, after_event = "rejected", "after_rejection"
                log({"event": event, **evidence})
                if not execute:
                    result.update(status="blocked", reason=feedback["reason"])
                    break
            else:
                # Only a completed tool call increments actions_executed; task success is separate.
                result["actions_executed"] += 1
                record["outcome"] = outcome
                history.append(record)
                log({"event": "executed", **record})
                after_event = "after_action"
                # Page loads and app launches need longer to settle than a click or keystroke.
                slow = action["action"] in ("navigate_url", "launch_app", "launch_app_gui")
                settle(timeout=5.0 if slow else 2.5)

            # This is the single observation path after success, rejection, or recoverable failure.
            stage = after_event + "_observation"
            before = observation
            observation = observe(use_omni=use_omni)
            # Did the action visibly do anything? Rejected actions never ran, so they have no effect.
            if record["outcome"].get("status") in ("executed", "failed"):
                record["effect"] = describe_effect(before, observation)
                # Typing actions read the field back: report exactly what arrived.
                readback = record["outcome"].get("readback") or {}
                if readback.get("status") == "mismatch":
                    record["effect"]["text_mismatch"] = True
                    record["effect"]["no_effect"] = False
                    record["effect"]["summary"] = (
                        f"TEXT MISMATCH: intended {readback['expected']!r} but {readback.get('field') or 'the field'} "
                        f"shows {readback['actual']!r}; " + record["effect"]["summary"])
                    result["text_mismatches"] = result.get("text_mismatches", 0) + 1
                elif readback.get("status") == "match":
                    record["effect"]["text_confirmed"] = True
                    record["effect"]["summary"] += "; field text confirmed"
                log({"event": "effect", "step": step, "effect": record["effect"]})
                if record["outcome"]["status"] == "executed":
                    guard.record(signature, record["effect"])
            if (observation["window_handle"] != initial_handle
                    and not (authorize_window and authorize_window(observation["window_handle"]))):
                raise RuntimeError("Foreground window changed outside task scope")
            if evidence is not None:
                evidence["after_observation_id"] = observation["observation_id"]
                evidence["after_screenshot"] = observation.get("screenshot_path")
            log({"event": after_event, "step": step, "data": observation})
            # The next iteration decides what to do. No failed click or typing is replayed automatically.
    except (KeyboardInterrupt, pyautogui.FailSafeException):
        result["status"] = "stopped"
    except Exception as error:
        # Provider, capture, scope and programming failures stop when a safe next observation is unavailable.
        result["usage_complete"] = getattr(error, "usage_complete", True)
        tokens += getattr(error, "tokens_used", 0)
        if hasattr(error, "safe_code"):
            result["provider_error"] = error.safe_code
        result.update(status="error", error_type=type(error).__name__, error_stage=stage)
        result["error_trace"] = [{"file": Path(frame.f_code.co_filename).name,
                                  "function": frame.f_code.co_name, "line": line}
                                 for frame, line in traceback.walk_tb(error.__traceback__)][-12:]
        if isinstance(error, (ValueError, RuntimeError)):
            result["reason"] = str(error)[:500]
    # The framework wrapper extracts screenshot paths from this observation and returns the same contract.
    if observation is not None:
        result["last_observation"] = observation
    result.update(tokens_used=tokens, actions_failed=len(failures), action_failures=failures,
                  recovery_events=recoveries)
    # A readable trajectory (targets by name, intent, visible effect) for the caller and later reflection.
    result["steps_summary"] = [summarize_record(record) for record in history]
    result["no_effect_actions"] = sum(1 for r in history if (r.get("effect") or {}).get("no_effect"))
    # Which stored lessons this run relied on (memory.py credits or blames them by the outcome).
    result["lessons_applied"] = sorted(lessons_applied)
    result["token_budget"] = {"limit": max_tokens, "per_call_limit": call_token_cap,
                              "remaining": max(0, max_tokens - tokens) if result["usage_complete"] else None}
    log({"event": "result", **result})
    return result
