"""Controlled Quick Fix integration test (local mode only).

After the first worker attempt writes the target file, the local harness
deliberately corrupts its persisted contents before verification. The
deterministic checker must catch the mismatch, the critic must issue a repair
instruction, and the same worker must write the correct content on its retry.
"""

REQUEST = (
    "Create a UTF-8 text file named QuickFixProbe.txt in the sandbox root. "
    "Its content must be exactly QUICK_FIX_RECOVERED."
)

OUTPUT_PATH = "QuickFixProbe.txt"
EXPECTED_TEXT = "QUICK_FIX_RECOVERED"
INJECTED_TEXT = "QUICK_FIX_INJECTED_FAILURE"

# Fixed acceptance criteria deliberately isolate the Quick Fix path from
# planner variance. Task 16 already exercised the live planner successfully.
PLAN = [
    {
        "id": 0,
        "capability": "files",
        "instruction": (
            "Create QuickFixProbe.txt in the sandbox root and write exactly "
            "QUICK_FIX_RECOVERED as its content."
        ),
        "expected_output": (
            "QuickFixProbe.txt exists and contains QUICK_FIX_RECOVERED."
        ),
        "check_spec": {
            "checks": [
                {
                    "kind": "text_equals",
                    "path": OUTPUT_PATH,
                    "text": EXPECTED_TEXT,
                }
            ],
            "rationale": "Verify that the entire file exactly matches the required marker text.",
        },
    }
]


async def setup(vm):
    arm_fault = getattr(vm, "arm_write_fault", None)
    if not callable(arm_fault):
        raise RuntimeError(
            "The Quick Fix diagnostic requires AGENT_MODE=local; "
            "it uses a local-only, one-shot test fault."
        )

    # Remove only the diagnostic file, never the sandbox or other user files.
    await vm.call("harness_remove_path", path=OUTPUT_PATH)
    arm_fault(path=OUTPUT_PATH, replacement_content=INJECTED_TEXT)


async def check(vm):
    if not getattr(vm, "fault_injection_triggered", False):
        return False, "test fault was not triggered; Quick Fix path was not exercised"

    info = await vm.call("path_info", path=OUTPUT_PATH)
    if not info.get("exists") or info.get("is_dir"):
        return False, f"{OUTPUT_PATH} is missing or is not a file"

    content = await vm.call("read_text", path=OUTPUT_PATH)
    if content != EXPECTED_TEXT:
        return False, (
            f"Quick Fix did not restore the exact expected marker in {OUTPUT_PATH}; "
            f"actual content: {content[:160]!r}"
        )

    return True, "injected first-attempt write corruption detected and repaired; final content verified"


def validate_run(final):
    """Require evidence that one real LLM-critic repair occurred."""
    attempts = final.get("quick_fix_attempts", {}).get("0", 0)
    if attempts != 1:
        return False, f"expected exactly one Quick Fix attempt, observed {attempts}"

    critic_used = final.get("quick_fix_critic", {}).get("0") is True
    if not critic_used:
        return False, "repair used the deterministic fallback; LLM critic was not verified"

    check = final.get("checks", {}).get("0", {})
    if check.get("status") != "pass":
        return False, "final deterministic subgoal check did not pass"

    return True, "one LLM-critic-guided repair completed and passed deterministic verification"
