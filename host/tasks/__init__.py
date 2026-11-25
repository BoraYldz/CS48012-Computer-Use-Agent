"""Task registry. Each task module defines REQUEST, async setup(vm), async check(vm) -> (bool, str)."""
import importlib

TASKS = {
    "16": "tasks.task16_folders",
    "quickfix": "tasks.task_quick_fix",
}


def load(task_id: str):
    return importlib.import_module(TASKS[task_id])
