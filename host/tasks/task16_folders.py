"""Task 16 - Proje klasör yapısı oluşturma (smoke test for the whole pipeline)."""
import base64

REQUEST = (
    "In Desktop (the sandbox-relative folder named Desktop), create exactly "
    "this directory structure. All paths below are relative to the sandbox root; "
    "do not create Proje at the sandbox root:\n"
    "Desktop/Proje/\n"
    "Desktop/Proje/Girdiler/\n"
    "Desktop/Proje/Çıktılar/\n"
    "Desktop/Proje/Raporlar/"
)

EXISTING = "Desktop/notlar.txt"  # must survive untouched


async def setup(vm):
    # Remove only this task's expected output so stale folders cannot make a
    # later run pass without the worker doing the work. Also remove the known
    # wrong-location output created by the earlier planner bug.
    await vm.call("harness_remove_path", path="Desktop/Proje")
    await vm.call("harness_remove_path", path="Proje")
    await vm.call("harness_put_file", path=EXISTING,
                  b64=base64.b64encode(b"do not touch").decode())


async def check(vm):
    misplaced = await vm.call("path_info", path="Proje")
    if misplaced.get("exists"):
        return False, "unexpected folder Proje at sandbox root; expected Desktop/Proje"
    for sub in ["Girdiler", "Çıktılar", "Raporlar"]:
        info = await vm.call("path_info", path=f"Desktop/Proje/{sub}")
        if not info.get("is_dir"):
            return False, f"missing folder Desktop/Proje/{sub}"
    if base64.b64decode(await vm.call("harness_get_file", path=EXISTING)) != b"do not touch":
        return False, "existing file was changed"
    return True, "all folders exist, existing file untouched"
