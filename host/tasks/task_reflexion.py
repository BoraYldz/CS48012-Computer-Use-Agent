"""Controlled Reflexion integration test (local mode only).

The harness corrupts the target file after the first worker attempt and again after its Quick Fix.
The repeated checker evidence ends trial 1, so the run must store a lesson, roll back and pass in a
new trial.
"""
from agent.runtime import lessons_enabled

REQUEST = (
    "Create a UTF-8 text file named ReflexionProbe.txt in the sandbox root. "
    "Its content must be exactly REFLEXION_RECOVERED."
)

OUTPUT_PATH = "ReflexionProbe.txt"
EXPECTED_TEXT = "REFLEXION_RECOVERED"
INJECTED_TEXT = "REFLEXION_INJECTED_FAILURE"

PLAN = [
    {
        "id": 0,
        "capability": "files",
        "instruction": (
            "Create ReflexionProbe.txt in the sandbox root and write exactly "
            "REFLEXION_RECOVERED as its content."
        ),
        "expected_output": "ReflexionProbe.txt exists and contains REFLEXION_RECOVERED.",
        "check_spec": {
            "checks": [{"kind": "text_equals", "path": OUTPUT_PATH, "text": EXPECTED_TEXT}],
            "rationale": "Verify that the entire file exactly matches the required marker text.",
        },
    }
]


async def setup(vm):
    arm_fault = getattr(vm, "arm_write_fault", None)
    if not callable(arm_fault):
        raise RuntimeError("The Reflexion diagnostic requires AGENT_MODE=local with the direct transport.")
    await vm.call("harness_remove_path", path=OUTPUT_PATH)     # before the snapshot is taken
    arm_fault(path=OUTPUT_PATH, replacement_content=INJECTED_TEXT, shots=2)


async def check(vm):
    if not getattr(vm, "fault_injection_triggered", False):
        return False, "test fault was not triggered; Reflexion path was not exercised"
    info = await vm.call("path_info", path=OUTPUT_PATH)
    if not info.get("exists") or info.get("is_dir"):
        return False, f"{OUTPUT_PATH} is missing or is not a file"
    content = await vm.call("read_text", path=OUTPUT_PATH)
    if content != EXPECTED_TEXT:
        return False, f"final content of {OUTPUT_PATH} is wrong: {content[:160]!r}"
    return True, "final content verified"


def validate_run(final):
    """Require a failed first trial followed by a verified later one."""
    trials = final.get("trials") or []
    if len(trials) < 2:
        why = final.get("reflexion_off") or (trials[0]["stop"] if trials else "MAX_ATTEMPTS must be at least 2")
        return False, f"no second trial: {why}"
    if trials[0]["status"] != "failed":
        return False, f"first trial ended {trials[0]['status']}, expected failed"
    if final.get("checks", {}).get("0", {}).get("status") != "pass":
        return False, "final deterministic subgoal check did not pass"
    lessons = final.get("lessons") or []
    if lessons_enabled() and not lessons:
        return False, "no lesson was stored"
    note = lessons[-1]["lesson"] if lessons else "lessons off"
    return True, f"recovered in trial {len(trials)}; {note}"