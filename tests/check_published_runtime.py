"""Hosted-only compatibility gate against the exact published npm package."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
package = root / "node_modules/@wonderwhy-er/desktop-commander"
patched_files = [package / "dist" / relative for relative in (
    "tools/improved-process-tools.js", "tools/schemas.js", "tools/edit.js",
    "tools/process.js", "handlers/filesystem-handlers.js",
)]
originals = {p:p.read_bytes() for p in patched_files}
lock = json.loads((root / "package-lock.json").read_text())
entry = lock["packages"]["node_modules/@wonderwhy-er/desktop-commander"]
assert entry["version"] == "0.3.0"
assert entry["integrity"] == "sha512-2EXNxuztSIW3fQw6OEGMek39M8yQlKinXooMYtNR4wRLhtA+f3b0cbxBggAfDeKemMWltm1qWQy/t2Av9VmYLw=="
control = root / "control"
control.mkdir()
for item in (Path(__file__).parents[1] / "src").iterdir():
    if item.is_file(): shutil.copyfile(item, control / item.name)
spec = importlib.util.spec_from_file_location("active", control / "rdc_active_hook.py")
active = importlib.util.module_from_spec(spec)
spec.loader.exec_module(active)
hook = active.configure_hook({"installation_id":"managed-0.3.0"},package)
hook.CONTROL_ROOT = control
hook.REQUEST_ROOT = root / "requests"
hook.SLRUNNER_ENTRY = root / "fixture-slrunner.py"
hook.SLRUNNER_ENTRY.write_text("# hosted fixture, never executed\n")
result = hook.ensure()
assert result["healthy"], result
assert hook.status()["healthy"]
assert hook.ensure()["changed"] is False, "install must be idempotent"
for filename in ("improved-process-tools.js","schemas.js","analienx-runner-policy.js","analienx-write-audit.js","edit.js","process.js"):
    subprocess.run(["node","--check",str(package / "dist/tools" / filename)],check=True)
subprocess.run(["node","--check",str(package / "dist/handlers/filesystem-handlers.js")],check=True)
print(json.dumps({"published_version":"0.3.0","routing_and_write_audit_healthy":True,"idempotent":True}))
# Ship the original distribution. Production installs generate their own
# content-addressed hook backups at their real destination, never CI paths.
for p,data in originals.items(): p.write_bytes(data)
for filename in ("analienx-runner-policy.js","analienx-write-audit.js"):
    (package / "dist/tools" / filename).unlink()
