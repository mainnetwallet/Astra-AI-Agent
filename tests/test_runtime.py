"""Tests for the Astra Agent Runtime (astra/runtime/).

Two layers:

* **Unit** — engine argv/isolation contract, safe path resolution, archive
  extraction guards, package-install planning. No proot needed, fast.
* **Integration** — the real isolated runtime: lifecycle, PTY execution,
  isolation from the host, shared chat↔terminal session, package manager
  detection. These run against the actual proot rootfs and are skipped
  when the environment cannot provide one (there is no host fallback to
  fall back TO, so "unavailable" is a legitimate, testable state).
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.core.exceptions import ValidationError
from astra.runtime import files as rt_files
from astra.runtime import packages as rt_packages
from astra.runtime.engine import (GUEST_TMP, GUEST_WORKSPACE,
                                  AstraRuntimeUnavailable, RuntimeEngine)
from astra.runtime.manager import AgentRuntime, RuntimeManager


def _tmpdir() -> str:
    """Temp dir under $HOME — this environment's /tmp is not writable."""
    return tempfile.mkdtemp(prefix="astra_rt_test_",
                            dir=os.path.expanduser("~"))


def _has_runtime() -> bool:
    return RuntimeEngine().available()


needs_runtime = unittest.skipUnless(
    _has_runtime(), "Agent Runtime (proot + distro rootfs) unavailable")


# ── unit: engine ────────────────────────────────────────────────────────────

class TestRuntimeEngine(unittest.TestCase):
    def test_probe_reports_backend_and_reason(self):
        info = RuntimeEngine().probe()
        if info["available"]:
            self.assertEqual(info["backend"], "proot")
            self.assertTrue(os.path.isdir(info["rootfs"]))
            self.assertEqual(info["reason"], "")
        else:
            # Unavailable is a first-class state with a human-readable why.
            self.assertTrue(info["reason"])

    def test_require_raises_instead_of_falling_back(self):
        engine = RuntimeEngine(prefix="/nonexistent-prefix")
        with self.assertRaises(AstraRuntimeUnavailable):
            engine.require()

    def test_argv_binds_only_the_runtime_dirs(self):
        engine = RuntimeEngine()
        if not engine.available():
            self.skipTest("runtime unavailable")
        root = _tmpdir()
        ws = os.path.join(root, "workspace")
        home = os.path.join(root, "root")
        tmp = os.path.join(root, "tmp")
        argv = engine.build_argv(
            binds=[(ws, GUEST_WORKSPACE), (home, "/root"), (tmp, "/tmp")],
            cwd=GUEST_WORKSPACE)
        # Every host path bound in is one of the runtime's OWN directories;
        # the guest never gets the host home, /sdcard or the Termux prefix.
        binds = [a for a in argv if a.startswith("--bind=")]
        sources = [b[len("--bind="):].split(":")[0] for b in binds]
        allowed_roots = (os.path.abspath(root), engine.sysdata_dir(),
                         engine.shm_dir())
        for src in sources:
            if src in ("/dev", "/proc", "/sys"):
                continue
            self.assertTrue(
                any(src.startswith(r) for r in allowed_roots),
                msg=f"unexpected host bind source: {src}")
        # The GUEST side of every bind must be one of the small set of
        # locations the runtime is allowed to expose. Nothing maps a host
        # path to /data, /sdcard, /storage, /system or the guest root.
        guest_targets = set()
        for item in binds:
            spec = item[len("--bind="):]
            guest_targets.add(spec.split(":", 1)[1] if ":" in spec
                              else spec)
        allowed_targets = {GUEST_WORKSPACE, "/root", "/tmp", "/dev", "/proc",
                           "/sys", "/dev/shm", "/sys/fs/selinux"}
        self.assertTrue(guest_targets <= allowed_targets,
                        msg=f"unexpected guest bind target(s): "
                            f"{guest_targets - allowed_targets}")
        for required in (GUEST_WORKSPACE, "/root", "/tmp"):
            self.assertIn(required, guest_targets)
        self.assertIn(f"--bind={ws}:{GUEST_WORKSPACE}", argv)
        self.assertIn("--change-id=0:0", argv)
        self.assertIn("--rootfs=" + engine.base_rootfs(), argv)
        self.assertIn("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
                      "/sbin:/bin", argv)
        shutil.rmtree(root, ignore_errors=True)

    def test_argv_does_not_inherit_host_environment(self):
        engine = RuntimeEngine()
        if not engine.available():
            self.skipTest("runtime unavailable")
        os.environ["ASTRA_HOST_SECRET_XYZ"] = "leak"
        try:
            argv = engine.build_argv(binds=[(_tmpdir(), GUEST_WORKSPACE)],
                                     cwd=GUEST_WORKSPACE)
        finally:
            os.environ.pop("ASTRA_HOST_SECRET_XYZ", None)
        self.assertNotIn("ASTRA_HOST_SECRET_XYZ=leak", argv)


# ── unit: files ─────────────────────────────────────────────────────────────

class TestRuntimePaths(unittest.TestCase):
    def setUp(self):
        self.root = _tmpdir()
        self.paths = rt_files.RuntimePaths(
            os.path.join(self.root, "workspace"),
            os.path.join(self.root, "root"),
            os.path.join(self.root, "tmp"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_traversal_is_rejected(self):
        for bad in ("/workspace/../../etc/passwd", "../../etc/passwd",
                    "/etc/passwd", "/root/../..", "/workspace/a/../../../b"):
            with self.assertRaises(ValidationError, msg=bad):
                self.paths.resolve(bad)

    def test_workspace_containment(self):
        target = self.paths.resolve("/workspace/project/file.txt")
        self.assertTrue(target.startswith(self.paths.host[GUEST_WORKSPACE]))

    def test_symlink_escape_is_rejected(self):
        ws = self.paths.host[GUEST_WORKSPACE]
        os.makedirs(ws, exist_ok=True)
        outside = os.path.join(self.root, "outside")
        os.makedirs(outside, exist_ok=True)
        link = os.path.join(ws, "escape")
        os.symlink(outside, link)
        with self.assertRaises(ValidationError):
            self.paths.resolve("/workspace/escape/secret.txt")

    def test_protected_roots_cannot_be_removed(self):
        for root in (GUEST_WORKSPACE, "/root", "/tmp"):
            with self.assertRaises(ValidationError):
                rt_files.remove_path(self.paths, root, recursive=True)


class TestArchiveGuards(unittest.TestCase):
    def setUp(self):
        self.root = _tmpdir()
        self.paths = rt_files.RuntimePaths(
            os.path.join(self.root, "workspace"),
            os.path.join(self.root, "root"),
            os.path.join(self.root, "tmp"))
        self.ws = self.paths.host[GUEST_WORKSPACE]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _zip(self, name, entries):
        path = os.path.join(self.root, name)
        with zipfile.ZipFile(path, "w") as zf:
            for member, content in entries:
                zf.writestr(member, content)
        return path

    def test_zip_round_trip(self):
        src = self._zip("good.zip", [("proj/a.txt", "a"), ("proj/b/c.txt", "c")])
        dest = "/workspace/out"
        src_guest = f"{GUEST_TMP}/good.zip"
        shutil.copyfile(src, os.path.join(self.paths.host[GUEST_TMP],
                                          "good.zip"))
        result = rt_files.extract_archive(self.paths, src_guest, dest)
        self.assertEqual(result["count"], 2)
        self.assertTrue(os.path.isfile(
            os.path.join(self.ws, "out/proj/b/c.txt")))

    def test_zip_traversal_is_rejected(self):
        src = self._zip("evil.zip", [("../../pwned.txt", "x")])
        shutil.copyfile(src, os.path.join(self.paths.host[GUEST_TMP],
                                          "evil.zip"))
        with self.assertRaises(ValidationError):
            rt_files.extract_archive(self.paths, f"{GUEST_TMP}/evil.zip",
                                     "/workspace/out")
        self.assertFalse(os.path.exists(os.path.join(self.root, "pwned.txt")))

    def test_zip_absolute_member_is_rejected(self):
        src = self._zip("abs.zip", [("/tmp/pwned.txt", "x")])
        shutil.copyfile(src, os.path.join(self.paths.host[GUEST_TMP],
                                          "abs.zip"))
        with self.assertRaises(ValidationError):
            rt_files.extract_archive(self.paths, f"{GUEST_TMP}/abs.zip",
                                     "/workspace/out")

    def test_tar_gz_round_trip_and_escape(self):
        good = os.path.join(self.root, "good.tar.gz")
        with tarfile.open(good, "w:gz") as tf:
            inner = os.path.join(self.root, "hello.txt")
            with open(inner, "w") as fh:
                fh.write("hi")
            tf.add(inner, arcname="pkg/hello.txt")
        shutil.copyfile(good, os.path.join(self.paths.host[GUEST_TMP],
                                           "good.tar.gz"))
        result = rt_files.extract_archive(self.paths, f"{GUEST_TMP}/good.tar.gz",
                                          "/workspace/tarred")
        self.assertEqual(result["count"], 1)
        self.assertTrue(os.path.isfile(
            os.path.join(self.ws, "tarred/pkg/hello.txt")))

    def test_member_limit_is_enforced(self):
        paths = rt_files.RuntimePaths(
            os.path.join(self.root, "workspace2"),
            os.path.join(self.root, "root2"),
            os.path.join(self.root, "tmp2"), max_members=2)
        src = self._zip("many.zip", [(f"f{i}.txt", "x") for i in range(5)])
        shutil.copyfile(src, os.path.join(paths.host[GUEST_TMP], "many.zip"))
        with self.assertRaises(ValidationError):
            rt_files.extract_archive(paths, f"{GUEST_TMP}/many.zip",
                                     "/workspace/out")

    def test_binary_content_is_preserved(self):
        payload = bytes(range(256)) * 4
        src = os.path.join(self.root, "bin.zip")
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("blob.bin", payload)
        shutil.copyfile(src, os.path.join(self.paths.host[GUEST_TMP],
                                          "bin.zip"))
        rt_files.extract_archive(self.paths, f"{GUEST_TMP}/bin.zip",
                                 "/workspace/out")
        with open(os.path.join(self.ws, "out/blob.bin"), "rb") as fh:
            self.assertEqual(fh.read(), payload)


class TestPackagePlanning(unittest.TestCase):
    def test_npm_plan_and_verifier(self):
        plan = rt_packages.plan_install(ecosystem="npm", packages=["express"],
                                        managers=["npm"])
        self.assertTrue(plan["supported"])
        self.assertIn("npm install", plan["command"])
        self.assertIn("node_modules/express", plan["verifier"])

    def test_pip_plan_verifies_by_import(self):
        plan = rt_packages.plan_install(ecosystem="pip", packages=["requests"],
                                        managers=["python3", "pip3"])
        self.assertTrue(plan["supported"])
        self.assertIn("import requests", plan["verifier"])
        self.assertIn("python3 -c", plan["verifier"])
        self.assertNotIn("-m pip -c", plan["verifier"])

    def test_pip_bootstraps_from_apt_when_absent(self):
        # `managers` is the *package-manager* list the probe returns:
        # python3 present but no pip3 means pip must be bootstrapped.
        plan = rt_packages.plan_install(ecosystem="pip", packages=["six"],
                                        managers=["apt-get"])
        self.assertTrue(plan["supported"])
        self.assertIn("python3-pip", plan["bootstrap"])

    def test_missing_manager_is_reported_not_faked(self):
        plan = rt_packages.plan_install(ecosystem="apk", packages=["curl"],
                                        managers=["npm", "git"])
        self.assertFalse(plan["supported"])
        self.assertIn("apk", plan["reason"])

    def test_unknown_ecosystem_is_rejected(self):
        plan = rt_packages.plan_install(ecosystem="brew", packages=["x"],
                                        managers=["npm"])
        self.assertFalse(plan["supported"])

    def test_git_verifier_checks_worktree(self):
        plan = rt_packages.plan_install(ecosystem="git",
                                        packages=["https://x/y/demo.git"],
                                        managers=["git"])
        self.assertTrue(plan["supported"])
        self.assertIn(".git", plan["verifier"])
        self.assertIn("demo", plan["verifier"])


# ── integration: the real isolated runtime ──────────────────────────────────

@needs_runtime
class TestRuntimeLifecycle(unittest.TestCase):
    def setUp(self):
        self.base = _tmpdir()
        self.mgr = RuntimeManager(base_dir=self.base)
        self.rt = self.mgr.default()

    def tearDown(self):
        self.rt.close_all()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_create_start_stop_restart(self):
        status = self.rt.create()
        self.assertIn(status["state"], ("created", "stopped"))
        started = self.rt.start()
        self.assertEqual(started["state"], "running")
        self.assertTrue(started["available"])
        stopped = self.rt.stop()
        self.assertEqual(stopped["state"], "stopped")
        restarted = self.rt.restart()
        self.assertEqual(restarted["state"], "running")

    def test_reset_and_destroy(self):
        self.rt.start()
        self.rt.write_file("/workspace/keep.txt", "x")
        self.rt.reset()
        listing = self.rt.list_directory("/workspace")
        self.assertEqual(listing["entries"], [])
        destroyed = self.rt.destroy()
        self.assertTrue(destroyed["destroyed"])
        self.assertFalse(os.path.isdir(self.base + "/default"))

    def test_unavailable_runtime_refuses_to_start(self):
        mgr = RuntimeManager(engine=RuntimeEngine(prefix="/nope"),
                            base_dir=_tmpdir())
        with self.assertRaises(AstraRuntimeUnavailable):
            mgr.default().start()
        with self.assertRaises(AstraRuntimeUnavailable):
            mgr.default().exec_command("echo hi")

    def test_files_persist_across_sessions_but_not_process(self):
        self.rt.start()
        self.rt.write_file("/workspace/persist.txt", "kept")
        self.rt.stop()
        # A new runtime object over the same directory restores the data.
        again = AgentRuntime("default", self.rt.engine, self.base + "/default")
        again.create()
        self.assertEqual(again.read_file("/workspace/persist.txt")["text"],
                         "kept")


@needs_runtime
class TestRuntimeExecution(unittest.TestCase):
    def setUp(self):
        self.base = _tmpdir()
        self.mgr = RuntimeManager(base_dir=self.base)
        self.rt = self.mgr.default()
        self.rt.start()

    def tearDown(self):
        self.rt.close_all()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_command_runs_inside_runtime_with_persistent_cwd(self):
        first = self.rt.exec_command("mkdir -p proj && cd proj", session_id="t")
        self.assertEqual(first["exit_code"], 0)
        second = self.rt.exec_command("pwd", session_id="t")
        self.assertEqual(second["stdout"].strip(), "/workspace/proj")

    def test_exit_code_is_reported(self):
        good = self.rt.exec_command("true", session_id="t")
        self.assertEqual(good["exit_code"], 0)
        # A subshell, not `exit 3`: exiting the SHARED shell would end the
        # session under test rather than report a non-zero status.
        bad = self.rt.exec_command("(exit 3)", session_id="t")
        self.assertEqual(bad["exit_code"], 3)
        self.assertEqual(bad["status"], "failed")

    def test_host_filesystem_is_not_reachable(self):
        host_home = os.path.expanduser("~")
        name = f".astra_rt_probe_{os.getpid()}_{int(__import__('time').time())}"
        marker = os.path.join(host_home, name)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("host-only")
        try:
            # 1. The guest cannot read host bytes.
            read = self.rt.exec_command(f"cat {marker}", session_id="t")
            self.assertNotIn("host-only", read["stdout"])
            # 2. A guest write lands in the guest's own filesystem view
            #    (proot maps the path inside the rootfs) — the property that
            #    matters is that the HOST file is untouched.
            self.rt.exec_command(f"echo pwned > {marker}", session_id="t")
            with open(marker, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "host-only")
            # 3. The host home directory is not even listed.
            listing = self.rt.exec_command(
                f"ls {host_home} 2>&1 | head -3", session_id="t")
            self.assertNotIn("Astra-AI-Agent", listing["stdout"])
        finally:
            os.remove(marker)

    def test_host_binaries_are_not_on_the_guest_path(self):
        out = self.rt.exec_command("command -v termux-info || echo none",
                                   session_id="t")
        self.assertIn("none", out["stdout"])
        paths = self.rt.exec_command("echo $PATH", session_id="t")
        self.assertNotIn("/data/data/com.termux", paths["stdout"])
        # ...and no host tool can be executed by its absolute path.
        probe = self.rt.exec_command(
            "test -e /data/data/com.termux/files/usr/bin/git && echo PRESENT "
            "|| echo ABSENT", session_id="t")
        self.assertIn("ABSENT", probe["stdout"])
        run = self.rt.exec_command(
            "/data/data/com.termux/files/usr/bin/termux-info 2>&1 "
            "|| echo EXEC_FAILED", session_id="t")
        self.assertIn("EXEC_FAILED", run["stdout"])

    def test_resize_reaches_the_pty(self):
        process = self.rt.open_terminal("resize-test", rows=24, cols=80)
        self.assertTrue(process.resize(48, 132))
        out = self.rt.exec_command("tput cols; tput lines",
                                   session_id="resize-test")
        self.assertIn("132", out["stdout"])
        self.assertIn("48", out["stdout"])

    def test_chat_and_terminal_share_one_session(self):
        # The chat's runtime tool and the terminal panel both address
        # `conv-7`, so they must resolve to the same PTY process.
        self.rt.exec_command("mkdir -p shared && cd shared",
                             session_id="conv-7")
        process = self.rt.open_terminal("conv-7")
        self.assertIs(process, self.rt.get_terminal("conv-7"))
        offset = process.total_bytes()
        process.write(b"echo USER_SIDE > user_note.txt\n")
        for _ in range(40):
            if "USER_SIDE" in process.text_since(offset)["data"]:
                break
            import time
            time.sleep(0.05)
        seen = self.rt.exec_command("cat user_note.txt", session_id="conv-7")
        self.assertIn("USER_SIDE", seen["stdout"])

    def test_capability_detection_is_real(self):
        caps = self.rt.capabilities(refresh=True)
        self.assertTrue(caps["available"])
        # The plan requires python3/node/npm/git to actually exist — these
        # assertions are about the real probe, not a hardcoded list.
        for tool in ("python3", "node", "npm", "git"):
            self.assertIn(tool, caps["tools"], msg=tool)

    def test_package_manager_detection_lists_apt_inside_rootfs(self):
        self.assertIn("apt-get", self.rt.package_managers(refresh=True))

    def test_npm_install_is_verified(self):
        caps = self.rt.capabilities(refresh=True)
        if "npm" not in caps.get("managers", []):
            self.skipTest("npm unavailable in the runtime rootfs")
        self.rt.exec_command("mkdir -p app && cd app && npm init -y >/dev/null",
                             session_id="t")
        result = self.rt.package_install(
            ecosystem="npm", packages=["is-odd"], cwd="/workspace/app",
            timeout=600)
        if not result.get("installed"):
            self.skipTest("no network for npm inside the runtime")
        self.assertTrue(result["verified"], result.get("error"))
        self.assertTrue(os.path.isdir(
            os.path.join(self.rt.workspace_dir, "app/node_modules/is-odd")))

    def test_events_are_emitted(self):
        class Bus:
            def __init__(self):
                self.kinds = []

            def emit(self, kind, **data):
                self.kinds.append(kind)

        bus = Bus()
        base = _tmpdir()
        rt = RuntimeManager(events=bus, base_dir=base).default()
        rt.start()
        rt.exec_command("echo hi", session_id="e")
        rt.stop()
        for expected in ("runtime.started", "terminal.started",
                         "terminal.completed", "runtime.stopped"):
            self.assertIn(expected, bus.kinds, msg=expected)
        rt.close_all()
        shutil.rmtree(base, ignore_errors=True)


# ── unit: web layer contract ────────────────────────────────────────────────

class TestTerminalWebContract(unittest.TestCase):
    """The terminal's server-side contract: it is advertised as a tab, it
    decodes keystrokes into real PTY bytes, and its session field survives
    the global secret-redaction rule."""

    def test_terminal_is_advertised_as_a_core_tab(self):
        from astra.web import CORE_TABS, AstraSite
        tabs = [t["tab"] for t in CORE_TABS]
        self.assertIn("terminal", tabs)
        manifest = AstraSite.__dict__.get("manifest")
        self.assertIsNotNone(manifest)

    def test_key_encoding_matches_a_real_terminal(self):
        from astra.web import _key_to_bytes
        self.assertEqual(_key_to_bytes({"key": "ArrowUp"}), b"\x1b[A")
        self.assertEqual(_key_to_bytes({"key": "ArrowDown"}), b"\x1b[B")
        self.assertEqual(_key_to_bytes({"key": "Tab"}), b"\t")
        self.assertEqual(_key_to_bytes({"key": "Backspace"}), b"\x7f")
        self.assertEqual(_key_to_bytes({"key": "Delete"}), b"\x1b[3~")
        self.assertEqual(_key_to_bytes({"key": "Home"}), b"\x1b[H")
        self.assertEqual(_key_to_bytes({"key": "PageUp"}), b"\x1b[5~")
        # Ctrl+C is the raw 0x03 byte the line discipline turns into SIGINT.
        self.assertEqual(_key_to_bytes({"key": "c", "ctrl": True}), b"\x03")
        self.assertEqual(_key_to_bytes({"key": "d", "ctrl": True}), b"\x04")
        self.assertEqual(_key_to_bytes({"key": "l", "ctrl": True}), b"\x0c")
        self.assertEqual(_key_to_bytes({"key": "a", "ctrl": True}), b"\x01")
        self.assertEqual(_key_to_bytes({"key": "e", "ctrl": True}), b"\x05")
        self.assertEqual(_key_to_bytes({"key": "w", "ctrl": True}), b"\x17")
        self.assertEqual(_key_to_bytes({"key": "r", "ctrl": True}), b"\x12")
        self.assertEqual(_key_to_bytes({"key": "z", "ctrl": True}), b"\x1a")
        self.assertEqual(_key_to_bytes({"key": "A"}), b"A")
        self.assertEqual(_key_to_bytes({"text": "npm install "}),
                         b"npm install ")
        self.assertEqual(_key_to_bytes({"key": "Meta"}), b"")

    def test_session_alias_survives_redaction(self):
        from astra.security import redact
        from astra.web import _session_alias
        payload = _session_alias({"session_id": "conv-3", "status": "running"})
        self.assertEqual(payload["session"], "conv-3")
        # The mirror is a non-secret key, so it survives the API's redaction.
        self.assertEqual(redact(payload)["session"], "conv-3")

    def test_runtime_routes_are_registered(self):
        import inspect

        from astra.web import WebApp
        source = inspect.getsource(WebApp._route)
        for route in ('["api", "runtime", "status"]',
                      '["api", "runtime", "terminal", "stream"]',
                      '["api", "runtime", "terminal", "input"]',
                      '["api", "runtime", "terminal", "resize"]',
                      '["api", "runtime", "upload"]'):
            self.assertIn(route, source, msg=route)
        # Every runtime endpoint resolves through the one guard that reports
        # unavailability — there is no direct host path.
        self.assertIn("def _runtime(self, req)", inspect.getsource(WebApp))


if __name__ == "__main__":
    unittest.main()
