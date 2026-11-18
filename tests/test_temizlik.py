"""Regression tests for Çöpcüler Kralı (Trash King).

All tests run only in temporary Git repositories created with tempfile and with a temporary
COPCULER_STATE_DIR. No real target project is touched.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import temizlik  # noqa: E402


GIT_ENV = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
GIT_ENV.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)


def run_git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, env=GIT_ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check,
    )


def make_repo(base, files, commit=True):
    """Create a temporary Git repo under base. files: {name: bytes|str}."""
    root = Path(base) / "project"
    root.mkdir()
    run_git(root, "init", "-q")
    run_git(root, "config", "core.autocrlf", "false")
    for name, content in files.items():
        write(root, name, content)
    if commit:
        run_git(root, "add", "-A")
        run_git(root, "commit", "-q", "-m", "initial")
    return root.resolve()


def write(root, name, content):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


def read(root, name):
    return (Path(root) / name).read_bytes()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="copculer-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.state = self.tmp / "state"
        patcher = mock.patch.dict(os.environ, {"COPCULER_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.confirmed = []
        self.real_confirm = temizlik.confirm

        def fake_confirm(action, approval):
            self.confirmed.append((action, approval))

        patcher = mock.patch.object(temizlik, "confirm", fake_confirm)
        patcher.start()
        self.addCleanup(patcher.stop)

    def repo(self, files, commit=True):
        return make_repo(self.tmp, files, commit)

    def run_cli(self, *argv):
        """Run main(); return (exit_ok, stdout, stderr_message)."""
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                temizlik.main([str(item) for item in argv])
        except (temizlik.Refusal, OSError, ValueError, KeyError, IndexError, TypeError) as error:
            return False, out.getvalue(), str(error)
        return True, out.getvalue(), ""


class HelperSmokeTest(Base):
    def test_make_repo_and_state_env(self):
        root = self.repo({"a.py": "x = 1\n"})
        self.assertEqual(read(root, "a.py"), b"x = 1\n")
        self.assertEqual(temizlik.state_dir(), self.state)
        # The repo really lives in the temporary directory.
        self.assertTrue(temizlik.is_inside(root, self.tmp))

    def test_snapshot_runs(self):
        root = self.repo({"a.py": "x = 1\n"})
        ok, out, _ = self.run_cli("snapshot", root)
        self.assertTrue(ok)
        self.assertIn("a.py", json.loads(out)["files"])


class StateDirTest(Base):
    def test_env_override_has_priority(self):
        self.assertEqual(temizlik.state_dir(), self.state)

    def test_default_windows_location(self):
        env = {"LOCALAPPDATA": "L"}
        self.assertEqual(temizlik.default_state_path("nt", env, "H"), os.path.join("L", "copculer_kral"))
        self.assertEqual(temizlik.default_state_path("nt", {}, "H"),
                         os.path.join("H", "AppData", "Local", "copculer_kral"))

    def test_default_posix_location(self):
        self.assertEqual(temizlik.default_state_path("posix", {"XDG_STATE_HOME": "X"}, "H"),
                         os.path.join("X", "copculer_kral"))
        self.assertEqual(temizlik.default_state_path("posix", {}, "H"),
                         os.path.join("H", ".local", "state", "copculer_kral"))

    def test_state_dir_inside_project_is_refused(self):
        root = self.repo({"a.py": "x = 1\n"})
        with mock.patch.dict(os.environ, {"COPCULER_STATE_DIR": str(root / "inside" / "state")}):
            with self.assertRaises(temizlik.Refusal):
                temizlik.prepare_state(root)
        self.assertFalse((root / "inside").exists())

    def test_state_dir_equal_to_project_is_refused(self):
        root = self.repo({"a.py": "x = 1\n"})
        with mock.patch.dict(os.environ, {"COPCULER_STATE_DIR": str(root)}):
            with self.assertRaises(temizlik.Refusal):
                temizlik.prepare_state(root)

    def test_state_dir_is_created_outside(self):
        root = self.repo({"a.py": "x = 1\n"})
        base = temizlik.prepare_state(root)
        self.assertTrue(base.is_dir())

    @unittest.skipIf(os.name == "nt", "POSIX permissions cannot be observed on Windows (see docs/manual-checks.md)")
    def test_state_dir_mode_0700(self):
        root = self.repo({"a.py": "x = 1\n"})
        base = temizlik.prepare_state(root)
        self.assertEqual(base.stat().st_mode & 0o777, 0o700)

    def test_plan_inside_project_is_refused(self):
        root = self.repo({"a.py": "x = 1\n"})
        plan = root / "plan.json"
        plan.write_text(json.dumps({"snapshot": "x", "candidates": []}))
        ok, _, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertFalse(ok)
        self.assertIn("The plan file", message)

    def test_plan_outside_project_passes_location_check(self):
        root = self.repo({"a.py": "x = 1\n"})
        plan = self.tmp / "plan.json"
        plan.write_text(json.dumps({"snapshot": "x", "candidates": []}))
        ok, _, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertFalse(ok)
        self.assertNotIn("The plan file", message)  # rejected for another reason (the snapshot digest)


class Flow(Base):
    """Helpers: write plans, run review, apply, undo, recover."""

    DIGEST = re.compile(r"Approval digest ([0-9a-f]{64})")
    RECEIPT = re.compile(r"Receipt id ([0-9a-f]{32})")

    def make_plan(self, root, candidates):
        ok, out, message = self.run_cli("snapshot", root)
        self.assertTrue(ok, message)
        plan = self.tmp / "plan.json"
        plan.write_text(json.dumps({"snapshot": json.loads(out)["snapshot"], "candidates": candidates}), encoding="utf-8")
        return plan

    @staticmethod
    def forced_complete():
        """Pretend every candidate is REVIEW_COMPLETE so write-pipeline tests stay focused on writing."""
        def fake(root, files, excluded, selected, ops):
            return {item["id"]: "REVIEW_COMPLETE" for item in selected}, []

        return mock.patch.object(temizlik, "review_statuses", fake)

    def review(self, root, plan, *ids, force=False):
        with (self.forced_complete() if force else contextlib.nullcontext()):
            ok, out, message = self.run_cli("review", root, plan, "--ids", *ids)
        self.assertTrue(ok, message)
        return self.DIGEST.search(out).group(1)

    def apply(self, root, plan, *ids, digest=None, force=True):
        """force=True uses forced_complete for review and apply; force=False uses the real statuses."""
        with (self.forced_complete() if force else contextlib.nullcontext()):
            digest = digest or self.review(root, plan, *ids, force=force)
            ok, out, message = self.run_cli("apply", root, plan, "--ids", *ids, "--digest", digest)
        found = self.RECEIPT.search(out)
        return ok, (found.group(1) if found else None), message

    def apply_ok(self, root, plan, *ids):
        ok, receipt, message = self.apply(root, plan, *ids)
        self.assertTrue(ok, message)
        return receipt

    def receipt_dir(self, receipt_id):
        return self.state / "receipts" / receipt_id

    def journal(self, receipt_id):
        return json.loads((self.receipt_dir(receipt_id) / "journal.json").read_bytes())

    def rewrite_receipt(self, receipt_id, mutate):
        """Simulates an attacker who can write to the state dir: receipt and index change consistently."""
        path = self.receipt_dir(receipt_id) / "receipt.json"
        data = json.loads(path.read_bytes())
        mutate(data)
        raw = temizlik.encoded(data)
        path.write_bytes(raw)
        index = self.state / "index.jsonl"
        lines = []
        for line in index.read_bytes().splitlines():
            entry = json.loads(line)
            if entry["receipt_id"] == receipt_id:
                entry["receipt_sha256"] = temizlik.digest(raw)
            lines.append(temizlik.encoded(entry))
        index.write_bytes(b"\n".join(lines) + b"\n")

    def failing_on(self, marker):
        real = temizlik.apply_patch

        def flaky(root, patch, *, reverse=False, check=False):
            if not check and marker in patch:
                raise temizlik.Refusal("enjekte edilen hata")
            return real(root, patch, reverse=reverse, check=check)

        return mock.patch.object(temizlik, "apply_patch", flaky)


THREE = {"one.py": "a\nb\nc\n", "two.py": "a\nb\nc\n", "three.py": "a\nb\nc\n"}
THREE_PLAN = [
    {"id": "C1", "path": "one.py", "action": "remove_lines", "lines": [[2, 2]]},
    {"id": "C2", "path": "two.py", "action": "remove_lines", "lines": [[2, 2]]},
    {"id": "C3", "path": "three.py", "action": "remove_lines", "lines": [[2, 2]]},
]
EDITED = b"a\nc\n"
ORIGINAL = b"a\nb\nc\n"


class WritePipelineTest(Flow):
    """Section 2: shared write pipeline, journal, lock, recover."""

    def test_apply_writes_and_journals(self):
        root = self.repo(THREE)
        plan = self.make_plan(root, THREE_PLAN)
        receipt = self.apply_ok(root, plan, "C1", "C2", "C3")
        for name in THREE:
            self.assertEqual(read(root, name), EDITED)
        journal = self.journal(receipt)
        self.assertEqual((journal["direction"], journal["status"]), ("apply", "complete"))
        self.assertEqual(set(journal["files"].values()), {"done"})
        self.assertEqual(self.confirmed[-1][0], "APPLY")

    def test_wrong_digest_refused_and_nothing_written(self):  # scenario 8
        root = self.repo(THREE)
        plan = self.make_plan(root, THREE_PLAN)
        ok, receipt, message = self.apply(root, plan, "C1", digest="0" * 64)
        self.assertFalse(ok)
        self.assertIn("digest does not match", message)
        self.assertEqual(read(root, "one.py"), ORIGINAL)
        self.assertEqual(self.confirmed, [])
        self.assertFalse((self.state / "receipts").exists())

    def test_file_changed_during_confirmation_refused(self):  # scenario 8
        root = self.repo(THREE)
        plan = self.make_plan(root, THREE_PLAN)
        digest = self.review(root, plan, "C1", force=True)

        def tamper(action, approval):
            write(root, "one.py", "a\nX\nc\n")

        with mock.patch.object(temizlik, "confirm", tamper):
            ok, receipt, message = self.apply(root, plan, "C1", digest=digest)
        self.assertFalse(ok)
        self.assertEqual(read(root, "one.py"), b"a\nX\nc\n")
        self.assertFalse((self.state / "receipts").exists())

    def test_selection_emptying_directory_refused(self):  # scenario 15
        root = self.repo({"pkg/only.py": "x = 1\n", "main.py": "y = 2\n"})
        plan = self.make_plan(root, [{"id": "C1", "path": "pkg/only.py", "action": "delete_file"}])
        ok, _, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertFalse(ok)
        self.assertIn("empty a directory", message)
        self.assertTrue((root / "pkg" / "only.py").exists())

    def test_crlf_and_missing_final_newline_preserved(self):  # scenario 16 (only autocrlf=false, see manual-checks)
        original = b"a\r\nb\r\nc"
        root = self.repo({"crlf.py": original})
        plan = self.make_plan(root, [{"id": "C1", "path": "crlf.py", "action": "remove_lines", "lines": [[2, 2]]}])
        receipt = self.apply_ok(root, plan, "C1")
        self.assertEqual(read(root, "crlf.py"), b"a\r\nc")
        ok, out, message = self.run_cli("undo", root, receipt)
        self.assertTrue(ok, message)
        self.assertEqual(read(root, "crlf.py"), original)

    def test_new_file_patch_headers(self):
        patch = temizlik.file_patch("a.py", None, b"x\n", "100644")
        self.assertIn(b"new file mode 100644\n", patch)
        self.assertIn(b"--- /dev/null", patch)
        self.assertIn(b"+++ b/a.py", patch)
        patch = temizlik.file_patch("a.py", b"x\n", None, "100644")
        self.assertIn(b"deleted file mode 100644\n", patch)
        self.assertIn(b"+++ /dev/null", patch)

    def test_confirm_refuses_without_terminal(self):
        with mock.patch.object(sys, "stdin", io.StringIO()):
            with self.assertRaises(temizlik.Refusal):
                self.real_confirm("APPLY", "x" * 64)

    def test_partial_failure_is_journaled_and_locks(self):  # scenario 9
        root = self.repo(THREE)
        plan = self.make_plan(root, THREE_PLAN)
        with self.failing_on(b"b/two.py"):
            ok, receipt, message = self.apply(root, plan, "C1", "C2", "C3")
        self.assertFalse(ok)
        self.assertIn("recover", message)
        self.assertIn(receipt, message)
        journal = self.journal(receipt)
        self.assertEqual(journal["status"], "partial")
        self.assertEqual(journal["files"], {"one.py": "done", "two.py": "failed", "three.py": "todo"})
        self.assertEqual(read(root, "one.py"), EDITED)
        self.assertEqual(read(root, "two.py"), ORIGINAL)
        self.assertEqual(read(root, "three.py"), ORIGINAL)
        # Kilit: yeni apply ve undo reddedilir.
        plan = self.make_plan(root, THREE_PLAN)
        ok, _, message = self.apply(root, plan, "C3", digest="0" * 64)
        self.assertFalse(ok)
        self.assertIn("interrupted operation", message)
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)
        self.assertIn("recover", message)

    def _partial(self):
        root = self.repo(THREE)
        plan = self.make_plan(root, THREE_PLAN)
        with self.failing_on(b"b/two.py"):
            ok, receipt, _ = self.apply(root, plan, "C1", "C2", "C3")
        self.assertFalse(ok)
        return root, receipt

    def test_recover_backward_restores_original(self):  # scenario 9
        root, receipt = self._partial()
        ok, out, message = self.run_cli("recover", root, receipt, "--backward")
        self.assertTrue(ok, message)
        for name in THREE:
            self.assertEqual(read(root, name), ORIGINAL)
        self.assertEqual(self.journal(receipt)["status"], "reverted")
        self.assertEqual(self.confirmed[-1][0], "RECOVER")
        # The lock is gone: a new apply works.
        plan = self.make_plan(root, THREE_PLAN)
        self.apply_ok(root, plan, "C3")

    def test_recover_forward_reaches_target(self):  # scenario 9
        root, receipt = self._partial()
        ok, out, message = self.run_cli("recover", root, receipt, "--forward")
        self.assertTrue(ok, message)
        for name in THREE:
            self.assertEqual(read(root, name), EDITED)
        self.assertEqual(self.journal(receipt)["status"], "complete")
        ok, out, message = self.run_cli("undo", root, receipt)
        self.assertTrue(ok, message)
        for name in THREE:
            self.assertEqual(read(root, name), ORIGINAL)

    def test_recover_refuses_user_change_on_untouched_file(self):  # scenario 9
        for mode in ("--forward", "--backward"):
            with self.subTest(mode=mode):
                root, receipt = self._partial_fresh()
                write(root, "two.py", "user\n")  # a user change on a file that was not written
                before = {name: read(root, name) for name in THREE}
                ok, out, message = self.run_cli("recover", root, receipt, mode)
                self.assertFalse(ok)
                self.assertIn("two.py", message)
                self.assertEqual({name: read(root, name) for name in THREE}, before)
                self.assertEqual(self.journal(receipt)["status"], "partial")

    def test_recover_refuses_user_change_on_done_file(self):
        root, receipt = self._partial()
        write(root, "one.py", "user\n")
        ok, out, message = self.run_cli("recover", root, receipt, "--backward")
        self.assertFalse(ok)
        self.assertIn("one.py", message)
        self.assertEqual(read(root, "one.py"), "user\n".encode())
        self.assertEqual(read(root, "three.py"), ORIGINAL)

    def test_recover_on_complete_receipt_refused(self):
        root = self.repo(THREE)
        plan = self.make_plan(root, THREE_PLAN)
        receipt = self.apply_ok(root, plan, "C1")
        ok, _, message = self.run_cli("recover", root, receipt, "--forward")
        self.assertFalse(ok)

    def _partial_fresh(self):
        """Set up a partial failure with a fresh temp dir and state dir for subtests."""
        self.tmp = Path(tempfile.mkdtemp(prefix="copculer-sub-", dir=self._tmp.name)).resolve()
        self.state = self.tmp / "state"
        patcher = mock.patch.dict(os.environ, {"COPCULER_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return self._partial()


class UndoTest(Flow):
    """Section 3: reliable undo. The source of trust is the backup in the state directory."""

    FILES = {"src/a.py": "l1\nl2\nl3\nl4\n", "src/b.py": "k1\nk2\n", "keep.py": "z = 1\n"}
    PLAN = [
        {"id": "C1", "path": "src/a.py", "action": "remove_lines", "lines": [[2, 3]]},
        {"id": "C2", "path": "src/b.py", "action": "delete_file"},
    ]

    def snapshot_bytes(self, root):
        return {name: read(root, name) for name in self.FILES if (root / name).exists()}

    def applied(self, files=None, commit=True):
        root = self.repo(files or self.FILES, commit=commit)
        original = self.snapshot_bytes(root)
        plan = self.make_plan(root, self.PLAN)
        receipt = self.apply_ok(root, plan, "C1", "C2")
        return root, receipt, original

    def test_happy_path_restores_exact_bytes(self):  # scenario 1
        root, receipt, original = self.applied()
        self.assertEqual(read(root, "src/a.py"), b"l1\nl4\n")
        self.assertFalse((root / "src" / "b.py").exists())
        ok, out, message = self.run_cli("undo", root, receipt)
        self.assertTrue(ok, message)
        self.assertEqual(self.snapshot_bytes(root), original)
        self.assertEqual(self.confirmed[-1][0], "UNDO")
        self.assertIn("cross-check: OK", out)  # Git cross-check
        journal = self.journal(receipt)
        self.assertEqual((journal["direction"], journal["status"]), ("undo", "complete"))
        # A second undo is refused.
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)

    def test_receipt_has_no_patch_field_and_is_in_state_dir_only(self):
        root, receipt, _ = self.applied()
        data = json.loads((self.receipt_dir(receipt) / "receipt.json").read_bytes())
        self.assertNotIn("patch", data)
        self.assertTrue(temizlik.is_inside(self.receipt_dir(receipt), self.state))
        self.assertFalse(temizlik.is_inside(self.state, root))

    def test_tampered_receipt_json_refused(self):  # scenario 2
        root, receipt, _ = self.applied()
        path = self.receipt_dir(receipt) / "receipt.json"
        path.write_bytes(path.read_bytes().replace(b'"version":1', b'"version":1 '))
        before = {name: read(root, name) for name in ("src/a.py", "keep.py")}
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)
        self.assertIn("digest", message)
        self.assertEqual({name: read(root, name) for name in before}, before)
        self.assertFalse((root / "src" / "b.py").exists())

    def test_tampered_backup_refused(self):  # scenario 3
        root, receipt, _ = self.applied()
        (self.receipt_dir(receipt) / "before" / "0.bin").write_bytes(b"saldirgan\n")
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)
        self.assertIn("is corrupted", message)
        self.assertEqual(read(root, "src/a.py"), b"l1\nl4\n")

    def test_missing_backup_refused(self):
        root, receipt, _ = self.applied()
        (self.receipt_dir(receipt) / "before" / "1.bin").unlink()
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)
        self.assertFalse((root / "src" / "b.py").exists())

    def test_old_patch_field_is_ignored(self):
        root, receipt, original = self.applied()
        evil = temizlik.file_patch("keep.py", b"z = 1\n", b"evil\n")
        self.rewrite_receipt(receipt, lambda data: data.update(patch=evil.hex()))
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertTrue(ok, message)
        self.assertEqual(self.snapshot_bytes(root), original)
        self.assertEqual(read(root, "keep.py"), b"z = 1\n")

    def test_consistent_state_tampering_caught_by_pinned_commit(self):  # scenario 4
        """Even if an attacker changes the backup, before_sha and the index digest consistently, the pinned commit catches it."""
        root, receipt, _ = self.applied()
        fake = b"saldirgan\n"
        (self.receipt_dir(receipt) / "before" / "0.bin").write_bytes(fake)
        self.rewrite_receipt(receipt, lambda data: data["files"][0].update(before_sha=temizlik.digest(fake)))
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)
        self.assertIn("Git cross-check mismatch", message)
        self.assertEqual(read(root, "src/a.py"), b"l1\nl4\n")

    def test_wrong_base_blob_refused(self):  # scenario 4
        root, receipt, _ = self.applied()
        other = run_git(root, "rev-parse", "HEAD:keep.py").stdout.decode().strip()
        self.rewrite_receipt(receipt, lambda data: data["files"][0].update(base_blob=other))
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)
        self.assertIn("Git cross-check mismatch", message)

    def test_unreachable_commit_reports_and_uses_backup(self):  # scenario 4
        root, receipt, original = self.applied()
        run_git(root, "commit", "--amend", "--allow-empty", "-q", "-m", "baska")
        run_git(root, "reflog", "expire", "--expire=now", "--all")
        run_git(root, "gc", "-q", "--prune=now")
        base = json.loads((self.receipt_dir(receipt) / "receipt.json").read_bytes())["base_commit"]
        self.assertNotEqual(run_git(root, "cat-file", "-e", base + "^{commit}", check=False).returncode, 0)
        ok, out, message = self.run_cli("undo", root, receipt)
        self.assertTrue(ok, message)
        self.assertIn("not possible", out)
        self.assertEqual(self.snapshot_bytes(root), original)

    def test_head_advanced_after_apply(self):  # scenario 5
        root, receipt, original = self.applied()
        run_git(root, "add", "-A")
        run_git(root, "commit", "-q", "-m", "temizlik")
        self.assertNotEqual(run_git(root, "rev-parse", "HEAD").stdout.decode().strip(),
                            json.loads((self.receipt_dir(receipt) / "receipt.json").read_bytes())["base_commit"])
        ok, out, message = self.run_cli("undo", root, receipt)
        self.assertTrue(ok, message)
        self.assertIn("cross-check: OK", out)
        self.assertEqual(self.snapshot_bytes(root), original)

    def test_uncommitted_changes_in_target_file(self):  # scenario 6
        root = self.repo(self.FILES)
        write(root, "src/a.py", "l1\nl2\nl3\nl4\nlocal\n")  # uncommitted change
        original = self.snapshot_bytes(root)
        plan = self.make_plan(root, self.PLAN)
        receipt = self.apply_ok(root, plan, "C1", "C2")
        data = json.loads((self.receipt_dir(receipt) / "receipt.json").read_bytes())
        self.assertIsNone(data["files"][0]["base_blob"])  # the blob in the commit does not match the backup
        self.assertIsNotNone(data["files"][1]["base_blob"])
        ok, out, message = self.run_cli("undo", root, receipt)
        self.assertTrue(ok, message)
        self.assertEqual(self.snapshot_bytes(root), original)

    def test_user_changed_file_after_apply_refused(self):  # scenario 7
        root, receipt, _ = self.applied()
        write(root, "src/a.py", "l1\nkullanici\nl4\n")
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)
        self.assertIn("src/a.py", message)
        self.assertEqual(read(root, "src/a.py"), b"l1\nkullanici\nl4\n")
        self.assertFalse((root / "src" / "b.py").exists())

    def test_recreated_deleted_file_refused(self):  # scenario 7 (deleted file recreated)
        root, receipt, _ = self.applied()
        write(root, "src/b.py", "yeni\n")
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)
        self.assertIn("src/b.py", message)
        self.assertEqual(read(root, "src/b.py"), b"yeni\n")

    def test_invalid_receipt_ids_refused(self):
        root, receipt, _ = self.applied()
        for bad in ("../" + receipt, receipt.upper(), receipt[:-1], "0" * 32, receipt + "/x"):
            with self.subTest(bad=bad):
                ok, _, message = self.run_cli("undo", root, bad)
                self.assertFalse(ok)

    def test_receipt_of_other_project_refused(self):
        root, receipt, _ = self.applied()
        sibling = self.tmp / "baska"
        sibling.mkdir()
        other = make_repo(sibling, {"x.py": "1\n"})
        ok, _, message = self.run_cli("undo", other, receipt)
        self.assertFalse(ok)
        self.assertIn("does not belong to this project", message)

    def test_receipt_not_in_index_refused(self):
        root, receipt, _ = self.applied()
        (self.state / "index.jsonl").write_bytes(b"")
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)
        self.assertIn("was not found", message)

    def test_undo_state_dir_inside_project_refused(self):
        root, receipt, _ = self.applied()
        with mock.patch.dict(os.environ, {"COPCULER_STATE_DIR": str(root / "state")}):
            ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)
        self.assertIn("state directory", message)

    def test_deleted_executable_mode_is_recorded(self):
        root, receipt, _ = self.applied()
        data = json.loads((self.receipt_dir(receipt) / "receipt.json").read_bytes())
        self.assertIn(data["files"][1]["mode"], {"100644", "100755"})


APP = "def old():\n    return 1\n\ndef main():\n    return 2\n"
REMOVE_ESKI = {"id": "C1", "path": "src/app.py", "action": "remove_lines", "lines": [[1, 3]], "symbols": ["old"]}


class ScopeTest(Flow):
    """Section 4: out-of-scope reporting, snapshot reasons."""

    def excluded(self, root):
        ok, out, message = self.run_cli("snapshot", root)
        self.assertTrue(ok, message)
        data = json.loads(out)
        self.assertEqual(data["excluded_count"], len(data["excluded"]))
        return {item["path"]: item["reason"] for item in data["excluded"]}, data

    def test_snapshot_excluded_paths_and_reasons(self):  # scenario 18
        files = {
            "src/ok.py": "x = 1\n",
            ".github/workflows/ci.yml": "name: ci\n",
            "secrets.py": "TOKEN = 'abc'\n",
            "secrets/a.py": "x = 1\n",
            ".env": "A=1\n",
            ".env.local": "A=1\n",
            "config/key.pem": "pem\n",
            ".npmrc": "_authToken=zzz\n",
            "data/x.py": "x = 1\n",
            "image.png": b"\x89PNG\x00\x01",
            ".hidden/a.py": "x = 1\n",
            "bin.py": b"a\x00b\n",
            "latin.py": b"caf\xe9\n",
            "leak.py": 'password = "supersecret123"\n',
            "tpl/page.jinja": "{{ old() }}\n",
            "big.py": "#" * 1_200_000 + "\n",
            "huge.py": "#" * 5_100_000 + "\n",
            "gone.py": "x = 1\n",
        }
        root = self.repo(files)
        (root / "gone.py").unlink()
        excluded, data = self.excluded(root)
        self.assertEqual(excluded, {
            "secrets.py": "sensitive_name", "secrets/a.py": "sensitive_dir", ".env": "sensitive_name",
            ".env.local": "sensitive_name", "config/key.pem": "sensitive_name", ".npmrc": "sensitive_name",
            "data/x.py": "blocked_dir", "image.png": "extension", ".hidden/a.py": "dotfile",
            "bin.py": "binary", "latin.py": "non_utf8", "leak.py": "secret_content",
            "tpl/page.jinja": "extension", "big.py": "size", "huge.py": "too_large", "gone.py": "missing",
        })
        self.assertEqual(data["files"], [".github/workflows/ci.yml", "src/ok.py"])
        self.assertNotIn("supersecret123", json.dumps(data))

    def test_snapshot_digest_covers_exclusions(self):
        root = self.repo({"a.py": "x = 1\n", "b.py": "y = 1\n"})
        _, before = self.excluded(root)
        write(root, "b.py", b"y\x00\n")  # drops out of scope
        excluded, after = self.excluded(root)
        self.assertEqual(excluded, {"b.py": "binary"})
        self.assertNotEqual(before["snapshot"], after["snapshot"])

    def test_sensitive_names_are_never_opened(self):
        files = {"src/app.py": APP, "secrets.py": "old()\n", ".env": "old=1\n", "config/key.pem": "old\n",
                 "secrets/a.py": "old\n", ".npmrc": "old\n"}
        root = self.repo(files)
        opened = []
        real_open = Path.open

        def spy(self_path, *args, **kwargs):
            opened.append(self_path.name)
            return real_open(self_path, *args, **kwargs)

        with mock.patch.object(Path, "open", spy):
            self.assertTrue(self.run_cli("snapshot", root)[0])
            self.assertTrue(self.run_cli("refs", root, "old")[0])
            plan = self.make_plan(root, [REMOVE_ESKI])
            self.assertTrue(self.run_cli("review", root, plan, "--ids", "C1")[0])
        for name in ("secrets.py", ".env", "key.pem", ".npmrc", "a.py"):
            self.assertNotIn(name, opened)
        self.assertIn("app.py", opened)  # the spy really works: the in-scope file was opened

    def test_symlink_and_hardlink_excluded_as_link(self):  # scenario 17
        root = self.repo({"real.py": "x = 1\n", "twin.py": "y = 1\n", "other.py": "z = 1\n"})
        os.remove(root / "twin.py")
        os.link(root / "real.py", root / "twin.py")  # hard link
        excluded, _ = self.excluded(root)
        self.assertEqual(excluded.get("real.py"), "link")
        self.assertEqual(excluded.get("twin.py"), "link")
        self.assertNotIn("other.py", excluded)

    def test_symlink_excluded_as_link(self):  # scenario 17
        root = self.repo({"real.py": "x = 1\n", "ln.py": "y = 1\n"})
        os.remove(root / "ln.py")
        try:
            os.symlink(root / "real.py", root / "ln.py")
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Could not create a symlink ({error}); see docs/manual-checks.md")
        excluded, _ = self.excluded(root)
        self.assertEqual(excluded.get("ln.py"), "link")
        self.assertNotIn("real.py", excluded)


class RefsTest(Flow):
    def files(self, **extra):
        files = {"src/app.py": APP, "src/other.py": "def other():\n    return 3\n"}
        files.update(extra)
        return files

    def test_refs_reports_lines_counts_and_not_reviewed(self):
        root = self.repo(self.files(**{
            "src/user.py": "from app import old\n\nold()\n",
            "data/x.py": "old()\nold()\n",
            "tpl/page.jinja": "{{ old() }}\n",
            "secrets.py": "old()\n",
            "leak.py": 'password = "supersecret123"\nold()\n',
        }))
        ok, out, message = self.run_cli("refs", root, "old")
        self.assertTrue(ok, message)
        data = json.loads(out)
        in_scope = {item["path"]: item["lines"] for item in data["in_scope"]}
        self.assertEqual(in_scope, {"src/app.py": [1], "src/user.py": [1, 3]})
        self.assertEqual({item["path"]: item["count"] for item in data["excluded_matches"]},
                         {"data/x.py": 2, "tpl/page.jinja": 1})
        self.assertEqual({item["path"]: item["reason"] for item in data["not_reviewed"]},
                         {"secrets.py": "sensitive_name", "leak.py": "secret_content"})
        self.assertNotIn("supersecret123", out)

    def test_refs_word_boundaries(self):
        root = self.repo(self.files(**{"src/user.py": "old_degil = 1\nxold = 2\nold2 = 3\nx.old\n"}))
        data = json.loads(self.run_cli("refs", root, "old")[1])
        in_scope = {item["path"]: item["lines"] for item in data["in_scope"]}
        self.assertEqual(in_scope["src/user.py"], [4])

    def test_refs_symbol_must_exist_in_scope(self):  # scenario 12
        root = self.repo(self.files(**{"data/x.py": "sadece_burada()\n"}))
        for symbol in ("yok_boyle_bir_sey", "sadece_burada"):
            with self.subTest(symbol=symbol):
                ok, _, message = self.run_cli("refs", root, symbol)
                self.assertFalse(ok)
                self.assertIn("does not appear", message)

    def test_refs_symbol_format(self):  # scenario 12
        root = self.repo(self.files())
        for symbol in ("ab", "1abc", "a-b", "a b", "a.b", ""):
            with self.subTest(symbol=symbol):
                ok, _, message = self.run_cli("refs", root, symbol)
                self.assertFalse(ok)
                self.assertIn("The symbol", message)


class ReviewStatusTest(Flow):
    def status_of(self, root, candidates, *ids):
        plan = self.make_plan(root, candidates)
        ok, out, message = self.run_cli("review", root, plan, "--ids", *ids)
        self.assertTrue(ok, message)
        return plan, out

    def test_complete_when_no_reference_remains(self):
        root = self.repo({"src/app.py": APP})
        plan, out = self.status_of(root, [REMOVE_ESKI], "C1")
        self.assertIn("REVIEW_COMPLETE", out)
        ok, receipt, message = self.apply(root, plan, "C1", force=False)
        self.assertTrue(ok, message)

    def test_reference_in_excluded_file_is_incomplete(self):  # scenario 10
        root = self.repo({"src/app.py": APP, "data/x.py": "from app import old\nold()\n"})
        plan, out = self.status_of(root, [REMOVE_ESKI], "C1")
        self.assertIn("REVIEW_INCOMPLETE", out)
        self.assertIn("out-of-scope files", out)
        ok, receipt, message = self.apply(root, plan, "C1", force=False)
        self.assertFalse(ok)
        self.assertIn("cannot be applied", message)
        self.assertEqual(read(root, "src/app.py"), APP.encode())
        self.assertFalse((self.state / "receipts").exists())
        # There is no flag that overrides REVIEW_INCOMPLETE.
        digest = self.review(root, plan, "C1")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            temizlik.main(["apply", str(root), str(plan), "--ids", "C1", "--digest", digest, "--accept-incomplete"])

    def test_reference_in_template_file_is_incomplete(self):
        root = self.repo({"src/app.py": APP, "tpl/page.jinja": "{{ old() }}\n"})
        _, out = self.status_of(root, [REMOVE_ESKI], "C1")
        self.assertIn("REVIEW_INCOMPLETE", out)

    def test_reference_outside_deleted_range_in_scope_is_incomplete(self):
        root = self.repo({"src/app.py": APP, "src/user.py": "old()\n"})
        _, out = self.status_of(root, [REMOVE_ESKI], "C1")
        self.assertIn("outside the deleted range", out)

    def test_self_reference_inside_range_is_complete(self):
        source = "def old(n):\n    return old(n - 1)\n\ndef main():\n    return 2\n"
        root = self.repo({"src/app.py": source})
        _, out = self.status_of(root, [REMOVE_ESKI], "C1")
        self.assertIn("REVIEW_COMPLETE", out)

    def test_caller_and_callee_selected_together_is_complete(self):
        files = {"src/keep.py": "k = 1\n", "src/a.py": "def old():\n    return 1\n", "src/b.py": "from a import old\n\ndef yeni():\n    return old()\n"}
        root = self.repo(files)
        candidates = [
            {"id": "C1", "path": "src/a.py", "action": "delete_file", "symbols": ["old"]},
            {"id": "C2", "path": "src/b.py", "action": "delete_file", "symbols": ["yeni", "old"]},
        ]
        _, out = self.status_of(root, candidates, "C1", "C2")
        self.assertEqual(out.count("| REVIEW_COMPLETE"), 2)

    def test_unreviewable_source_makes_candidate_incomplete(self):  # scenario 11
        for name, content in (("secrets.py", "old()\n"), ("cfg.py", 'password = "supersecret123"\nold()\n')):
            with self.subTest(name=name):
                sub = self.tmp / ("project-" + name)
                sub.mkdir()
                root = make_repo(sub, {"src/app.py": APP, name: content})
                plan, out = self.status_of(root, [REMOVE_ESKI], "C1")
                self.assertIn("REVIEW_INCOMPLETE", out)
                self.assertIn("unreviewable", out)
                self.assertNotIn("supersecret123", out)
                ok, _, message = self.apply(root, plan, "C1", force=False)
                self.assertFalse(ok)

    def test_unreviewable_non_source_is_not_a_reason(self):
        root = self.repo({"src/app.py": APP, "image.png": b"\x89PNG\x00\x01"})
        _, out = self.status_of(root, [REMOVE_ESKI], "C1")
        self.assertIn("REVIEW_COMPLETE", out)

    def test_empty_symbols_for_code_is_incomplete(self):
        root = self.repo({"src/app.py": APP})
        candidate = {"id": "C1", "path": "src/app.py", "action": "remove_lines", "lines": [[1, 3]]}
        _, out = self.status_of(root, [candidate], "C1")
        self.assertIn("symbols is empty", out)

    def test_comment_only_python_and_markdown_need_no_symbols(self):
        root = self.repo({"src/app.py": "# old not\nx = 1\n", "README.md": "# Baslik\nold satir\n"})
        candidates = [
            {"id": "C1", "path": "src/app.py", "action": "remove_lines", "lines": [[1, 1]]},
            {"id": "C2", "path": "README.md", "action": "remove_lines", "lines": [[2, 2]]},
        ]
        _, out = self.status_of(root, candidates, "C1", "C2")
        self.assertEqual(out.count("| REVIEW_COMPLETE"), 2)

    def test_status_is_part_of_approval_digest(self):
        root = self.repo({"src/app.py": APP})
        plan = self.make_plan(root, [REMOVE_ESKI])
        first = self.review(root, plan, "C1")
        write(root, "data/x.py", "old()\n")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-q", "-m", "x")
        plan = self.make_plan(root, [REMOVE_ESKI])
        self.assertNotEqual(first, self.review(root, plan, "C1"))

    def test_invalid_symbols_field_refused(self):
        root = self.repo({"src/app.py": APP})
        for bad in ("old", [1], ["a b"], [""], [["old"]]):
            with self.subTest(bad=bad):
                candidate = dict(REMOVE_ESKI, symbols=bad)
                plan = self.make_plan(root, [candidate])
                ok, _, message = self.run_cli("review", root, plan, "--ids", "C1")
                self.assertFalse(ok)
                self.assertIn("symbols", message)


class SyntaxGuardTest(Flow):
    """Section 5a: syntax results."""

    def candidate(self, name, lines, symbols=("xyz",)):
        return {"id": "C1", "path": name, "action": "remove_lines", "lines": lines, "symbols": list(symbols)}

    def test_syntax_status_unit(self):
        self.assertEqual(temizlik.syntax_status("a.py", b"x = 1\n"), "PASSED")
        self.assertEqual(temizlik.syntax_status("a.py", b"def f():\n"), "ERROR")
        self.assertEqual(temizlik.syntax_status("a.json", b'{"a": 1}'), "PASSED")
        self.assertEqual(temizlik.syntax_status("a.json", b'{"a": 1,}'), "ERROR")
        self.assertEqual(temizlik.syntax_status("a.js", b"function ("), "UNVERIFIED")
        self.assertEqual(temizlik.syntax_status("a.rs", b"fn"), "UNVERIFIED")

    @unittest.skipUnless(sys.version_info >= (3, 11), "no tomllib (needs Python 3.11+); .toml returns UNVERIFIED")
    def test_toml_status(self):
        self.assertEqual(temizlik.syntax_status("a.toml", b"a = 1\n"), "PASSED")
        self.assertEqual(temizlik.syntax_status("a.toml", b"a = \n"), "ERROR")

    def test_toml_without_tomllib_is_unverified(self):
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            self.assertEqual(temizlik.syntax_status("a.toml", b"a = \n"), "UNVERIFIED")

    def test_empty_python_block_refused(self):  # scenario 13
        root = self.repo({"a.py": "def f():\n    x = 1\n\ny = 2\n"})
        plan = self.make_plan(root, [self.candidate("a.py", [[2, 2]])])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertFalse(ok)
        self.assertIn("syntax error", message)
        ok, receipt, message = self.apply(root, plan, "C1", digest="0" * 64)
        self.assertFalse(ok)
        self.assertEqual(read(root, "a.py"), b"def f():\n    x = 1\n\ny = 2\n")
        self.assertFalse((self.state / "receipts").exists())

    def test_valid_python_edit_reports_passed(self):
        root = self.repo({"a.py": "x = 1\ny = 2\n"})
        plan = self.make_plan(root, [self.candidate("a.py", [[2, 2]], ["why"])])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertTrue(ok, message)
        self.assertIn("Syntax | a.py | PASSED", out)
        self.assertIn("Limits of these results", out)

    def test_already_broken_python_not_blocked(self):
        root = self.repo({"a.py": "def f(:\nx = 1\ny = 2\n"})
        plan = self.make_plan(root, [self.candidate("a.py", [[3, 3]], ["why"])])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertTrue(ok, message)
        self.assertIn("Syntax | a.py | ERROR", out)

    def test_broken_json_refused_through_commit_pipeline(self):  # scenario 13 (.json cannot be edited; the pipeline is tested directly)
        root = self.repo({"c.json": '{"a": 1,\n "b": 2}\n'})
        ops = [("c.json", b'{"a": 1,\n "b": 2}\n', b'{"a": 1,\n')]
        with self.assertRaises(temizlik.Refusal):
            temizlik.prepare_commit(root, ops, "APPLY")
        ops = [("c.json", b'{"a": 1,\n "b": 2}\n', b'{"a": 1}\n')]
        prepared = temizlik.prepare_commit(root, ops, "APPLY")
        self.assertEqual(prepared["report"], [("c.json", "PASSED")])

    def test_javascript_reported_unverified(self):  # scenario 13
        root = self.repo({"a.js": "function f() {\n  return 1;\n}\nconst y = 2;\n"})
        plan = self.make_plan(root, [self.candidate("a.js", [[4, 4]], ["why"])])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertTrue(ok, message)
        self.assertIn("Syntax | a.js | UNVERIFIED", out)
        self.assertNotIn("a.js | PASSED", out)


class DirectiveGuardTest(Flow):
    """Section 5b: directive and docstring protection."""

    SAMPLES = [
        "#!/usr/bin/env python", "# -*- coding: utf-8 -*-", "x = 1  # type: ignore", "x = 1  # type:ignore",
        "import a  # noqa: F401", "# NOQA", "# pylint: disable=x", "# mypy: ignore-errors", "# pyright: basic",
        "# fmt: off", "# fmt: skip", "# isort: skip", "// eslint-disable-next-line", "// @ts-ignore",
        "// @ts-expect-error", "// @ts-nocheck", "// prettier-ignore", "/* istanbul ignore next */",
        "/* c8 ignore next */", "/* jshint ignore:line */", "// @flow", "/** @jsx h */", "//go:build linux",
        "// +build linux", "//nolint:errcheck", "#[allow(dead_code)]", "#![deny(warnings)]", "// NOSONAR",
        "// swiftlint:disable all", "# rubocop:disable Style", "#pragma once", "/* @license MIT */",
        "/* @preserve */", "// SPDX-License-Identifier: MIT", "<!-- [if IE]>",
    ]

    def test_every_directive_pattern_detected(self):  # scenario 14
        for sample in self.SAMPLES:
            with self.subTest(sample=sample):
                data = ("a = 1\n" + sample + "\nb = 2\n").encode()
                self.assertEqual([number for number, _ in temizlik.directive_violations(data, {2})], [2])

    def test_ordinary_lines_pass(self):
        data = b"a = 1\n# normal yorum\nb = 2\n"
        self.assertEqual(temizlik.directive_violations(data, {2}), [])

    def test_shebang_only_at_line_start(self):
        data = b"x = '#!' \n"
        self.assertEqual(temizlik.directive_violations(data, {1}), [])

    def test_shebang_removal_refused_end_to_end(self):  # scenario 14
        root = self.repo({"run.py": "#!/usr/bin/env python\nprint(1)\n"})
        plan = self.make_plan(root, [{"id": "C1", "path": "run.py", "action": "remove_lines", "lines": [[1, 1]]}])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertFalse(ok)
        self.assertIn("directive", message)
        self.assertNotIn("env python", message)

    def test_type_ignore_removal_refused(self):  # scenario 14
        root = self.repo({"a.py": "x = foo()  # type: ignore\ny = 1\n"})
        plan = self.make_plan(root, [{"id": "C1", "path": "a.py", "action": "remove_lines", "lines": [[1, 1]],
                                      "symbols": ["foo"]}])
        ok, _, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertFalse(ok)
        self.assertIn("directive", message)

    def test_deleting_whole_file_with_shebang_allowed(self):
        root = self.repo({"keep.py": "k = 1\n", "run.py": "#!/usr/bin/env python\nprint(1)\n"})
        plan = self.make_plan(root, [{"id": "C1", "path": "run.py", "action": "delete_file", "symbols": ["run"]}])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertTrue(ok, message)

    DOC = 'def f():\n    """Aciklama."""\n    return 1\n\ndef g():\n    return 2\n'

    def test_docstring_removal_refused(self):  # scenario 14
        root = self.repo({"a.py": self.DOC})
        plan = self.make_plan(root, [{"id": "C1", "path": "a.py", "action": "remove_lines", "lines": [[2, 2]],
                                      "symbols": ["fun"]}])
        ok, _, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertFalse(ok)
        self.assertIn("docstring", message)

    def test_module_and_class_docstring_removal_refused(self):
        for source, lines in (('"""Modul."""\nx = 1\n', [[1, 1]]),
                              ('class A:\n    """Sinif."""\n    x = 1\n', [[2, 2]]),
                              ('"""Cok\nsatirli."""\nx = 1\n', [[1, 2]])):
            with self.subTest(source=source):
                sub = self.tmp / ("p%d" % len(source))
                sub.mkdir()
                root = make_repo(sub, {"a.py": source})
                plan = self.make_plan(root, [{"id": "C1", "path": "a.py", "action": "remove_lines",
                                              "lines": lines, "symbols": ["xyz"]}])
                ok, _, message = self.run_cli("review", root, plan, "--ids", "C1")
                self.assertFalse(ok)
                self.assertIn("docstring", message)

    def test_removing_whole_definition_with_its_docstring_allowed(self):
        root = self.repo({"a.py": self.DOC})
        plan = self.make_plan(root, [{"id": "C1", "path": "a.py", "action": "remove_lines", "lines": [[1, 4]],
                                      "symbols": ["fun"]}])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertTrue(ok, message)
        self.assertIn("PASSED", out)

    def test_decorated_definition_removal_allowed(self):
        source = 'import functools\n\n@functools.cache\ndef f():\n    """Dok."""\n    return 1\n\ny = 2\n'
        root = self.repo({"a.py": source})
        plan = self.make_plan(root, [{"id": "C1", "path": "a.py", "action": "remove_lines", "lines": [[3, 7]],
                                      "symbols": ["fun"]}])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertTrue(ok, message)

    def test_fallback_diff_detects_directive_for_ops_without_spans(self):
        before = b"a = 1\n# noqa\nb = 2\n"
        after = b"a = 1\nb = 2\n"
        with self.assertRaises(temizlik.Refusal):
            temizlik.check_change("a.py", before, after, None)


class ReviewFixesTest(Flow):
    """Review fixes: symbol verification, lock, undo, coding pattern."""

    def status(self, root, candidate):
        plan = self.make_plan(root, [candidate])
        ok, out, message = self.run_cli("review", root, plan, "--ids", candidate["id"])
        self.assertTrue(ok, message)
        line = [line for line in out.splitlines() if line.startswith(candidate["id"] + " |")][0]
        return line

    def cand(self, symbols, lines=((1, 3),), path="src/app.py"):
        return {"id": "C1", "path": path, "action": "remove_lines", "lines": [list(x) for x in lines],
                "symbols": symbols}

    def test_typo_symbol_is_incomplete(self):  # geri bildirim 1
        root = self.repo({"src/app.py": APP})
        line = self.status(root, self.cand(["oldi"]))
        self.assertIn("REVIEW_INCOMPLETE", line)
        self.assertIn("oldi: does not appear in the deleted lines", line)
        self.assertIn("REVIEW_COMPLETE", self.status(root, self.cand(["old"])))

    def test_symbol_not_in_deleted_lines_is_incomplete(self):
        root = self.repo({"src/app.py": APP})
        self.assertIn("does not appear", self.status(root, self.cand(["old", "main"])))  # main is not deleted

    def test_defined_name_missing_from_symbols_is_incomplete(self):  # geri bildirim 1
        source = "def old():\n    return 1\n\nclass Eski:\n    pass\n\ndef main():\n    return 2\n"
        root = self.repo({"src/app.py": source})
        line = self.status(root, self.cand(["old"], [(1, 5)]))
        self.assertIn("REVIEW_INCOMPLETE", line)
        self.assertIn("Eski", line)
        self.assertIn("REVIEW_COMPLETE", self.status(root, self.cand(["old", "Eski"], [(1, 5)])))

    def test_assignment_and_import_names_are_required(self):
        root = self.repo({"src/app.py": "import collections.abc\nLIMIT = 3\nkeep = 1\n"})
        line = self.status(root, self.cand(["LIMIT"], [(1, 2)]))
        self.assertIn("REVIEW_INCOMPLETE", line)
        self.assertIn("collections", line)

    def test_short_defined_name_is_incomplete(self):
        root = self.repo({"src/app.py": "ab = 1\nkeep = 2\n"})
        line = self.status(root, self.cand(["keep"], [(1, 1)]))
        self.assertIn("short", line)

    def test_delete_file_requires_defined_names(self):
        root = self.repo({"keep.py": "k = 1\n", "old.py": "def old():\n    pass\n"})
        candidate = {"id": "C1", "path": "old.py", "action": "delete_file", "symbols": ["yanlis_ad"]}
        line = self.status(root, candidate)
        self.assertIn("REVIEW_INCOMPLETE", line)
        candidate["symbols"] = ["old"]
        self.assertIn("REVIEW_COMPLETE", self.status(root, candidate))

    def test_unsupported_language_code_deletion_is_never_complete(self):
        root = self.repo({"a.js": "function old() {}\nconst k = 1;\n"})
        line = self.status(root, self.cand(["old"], [(1, 1)], "a.js"))
        self.assertIn("REVIEW_INCOMPLETE", line)
        self.assertIn("cannot be verified", line)

    def test_comment_only_with_typo_symbol_is_incomplete(self):
        root = self.repo({"src/app.py": "# old not\nx = 1\n"})
        self.assertIn("does not appear", self.status(root, self.cand(["baska_ad"], [(1, 1)])))
        self.assertIn("REVIEW_COMPLETE", self.status(root, self.cand([], [(1, 1)])))

    def test_deleted_receipt_dir_does_not_lock_project(self):  # geri bildirim 2
        root = self.repo(THREE)
        plan = self.make_plan(root, THREE_PLAN)
        with self.failing_on(b"b/two.py"):
            ok, receipt, _ = self.apply(root, plan, "C1", "C2", "C3")
        self.assertFalse(ok)
        shutil.rmtree(self.receipt_dir(receipt))
        plan = self.make_plan(root, THREE_PLAN)
        ok, new_receipt, message = self.apply(root, plan, "C3")
        self.assertTrue(ok, message)
        # undo on the deleted receipt gives a clear error and does not lock.
        ok, _, message = self.run_cli("undo", root, receipt)
        self.assertFalse(ok)
        ok, _, message = self.run_cli("undo", root, new_receipt)
        self.assertTrue(ok, message)

    def test_missing_journal_prints_warning(self):
        root = self.repo(THREE)
        plan = self.make_plan(root, THREE_PLAN)
        receipt = self.apply_ok(root, plan, "C1")
        (self.receipt_dir(receipt) / "journal.json").unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            temizlik.ensure_unlocked(root)
        self.assertIn("not counted as a lock", out.getvalue())

    def test_corrupt_journal_still_refuses(self):
        root = self.repo(THREE)
        plan = self.make_plan(root, THREE_PLAN)
        receipt = self.apply_ok(root, plan, "C1")
        (self.receipt_dir(receipt) / "journal.json").write_bytes(b"{bozuk")
        with self.assertRaises(temizlik.Refusal):
            temizlik.ensure_unlocked(root)

    def test_undo_restores_original_that_does_not_parse(self):  # geri bildirim 3
        original = "print 'old sozdizimi'\nokay = 1\nbaska = 2\n"
        root = self.repo({"a.py": original})
        candidate = {"id": "C1", "path": "a.py", "action": "remove_lines", "lines": [[1, 1]], "symbols": []}
        plan = self.make_plan(root, [candidate])
        receipt = self.apply_ok(root, plan, "C1")
        self.assertEqual(temizlik.syntax_status("a.py", read(root, "a.py")), "PASSED")
        ok, out, message = self.run_cli("undo", root, receipt)
        self.assertTrue(ok, message)
        self.assertEqual(read(root, "a.py"), original.encode())
        self.assertIn("checks skipped", out)

    def test_recover_backward_skips_syntax_check(self):
        files = {"one.py": "print 'x'\nb = 1\n", "two.py": "print 'y'\nb = 2\n"}
        root = self.repo(files)
        plan = self.make_plan(root, [
            {"id": "C1", "path": "one.py", "action": "remove_lines", "lines": [[1, 1]]},
            {"id": "C2", "path": "two.py", "action": "remove_lines", "lines": [[1, 1]]}])
        with self.failing_on(b"b/two.py"):
            ok, receipt, _ = self.apply(root, plan, "C1", "C2")
        self.assertFalse(ok)
        ok, out, message = self.run_cli("recover", root, receipt, "--backward")
        self.assertTrue(ok, message)
        self.assertEqual(read(root, "one.py"), files["one.py"].encode())

    def test_apply_still_checks_syntax(self):
        root = self.repo({"a.py": "def f():\n    return 1\nz = 2\n"})
        plan = self.make_plan(root, [{"id": "C1", "path": "a.py", "action": "remove_lines", "lines": [[2, 2]]}])
        ok, _, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertFalse(ok)
        self.assertIn("syntax error", message)

    def test_coding_cookie_only_in_first_two_lines(self):  # geri bildirim 4
        common = b"x = 1\ny = 2\nwith open(p, encoding=\"utf-8\") as f:\n    pass\n"
        self.assertEqual(temizlik.directive_violations(common, {3}), [])
        self.assertEqual(temizlik.directive_violations(b"open(p, encoding='utf-8')\n", {1}), [])
        for first in (b"# coding: utf-8\n", b"# -*- coding=latin-1 -*-\n", b"#!/usr/bin/python\n"):
            self.assertTrue(temizlik.directive_violations(first + b"x = 1\n", {1}), first)
        self.assertTrue(temizlik.directive_violations(b"#!/x\n# vim: set fileencoding=utf-8 coding: utf-8\n", {2}))
        self.assertEqual(temizlik.directive_violations(b"a = 1\nb = 2\n# coding: utf-8\n", {3}), [])

    def test_no_invalid_escape_warnings(self):
        source = (Path(temizlik.__file__)).read_text(encoding="utf-8")
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            compile(source, "temizlik.py", "exec")


class BindingTest(Flow):
    """Names bound by deleted statements: definition detection and the 'cannot be sure' cases."""

    def status(self, root, symbols, lines, path="src/app.py"):
        candidate = {"id": "C1", "path": path, "action": "remove_lines",
                     "lines": [list(item) for item in lines], "symbols": symbols}
        plan = self.make_plan(root, [candidate])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertTrue(ok, message)
        return [line for line in out.splitlines() if line.startswith("C1 |")][0]

    BLOCKS = {
        "for": ("for item in range(3):\n    pass\n", ["item"]),
        "with": ("with open(p) as handle:\n    pass\n", ["handle"]),
        "except": ("try:\n    risky()\nexcept ValueError as err:\n    pass\n", ["err"]),
        "walrus": ("if (found := lookup()):\n    pass\n", ["found"]),
        "global": ("def setup():\n    global state\n    state = 1\n", ["setup", "state"]),
        "del": ("del stale\n", ["stale"]),
        "match": ("match command:\n    case [first, *others]:\n        pass\n    case {'k': 1, **rest}:\n        pass\n",
                  ["first", "others", "rest"]),
        "import_as": ("import collections.abc as abc_mod\nimport json\n", ["abc_mod", "json"]),
        "class_attr": ("class Box:\n    size = 1\n", ["Box"]),
        "comprehension_walrus": ("values = [last := n for n in data]\n", ["values", "last"]),
    }

    def test_every_binding_path_is_required_in_symbols(self):
        for label, (block, names) in self.BLOCKS.items():
            with self.subTest(label=label):
                sub = self.tmp / label
                sub.mkdir()
                root = make_repo(sub, {"src/app.py": block + "keep = 1\n"})
                count = len(block.splitlines())
                self.assertIn("REVIEW_INCOMPLETE", self.status(root, [], [(1, count)]))
                for name in names:
                    others = [other for other in names if other != name]
                    if others:  # a missing name: INCOMPLETE and the name appears in the reason
                        line = self.status(root, others, [(1, count)])
                        self.assertIn("REVIEW_INCOMPLETE", line)
                        self.assertIn(name, line)
                self.assertIn("REVIEW_COMPLETE", self.status(root, names, [(1, count)]))

    def test_name_used_later_is_incomplete_even_when_listed(self):
        root = self.repo({"src/app.py": "for item in range(3):\n    pass\nprint(item)\n"})
        line = self.status(root, ["item"], [(1, 2)])
        self.assertIn("REVIEW_INCOMPLETE", line)
        self.assertIn("outside the deleted range", line)

    def test_partially_removed_statement_is_descended_into(self):
        root = self.repo({"src/app.py": "for q in items:\n    total = 1\n    other = 2\n"})
        line = self.status(root, [], [(2, 2)])
        self.assertIn("total", line)
        self.assertNotIn("other", line)
        self.assertIn("REVIEW_COMPLETE", self.status(root, ["total"], [(2, 2)]))

    def test_attribute_assignment_is_unverifiable(self):
        source = "class A:\n    def go(self):\n        self.cache = {}\n        return 1\n"
        root = self.repo({"src/app.py": source})
        for symbols in ([], ["cache"], ["cache", "self"]):
            with self.subTest(symbols=symbols):
                line = self.status(root, symbols, [(3, 3)])
                self.assertIn("REVIEW_INCOMPLETE", line)
                self.assertIn("attribute or index assignment cannot be verified", line)
                self.assertNotIn("missing from symbols: self", line)

    def test_removing_whole_method_does_not_demand_attribute_names(self):
        source = "class A:\n    size = 1\n    def go(self):\n        self.cache = {}\n        return 1\n"
        root = self.repo({"src/app.py": source})
        self.assertIn("REVIEW_COMPLETE", self.status(root, ["go"], [(3, 5)]))

    def test_subscript_assignment_is_unverifiable(self):
        root = self.repo({"src/app.py": "d = {}\nd['k'] = 1\n"})
        line = self.status(root, [], [(2, 2)])
        self.assertIn("REVIEW_INCOMPLETE", line)
        self.assertIn("index assignment", line)
        self.assertNotIn("missing from symbols: d", line)

    def test_unrecognised_binding_forces_incomplete(self):
        root = self.repo({"src/app.py": "import json\nkeep = 1\n"})
        with mock.patch.object(temizlik, "defined_names", return_value=None):
            line = self.status(root, ["json"], [(1, 1)])
        self.assertIn("REVIEW_INCOMPLETE", line)
        self.assertIn("deleted bindings could not be fully verified", line)
        with mock.patch.object(temizlik, "defined_names", return_value=(set(), [temizlik.UNVERIFIED_BINDING])):
            line = self.status(root, ["json"], [(1, 1)])
        self.assertIn("deleted bindings could not be fully verified", line)

    def test_unknown_node_with_name_field_is_flagged(self):
        import ast as ast_module

        class Weird(ast_module.AST):
            _fields = ("name",)

        names, issues = set(), set()
        temizlik.collect_bindings(Weird(name="x"), names, issues)
        self.assertEqual(issues, {temizlik.UNVERIFIED_BINDING})

    def test_star_import_is_unverifiable(self):
        root = self.repo({"src/app.py": "from os.path import *\nkeep = 1\n"})
        line = self.status(root, ["path"], [(1, 1)])
        self.assertIn("deleted bindings could not be fully verified", line)

    @unittest.skipUnless(sys.version_info >= (3, 12), "needs Python 3.12 syntax (type statement, type parameters)")
    def test_type_alias_and_type_params(self):
        root = self.repo({"src/app.py": "type Alias = int\ndef ident[T](x: T) -> T:\n    return x\nkeep = 1\n"})
        self.assertIn("REVIEW_INCOMPLETE", self.status(root, [], [(1, 1)]))
        self.assertIn("REVIEW_COMPLETE", self.status(root, ["Alias"], [(1, 1)]))
        self.assertIn("REVIEW_COMPLETE", self.status(root, ["ident"], [(2, 3)]))

    def test_old_fabricated_symbols_are_now_incomplete(self):
        root = self.repo({"src/app.py": "def f():\n    return 1\n\nkeep = 1\n"})
        for symbol in ("why", "xyz", "fun"):
            with self.subTest(symbol=symbol):
                self.assertIn("REVIEW_INCOMPLETE", self.status(root, [symbol], [(1, 3)]))


class ShortNameTest(Flow):
    def status(self, root, symbols, lines, path="src/app.py"):
        candidate = {"id": "C1", "path": path, "action": "remove_lines",
                     "lines": [list(item) for item in lines], "symbols": symbols}
        plan = self.make_plan(root, [candidate])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertTrue(ok, message)
        return [line for line in out.splitlines() if line.startswith("C1 |")][0]

    def test_unused_short_import_is_complete(self):
        root = self.repo({"src/app.py": "import os\nkeep = 1\n"})
        self.assertIn("REVIEW_COMPLETE", self.status(root, ["os"], [(1, 1)]))

    def test_short_name_elsewhere_makes_it_incomplete(self):
        for label, files in (("code", {"src/app.py": "import os\nkeep = os\n"}),
                             ("comment", {"src/app.py": "import os\nkeep = 1  # os kullanilmiyor\n"}),
                             ("out_of_scope", {"src/app.py": "import os\nkeep = 1\n", "data/x.py": "# os\n"}),
                             ("text", {"src/app.py": "import os\nkeep = 1\n", "README.md": "os destekli\n"})):
            with self.subTest(label=label):
                sub = self.tmp / label
                sub.mkdir()
                root = make_repo(sub, files)
                line = self.status(root, ["os"], [(1, 1)])
                self.assertIn("REVIEW_INCOMPLETE", line)

    def test_short_name_must_appear_in_deleted_lines(self):
        root = self.repo({"src/app.py": "import os\nkeep = 1\n"})
        self.assertIn("ab: does not appear in the deleted lines", self.status(root, ["ab", "os"], [(1, 1)]))

    def test_unlisted_short_name_reason(self):
        root = self.repo({"src/app.py": "ab = 1\nkeep = 2\n"})
        line = self.status(root, [], [(1, 1)])
        self.assertIn("short names not listed in symbols, so they could not be searched: ab", line)
        self.assertIn("REVIEW_COMPLETE", self.status(root, ["ab"], [(1, 1)]))

    def test_refs_still_requires_three_characters(self):
        root = self.repo({"src/app.py": "import os\nkeep = os\n"})
        ok, _, message = self.run_cli("refs", root, "os")
        self.assertFalse(ok)
        self.assertIn("The symbol", message)

    def test_symbol_field_rejects_non_identifiers(self):
        root = self.repo({"src/app.py": "import os\nkeep = 1\n"})
        for bad in ("", "1a", "a b", "a-b", "a.b"):
            with self.subTest(bad=bad):
                candidate = {"id": "C1", "path": "src/app.py", "action": "remove_lines", "lines": [[1, 1]],
                             "symbols": [bad]}
                plan = self.make_plan(root, [candidate])
                ok, _, message = self.run_cli("review", root, plan, "--ids", "C1")
                self.assertFalse(ok)
                self.assertIn("symbols", message)


class StarImportAndParseTest(Flow):
    """Accepted rules: star import, unparsable file, non-Python languages."""

    def status(self, root, symbols, lines, path="src/app.py", action="remove_lines"):
        candidate = {"id": "C1", "path": path, "action": action, "symbols": symbols}
        if action == "remove_lines":
            candidate["lines"] = [list(item) for item in lines]
        plan = self.make_plan(root, [candidate])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertTrue(ok, message)
        return [line for line in out.splitlines() if line.startswith("C1 |")][0]

    SOURCE_TEXT = "def helper():\n    return 1\n\nkeep = 1\n"

    def test_baseline_without_star_import_is_complete(self):
        root = self.repo({"src/app.py": self.SOURCE_TEXT})
        self.assertIn("REVIEW_COMPLETE", self.status(root, ["helper"], [(1, 3)]))

    def test_star_import_in_same_file_is_incomplete(self):
        root = self.repo({"src/app.py": "from os.path import *\n" + self.SOURCE_TEXT})
        line = self.status(root, ["helper"], [(2, 4)])
        self.assertIn("REVIEW_INCOMPLETE", line)
        self.assertIn("because of from … import *", line)

    def test_star_import_of_the_module_elsewhere_is_incomplete(self):
        root = self.repo({"src/app.py": self.SOURCE_TEXT, "src/user.py": "from app import *\nz = 1\n"})
        line = self.status(root, ["helper"], [(1, 3)])
        self.assertIn("REVIEW_INCOMPLETE", line)
        self.assertIn("because of from … import *", line)

    def test_unrelated_star_import_does_not_affect(self):
        root = self.repo({"src/app.py": self.SOURCE_TEXT, "src/user.py": "from baska import *\nz = 1\n"})
        self.assertIn("REVIEW_COMPLETE", self.status(root, ["helper"], [(1, 3)]))

    def test_comment_only_deletion_is_not_affected_by_star_import(self):
        root = self.repo({"src/app.py": "from os.path import *\n# not\nkeep = 1\n"})
        self.assertIn("REVIEW_COMPLETE", self.status(root, [], [(2, 2)]))

    def test_unparsable_python_code_deletion_is_incomplete(self):
        root = self.repo({"src/app.py": "print 'old'\n# not\nkeep = 1\n"})
        line = self.status(root, [], [(1, 1)])
        self.assertIn("REVIEW_INCOMPLETE", line)
        self.assertIn("could not be parsed", line)
        line = self.status(root, ["old"], [(1, 1)])
        self.assertIn("could not be parsed", line)
        self.assertIn("REVIEW_INCOMPLETE", line)

    def test_unparsable_python_comment_only_deletion_is_not_code(self):
        root = self.repo({"src/app.py": "print 'old'\n# not\nkeep = 1\n"})
        self.assertIn("REVIEW_COMPLETE", self.status(root, [], [(2, 2)]))

    def test_non_python_code_deletion_is_incomplete(self):
        root = self.repo({"a.js": "function old() {}\nconst k = 1;\n"})
        line = self.status(root, ["old"], [(1, 1)], path="a.js")
        self.assertIn("REVIEW_INCOMPLETE", line)

    def test_incomplete_candidate_is_not_applied_without_flag(self):
        root = self.repo({"src/app.py": "from os.path import *\n" + self.SOURCE_TEXT})
        candidate = {"id": "C1", "path": "src/app.py", "action": "remove_lines", "lines": [[2, 4]],
                     "symbols": ["helper"]}
        plan = self.make_plan(root, [candidate])
        ok, receipt, message = self.apply(root, plan, "C1", force=False)
        self.assertFalse(ok)
        self.assertIn("cannot be applied", message)
        self.assertEqual(read(root, "src/app.py"), ("from os.path import *\n" + self.SOURCE_TEXT).encode())

    def test_limits_text_says_parse_is_not_proof(self):
        self.assertIn("can be parsed", temizlik.LIMITS_TEXT)
        self.assertIn("does not prove", temizlik.LIMITS_TEXT)


class ModuleAwareTest(Flow):
    """A name matched in another file only counts when that file can depend on the candidate module."""

    REPORT = "import shutil\nkeep = 1\n"

    def review_output(self, root, symbols=("shutil",), lines=((1, 1),), path="src/report.py"):
        candidate = {"id": "C1", "path": path, "action": "remove_lines",
                     "lines": [list(item) for item in lines], "symbols": list(symbols)}
        plan = self.make_plan(root, [candidate])
        ok, out, message = self.run_cli("review", root, plan, "--ids", "C1")
        self.assertTrue(ok, message)
        return out

    def status_line(self, out):
        return [line for line in out.splitlines() if line.startswith("C1 |")][0]

    def test_unrelated_python_file_with_its_own_binding_is_ignored(self):
        root = self.repo({"src/report.py": self.REPORT, "src/other.py": "import shutil\nshutil.copy(a, b)\n"})
        out = self.review_output(root)
        self.assertIn("REVIEW_COMPLETE", self.status_line(out))
        self.assertIn("Note | C1: shutil: ignored matches in 1 Python file(s)", out)
        self.assertIn("src/other.py", out)

    def test_backup_copy_in_blocked_directory_is_ignored_when_unrelated(self):
        root = self.repo({"src/report.py": self.REPORT,
                          "data/backup/report.py": "import shutil\nshutil.copy(a, b)\n"})
        self.assertIn("REVIEW_COMPLETE", self.status_line(self.review_output(root)))

    def test_file_that_mentions_the_module_stays_a_dependency(self):
        for label, content in (("import", "from report import shutil\n"),
                               ("attribute", "import report\nreport.shutil.copy(a, b)\n"),
                               ("string", "NAME = 'report'\nshutil = 1\n"),
                               ("comment", "# see report\nshutil = 1\n")):
            with self.subTest(label=label):
                sub = self.tmp / label
                sub.mkdir()
                root = make_repo(sub, {"src/report.py": self.REPORT, "src/other.py": content})
                self.assertIn("REVIEW_INCOMPLETE", self.status_line(self.review_output(root)))

    def test_symbol_in_comment_or_string_is_not_ignored(self):
        for label, content in (("comment", "# shutil is used here\nx = 1\n"),
                               ("string", "x = 'shutil'\n"),
                               ("mixed", "import shutil  # shutil\n")):
            with self.subTest(label=label):
                sub = self.tmp / label
                sub.mkdir()
                root = make_repo(sub, {"src/report.py": self.REPORT, "src/other.py": content})
                self.assertIn("REVIEW_INCOMPLETE", self.status_line(self.review_output(root)))

    def test_dynamic_import_machinery_blocks_ignoring(self):
        content = "import importlib\nimport shutil\nshutil.copy(a, b)\n"
        root = self.repo({"src/report.py": self.REPORT, "src/other.py": content})
        self.assertIn("REVIEW_INCOMPLETE", self.status_line(self.review_output(root)))

    def test_non_python_and_unparsable_files_are_not_ignored(self):
        for label, files in (("markdown", {"README.md": "shutil is great\n"}),
                             ("template", {"tpl/page.jinja": "{{ shutil }}\n"}),
                             ("unparsable", {"src/other.py": "print 'shutil'\nshutil = 1\n"})):
            with self.subTest(label=label):
                sub = self.tmp / label
                sub.mkdir()
                root = make_repo(sub, dict(files, **{"src/report.py": self.REPORT}))
                self.assertIn("REVIEW_INCOMPLETE", self.status_line(self.review_output(root)))

    def test_match_in_the_candidate_file_itself_stays_relevant(self):
        root = self.repo({"src/report.py": "import shutil\nshutil.copy(a, b)\n", "src/other.py": "x = 1\n"})
        self.assertIn("REVIEW_INCOMPLETE", self.status_line(self.review_output(root)))

    def test_ignored_matches_do_not_hide_a_real_dependency(self):
        files = {"src/report.py": self.REPORT, "src/other.py": "import shutil\nshutil.copy(a, b)\n",
                 "src/user.py": "from report import shutil\n"}
        root = self.repo(files)
        self.assertIn("REVIEW_INCOMPLETE", self.status_line(self.review_output(root)))

    def test_package_init_uses_the_package_name(self):
        root = self.repo({"pkg/__init__.py": self.REPORT, "src/other.py": "from pkg import thing\nshutil = 1\n"})
        out = self.review_output(root, path="pkg/__init__.py")
        self.assertIn("REVIEW_INCOMPLETE", self.status_line(out))


class DocsConsistencyTest(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def text(self, relative):
        return (self.ROOT / relative).read_text(encoding="utf-8")

    def test_readme_documents_every_command_and_flag(self):
        readme = self.text("README.md")
        for command in ("snapshot", "read", "refs", "review", "apply", "undo", "recover"):
            self.assertIn(f"`{command} ", readme)
        for flag in ("--forward", "--backward", "--digest", "COPCULER_STATE_DIR",
                     "REVIEW_COMPLETE", "REVIEW_INCOMPLETE", "UNVERIFIED", "python -m unittest discover -s tests -v"):
            self.assertIn(flag, readme)

    def test_agents_allows_only_read_only_commands(self):
        agents = self.text("AGENTS.md")
        self.assertIn("`snapshot`, `read`, `refs`, `review`", agents)
        for command in ("apply", "undo", "recover"):
            self.assertIn(f"`{command}`", agents)

    def test_manual_checklist_exists_and_has_no_observed_results(self):
        checklist = self.text("docs/manual-checks.md")
        for topic in ("OneDrive", "CRLF", "LFS", "autocrlf"):
            self.assertIn(topic, checklist)
        self.assertNotIn("RESULT: passed", checklist)

    def test_parser_accepts_documented_commands(self):
        for argv in (["refs", ".", "sembol"], ["recover", ".", "0" * 32, "--backward"], ["undo", ".", "0" * 32]):
            with self.subTest(argv=argv), mock.patch.object(temizlik, "project", side_effect=temizlik.Refusal("x")):
                with self.assertRaises(temizlik.Refusal):  # parsing passed, project validation stopped it
                    temizlik.main(argv)


if __name__ == "__main__":
    unittest.main()
