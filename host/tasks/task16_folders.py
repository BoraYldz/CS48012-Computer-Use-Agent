"""Task 16 - Proje klasör yapısı oluşturma (smoke test for the whole pipeline)."""
import base64

REQUEST = ("In Desktop, create a folder named Proje with three subfolders: "
           "Girdiler, Çıktılar and Raporlar.")

EXISTING = "Desktop/notlar.txt"  # must survive untouched


async def setup(vm):
    await vm.call("harness_put_file", path=EXISTING,
                  b64=base64.b64encode(b"do not touch").decode())


async def check(vm):
    for sub in ["Girdiler", "Çıktılar", "Raporlar"]:
        info = await vm.call("path_info", path=f"Desktop/Proje/{sub}")
        if not info.get("is_dir"):
            return False, f"missing folder Desktop/Proje/{sub}"
    if base64.b64decode(await vm.call("harness_get_file", path=EXISTING)) != b"do not touch":
        return False, "existing file was changed"
    return True, "all folders exist, existing file untouched"
