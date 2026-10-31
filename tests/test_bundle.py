"""Tests for make-bundle.sh and the self-extracting installer it produces.

Nothing here installs anything: the installer is only run with --dry-run, --show-info and
--extract, all of which leave the system untouched.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAKE = os.path.join(ROOT, "make-bundle.sh")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kw)


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def write_bytes(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


@unittest.skipUnless(shutil.which("bash") and shutil.which("tar") and shutil.which("base64"),
                     "needs bash, tar and base64")
class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ka-bundle-")
        cls.out = os.path.join(cls.tmp, "out")
        r = run(["bash", MAKE, "--allow-dirty", "--out", cls.out])
        cls.build = r
        cls.bundle = os.path.join(cls.out, "oracle-keepalive-installer.sh")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_build_succeeds(self):
        self.assertEqual(self.build.returncode, 0, self.build.stderr)
        self.assertTrue(os.path.isfile(self.bundle))
        self.assertIn("sha256", self.build.stdout)

    def test_syntax_ok(self):
        self.assertEqual(run(["bash", "-n", self.bundle]).returncode, 0)
        self.assertEqual(run(["bash", "-n", MAKE]).returncode, 0)

    def test_show_info_matches_build(self):
        r = run(["bash", self.bundle, "--show-info"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("version : 1.0.0", r.stdout)
        sha = hashlib.sha256(read_bytes(self.bundle)).hexdigest()
        self.assertIn(sha, r.stdout)

    def test_build_is_deterministic(self):
        other = os.path.join(self.tmp, "out2")
        r = run(["bash", MAKE, "--allow-dirty", "--out", other])
        self.assertEqual(r.returncode, 0, r.stderr)
        a = hashlib.sha256(read_bytes(self.bundle)).hexdigest()
        b = hashlib.sha256(read_bytes(os.path.join(other, "oracle-keepalive-installer.sh"))).hexdigest()
        self.assertEqual(a, b)

    def test_extract_contains_all_files_and_matches_repo(self):
        dest = os.path.join(self.tmp, "x")
        r = run(["bash", self.bundle, "--extract", dest])
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("oracle-keepalive.py", "oracle-keepalive.service", "oracle-keepalive.conf.example",
                     "install.sh", "LICENSE", "README.md"):
            got = read_bytes(os.path.join(dest, name))
            want = read_bytes(os.path.join(ROOT, name))
            self.assertEqual(got, want, name)

    @unittest.skipUnless(shutil.which("systemctl"), "install.sh --dry-run checks for systemd")
    def test_dry_run_changes_nothing(self):
        r = run(["bash", self.bundle, "--dry-run"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("checksum verified", r.stdout)
        self.assertIn("dry run complete", r.stdout)
        for path in ("/usr/local/lib/oracle-keepalive", "/etc/systemd/system/oracle-keepalive.service",
                     "/etc/oracle-keepalive.conf", "/var/lib/oracle-keepalive"):
            self.assertFalse(os.path.exists(path), path)

    def test_tampered_payload_is_rejected(self):
        data = bytearray(read_bytes(self.bundle))
        marker = data.index(b"\n__PAYLOAD_BELOW__\n") + len(b"\n__PAYLOAD_BELOW__\n")
        # flip one base64 character in the middle of the payload to a different valid one
        pos = marker + 400
        data[pos] = ord("A") if data[pos] != ord("A") else ord("B")
        bad = os.path.join(self.tmp, "bad.sh")
        write_bytes(bad, bytes(data))
        r = run(["bash", bad, "--dry-run"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("checksum mismatch", r.stderr)
        self.assertNotIn("dry run complete", r.stdout)

    def test_truncated_file_is_rejected(self):
        data = read_bytes(self.bundle)
        bad = os.path.join(self.tmp, "short.sh")
        write_bytes(bad, data[: len(data) // 2])
        r = run(["bash", bad, "--dry-run"])
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue("damaged" in r.stderr or "mismatch" in r.stderr, r.stderr)

    def test_piped_execution_gives_a_clear_error(self):
        p = subprocess.run("cat '%s' | bash -s -- --dry-run" % self.bundle, shell=True,
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0)

    def test_dirty_tree_is_refused_without_flag(self):
        if not shutil.which("git") or not os.path.isdir(os.path.join(ROOT, ".git")):
            self.skipTest("not a git checkout")
        probe = os.path.join(self.tmp, "probe")
        shutil.copytree(ROOT, probe, ignore=shutil.ignore_patterns("__pycache__", "dist"))
        with open(os.path.join(probe, "LICENSE"), "a") as fh:
            fh.write("\nlocal edit\n")
        r = run(["bash", os.path.join(probe, "make-bundle.sh"), "--out", os.path.join(self.tmp, "o3")])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("differ from HEAD", r.stderr)


if __name__ == "__main__":
    unittest.main()
