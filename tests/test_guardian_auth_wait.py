"""Execute the actual Guardian readiness branch with controlled host dependencies."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class GuardianAuthWaitTests(unittest.TestCase):
    def run_guardian(self, log, *, connected=False, previous="RESTARTED-NOT-READY"):
        shell = shutil.which("pwsh")
        if not shell:
            self.skipTest("PowerShell required; hosted Windows is acceptance lane")
        source = (Path(__file__).parents[1] / "src" / "RdcGuardian.ps1").read_text()
        section = source.split("# ---------- 3. relay connection ----------", 1)[1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "agent.log"
            output.write_text(log)
            script = root / "probe.ps1"
            script.write_text("""
$ErrorActionPreference = 'Stop'
function Get-NetTCPConnection { %s }
function Write-Log { param($message) }
function Save-State { param($status) Write-Output ('STATE:' + $status) }
function Invoke-RdcHidden { Write-Output 'NOTIFIED' | Out-Host }
function Try-Restart { throw 'Unexpected restart' }
$agents = @([pscustomobject]@{Id=999;StartTime=(Get-Date).AddMinutes(-20)})
$bootGraceSec = 120
$state = @{ConsecutiveFailures=2;LastStatus='%s'}
$outLog = '%s'
$notify = 'unused'
""" % ("[pscustomobject]@{State='Established'}" if connected else "$null",
       previous, str(output).replace("'", "''")) + section)
            return subprocess.run([shell, "-NoProfile", "-File", str(script)],
                                  capture_output=True, text=True, timeout=20)

    def test_modern_wait_preserves_agent_even_without_connection(self):
        result = self.run_guardian("Please complete authentication:\nWaiting for authorization...")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("STATE:NEEDS-USER-VERIFICATION", result.stdout)
        self.assertIn("NOTIFIED", result.stdout)

    def test_rejected_refresh_token_is_not_a_restart_trigger(self):
        result = self.run_guardian("Persisted session invalid: Invalid Refresh Token: Already Used")
        self.assertEqual(result.returncode, 4, result.stderr)

    def test_legacy_prompt_is_also_preserved(self):
        result = self.run_guardian("Verify your device; pairing code", connected=True)
        self.assertEqual(result.returncode, 4, result.stderr)

    def test_repeated_wait_does_not_repeat_notification(self):
        result = self.run_guardian("Waiting for authorization", previous="NEEDS-USER-VERIFICATION")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertNotIn("NOTIFIED", result.stdout)

    def test_ready_after_authentication_supersedes_old_prompt(self):
        result = self.run_guardian("Waiting for authorization\nDevice ready", connected=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STATE:OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
