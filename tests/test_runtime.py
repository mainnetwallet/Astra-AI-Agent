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
from astra.runtime.tools import register_runtime_tools


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
        engine = RuntimeEngine()
        info = engine.probe()
        # The backend is chosen FOR THE HOST and `proot` is not hardcoded as
        # the only one: Android/Termux -> proot, Windows -> wsl2 (WSL2
        # Ubuntu). Neither is ever the host shell.
        self.assertIn(info["backend"], ("proot", "wsl2"))
        self.assertEqual(info["platform"], engine.platform)
        self.assertEqual(info["workspace"], GUEST_WORKSPACE)
        self.assertNotIn("cmd", info["backend"].lower())
        self.assertNotIn("powershell", info["backend"].lower())
        if info["available"]:
            self.assertTrue(os.path.isdir(info["rootfs"]))
            self.assertEqual(info["reason"], "")
            self.assertTrue(info["host_isolation"])
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
        if engine.name != "proot":
            self._assert_wsl_argv_is_isolated(engine)
            return
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
        # The guest PATH is the system path plus this runtime's private
        # user-scope install targets (/root is bound per-runtime), and it
        # never contains the host Termux prefix.
        path_arg = next(a for a in argv if a.startswith("PATH="))
        self.assertTrue(
            path_arg.startswith("PATH=/usr/local/sbin:/usr/local/bin:"
                                "/usr/sbin:/usr/bin:/sbin:/bin"),
            path_arg)
        self.assertIn("/root/.local/bin", path_arg)
        self.assertNotIn("/data/data/com.termux", path_arg)
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

    def test_user_scope_env_keeps_package_state_per_runtime(self):
        """PIP_USER / NPM_CONFIG_PREFIX / CARGO_HOME … all resolve under the
        guest HOME, which is bound to THIS runtime's own `<runtime>/root`
        directory — so a user-scope install lands in private state even
        though the distro rootfs itself is shared (spec §15)."""
        from astra.runtime.engine import GUEST_HOME, _user_scope_env
        env = _user_scope_env()
        # PIP_USER is deliberately NOT exported: pip REFUSES a `--user`
        # install inside a virtualenv, which would break `python3 -m venv`
        # + `pip install` (spec 7). The runtime's own installer passes
        # `--user` explicitly instead, so privacy is unchanged but venvs
        # keep working.
        self.assertNotIn("PIP_USER", env)
        plan = rt_packages.plan_install(ecosystem="pip",
                                        packages=["requests"],
                                        managers=["python3", "pip3"])
        self.assertIn("--user", plan["command"])
        for key in ("PYTHONUSERBASE", "PIP_CACHE_DIR", "NPM_CONFIG_PREFIX",
                    "NPM_CONFIG_CACHE", "NODE_PATH", "CARGO_HOME", "GOPATH",
                    "GEM_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                    "XDG_CACHE_HOME"):
            self.assertTrue(
                env[key].startswith(GUEST_HOME + "/"),
                msg=f"{key} must stay inside the runtime home: {env[key]}")
        engine = RuntimeEngine()
        if not engine.available():
            self.skipTest("runtime unavailable")
        root = _tmpdir()
        try:
            argv = engine.build_argv(binds=[(root, GUEST_WORKSPACE)],
                                     cwd=GUEST_WORKSPACE)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        # The private package env is on the real child environment…
        self.assertNotIn("PIP_USER=1", argv)
        self.assertIn(f"NPM_CONFIG_PREFIX={GUEST_HOME}/.npm-global", argv)
        self.assertIn(f"PYTHONUSERBASE={GUEST_HOME}/.local", argv)
        # …and the private user-scope bin dirs are on the guest PATH.
        path_arg = next(a for a in argv if a.startswith("PATH="))
        self.assertIn(f"{GUEST_HOME}/.local/bin", path_arg)
        self.assertIn(f"{GUEST_HOME}/.npm-global/bin", path_arg)

    def _assert_wsl_argv_is_isolated(self, engine):
        """The Windows equivalent of the proot bind contract.

        proot answers "which host paths are bound in?" with `--bind=`; the
        WSL2 backend answers it with the isolation bootstrap it runs first:
        a private mount namespace, an environment built from scratch, and
        exactly the runtime's own tree mounted onto /workspace, /root, /tmp.
        """
        base = _tmpdir()
        try:
            ws, home, tmp = engine.host_dirs("default", base)
            guest_ws, guest_home, guest_tmp = engine.guest_source_dirs(
                "default", base)
            argv = engine.build_argv(
                binds=[(ws, GUEST_WORKSPACE), (home, "/root"),
                       (tmp, "/tmp")],
                cwd=GUEST_WORKSPACE)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        # wsl.exe is only the transport: the command runs as root, exec'd
        # directly (`-e`, so no extra shell pass rewrites the arguments).
        self.assertIn("-d", argv)
        self.assertIn(engine.container, argv)
        self.assertIn("-e", argv)
        # ...inside a PRIVATE mount namespace, which is what unmounts
        # C:\, /mnt/c and every other Windows-provided mount.
        self.assertTrue(any(str(a).endswith("unshare") for a in argv), argv)
        self.assertIn("-m", argv)
        self.assertIn("--propagation", argv)
        self.assertIn("private", argv)
        # The environment is constructed, never inherited (`env -i`).
        self.assertIn("/usr/bin/env", argv)
        self.assertIn("-i", argv)
        # The runtime's OWN tree becomes /workspace, /root and /tmp.
        for path in (guest_ws, guest_home, guest_tmp):
            self.assertIn(path, argv)
        path_arg = next(a for a in argv if a.startswith("PATH="))
        self.assertNotIn("/mnt/", path_arg)
        self.assertNotIn("com.termux", path_arg)
        # Nothing Windows-shaped reaches the guest: no host home, no C:\
        # path, no Windows binary, no host environment variable. argv[0] is
        # wsl.exe itself - Astra's own transport, never passed TO the guest.
        blob = "\n".join(str(a) for a in argv[1:])
        self.assertNotIn(os.path.expanduser("~"), blob)
        self.assertNotIn("cmd.exe", blob)
        self.assertNotIn("powershell", blob.lower())
        self.assertNotIn("ASTRA_HOST_SECRET_XYZ", blob)
        # ...and no individual argument is a Windows path or a Windows
        # executable. (The bootstrap script itself is one argv element and
        # mentions C:\ only in the comment that explains what it unmounts.)
        for arg in argv[1:]:
            text = str(arg)
            self.assertNotIn(os.path.expanduser("~"), text)
            self.assertNotIn("System32", text)
            self.assertFalse(text.endswith(".exe"), text)
            self.assertFalse(text.startswith("/mnt/"), text)


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


@needs_runtime
class TestPerRuntimeIsolation(unittest.TestCase):
    """Spec §15: every runtime owns private writable state. Runtime A's
    files and user-scope packages are invisible to runtime B, while a
    restart of A preserves them."""

    def setUp(self):
        self.base = _tmpdir()
        self.mgr = RuntimeManager(base_dir=self.base)
        self.a = self.mgr.get("A")
        self.b = self.mgr.get("B")
        self.a.start()
        self.b.start()

    def tearDown(self):
        self.mgr.close_all()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_workspaces_and_writes_are_private(self):
        self.a.write_file("/workspace/proj/marker_a.txt", "A")
        own = self.a.exec_command("cat /workspace/proj/marker_a.txt",
                                  session_id="a")
        self.assertIn("A", own["stdout"])
        seen = self.b.exec_command(
            "test -e /workspace/proj/marker_a.txt && echo VISIBLE || echo ABSENT",
            session_id="b")
        self.assertIn("ABSENT", seen["stdout"])
        self.b.exec_command(
            "mkdir -p /workspace/proj && echo B > /workspace/proj/marker_b.txt",
            session_id="b")
        back = self.a.exec_command(
            "test -e /workspace/proj/marker_b.txt && echo LEAK || echo NO_LEAK",
            session_id="a")
        self.assertIn("NO_LEAK", back["stdout"])

    def test_user_scope_install_is_private_and_survives_restart(self):
        managers = self.a.package_managers(refresh=True)
        if not any(m in managers for m in ("pip", "pip3", "python3")):
            self.skipTest("pip unavailable in the runtime rootfs")
        result = self.a.package_install(ecosystem="pip", packages=["cowsay"])
        if not result.get("installed"):
            self.skipTest("no network for pip inside the runtime")
        self.assertTrue(result["verified"], result.get("error"))
        # It landed in A's PRIVATE home, not the shared distro site-packages.
        where = self.a.exec_command(
            "python3 -c 'import cowsay; print(cowsay.__file__)'",
            session_id="p")
        self.assertIn("/root/.local", where["stdout"])
        # A restart of A keeps it…
        self.a.restart()
        again = self.a.exec_command(
            "python3 -c 'import cowsay; print(\"SURVIVED\")'", session_id="p")
        self.assertIn("SURVIVED", again["stdout"])
        # …and B, a different runtime, does not see it.
        other = self.b.exec_command(
            "python3 -c 'import cowsay' 2>&1 | tail -1", session_id="p")
        self.assertNotIn("SURVIVED", other["stdout"])
        self.assertIn("ModuleNotFoundError", other["stdout"])


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


class TestGatewayCapabilityAwareness(unittest.TestCase):
    """The Gateway decides *what a request needs* from the live capability
    catalog derived from the ONE ToolRegistry. Registering the runtime tools
    must therefore make `runtime` a first-class `execution.capability` — with
    no separate, hardcoded capability list to keep in sync."""

    def _registry(self):
        from astra.core.permissions import Policy
        from astra.tools.registry import ToolRegistry
        reg = ToolRegistry(policy=Policy(granted=["read"]))
        register_runtime_tools(reg, RuntimeManager(base_dir=_tmpdir()))
        return reg

    def test_runtime_category_reaches_the_gateway_catalog(self):
        from astra.ai.capability_context import collect_runtime_capabilities
        caps = collect_runtime_capabilities(self._registry())
        self.assertTrue(caps.has("runtime"))
        self.assertIn("runtime", caps.catalog_text())
        self.assertIn("Agent Runtime", caps.human_context)

    def test_catalog_is_empty_without_tools(self):
        from astra.ai.capability_context import collect_runtime_capabilities
        caps = collect_runtime_capabilities(None)
        self.assertFalse(caps.available)

    def test_runtime_tools_are_risked_correctly(self):
        from astra.core.permissions import Level
        reg = self._registry()
        self.assertEqual(reg.get("runtime_command").risk, Level.SYSTEM_ACTION)
        self.assertEqual(reg.get("runtime_start").risk, Level.SYSTEM_ACTION)
        self.assertEqual(reg.get("runtime_directory_list").risk, Level.READ)
        self.assertEqual(reg.get("runtime_file_write").risk,
                         Level.LOW_RISK_WRITE)

    def test_policy_denies_runtime_execution_when_not_granted(self):
        from astra.core.exceptions import PermissionError as ToolPermissionError
        reg = self._registry()          # granted: read only
        with self.assertRaises(ToolPermissionError):
            reg.execute("runtime_command", {"command": "echo hi"})
        # ...and a read-only runtime tool is still allowed.
        result = reg.execute("runtime_status", {})
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()


class PromptTests(unittest.TestCase):
    def test_prompt_is_astra_not_root_at_localhost(self):
        """The distro .bashrc overwrites a plain PS1, so the prompt must also
        be re-applied via a self-removing PROMPT_COMMAND."""
        from astra.runtime.engine import ASTRA_PS1, _prompt_env
        env = _prompt_env()
        self.assertIn("astra", ASTRA_PS1)
        self.assertNotIn("\\h", ASTRA_PS1)          # no hostname (=localhost)
        self.assertEqual(env["PS1"], ASTRA_PS1)
        self.assertIn("unset PROMPT_COMMAND", env["PROMPT_COMMAND"])
        self.assertNotIn("'", ASTRA_PS1)              # safe inside PS1='...'
