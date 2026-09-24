"""Backend coverage for the Astra Agent Runtime (astra/runtime/backends/).

The runtime is ONE implementation with two backends:

    Android/Termux  ->  ProotRuntimeBackend   (proot + proot-distro)
    Windows         ->  WslRuntimeBackend     (WSL2 + Ubuntu)

Everything that is not discovery/bootstrap/layout is shared and is covered
by `tests/test_runtime.py`. What is genuinely backend-specific is covered
here, and the WSL side is driven ENTIRELY through `WslRunner` - the single
narrow adapter over `wsl.exe` - so this file is a real test with no WSL, no
Windows and no distribution present (spec 20: "Do NOT require real WSL in
CI. Use a narrow backend adapter seam for unit tests.").

A machine that really has WSL2 + Ubuntu additionally runs the integration
layer in `tests/test_runtime.py` and `scripts/runtime_acceptance.py`.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from astra.runtime.backends import (BACKEND_PROOT, BACKEND_WSL2,
                                    InvalidBackend, ProotRuntimeBackend,
                                    WslRuntimeBackend, detect_platform,
                                    select_backend)
from astra.runtime.backends import wsl as wsl_mod
from astra.runtime.backends.base import (GUEST_HOME, GUEST_PATH, GUEST_SHELL,
                                         GUEST_TMP, GUEST_WORKSPACE,
                                         AstraRuntimeUnavailable, guest_env)
from astra.runtime.engine import RuntimeEngine
from astra.runtime.manager import AgentRuntime
from astra.runtime.pty import PtyProcess
from astra.runtime.pty_wsl import WslPtyProcess


class _Cfg:
    """Minimal config shim (backends only need `.get()`)."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)


def _tmpdir() -> str:
    """Temp dir under $HOME - this environment's /tmp is not writable."""
    return tempfile.mkdtemp(prefix="astra_backend_test_",
                            dir=os.path.expanduser("~"))


# A healthy Ubuntu: this is what the in-guest probe scripts print.
HEALTHY_TOOLS = """ASTRA_TOOLS=1
os=Ubuntu 24.04.3 LTS
uid=0
unshare=/usr/bin/unshare
mount=/usr/bin/mount
umount=/usr/bin/umount
awk=/usr/bin/awk
bash=/usr/bin/bash
python3=/usr/bin/python3
timeout=/usr/bin/timeout
"""

HEALTHY_SESSION = """ASTRA_WSL_CHECK=1
whoami=root
uid=0
cwd=/var/lib/astra/runtime/default/workspace
mnt_host_mounts=0
mnt_c_entries=0
workspace_bind=yes
home_bind=yes
path=PATHVALUE
python_ok=1
windows_bin=absent
cmd_bin=absent
c_drive=hidden
interop=registered
""".replace("PATHVALUE", GUEST_PATH)


class FakeWsl:
    """A scriptable `wsl.exe` - the only seam the WSL backend needs.

    It answers like the real thing: `-l -q` / `-l -v` in UTF-16LE (which is
    what wsl.exe emits), every `-e` command in UTF-8, and it records the
    argv of every call so a test can assert how the guest was invoked.
    """

    def __init__(self, *, distros=("Ubuntu",), version="2",
                 tools=HEALTHY_TOOLS, session=HEALTHY_SESSION, list_rc=0,
                 list_err="", prepared=True, exec_rc=0, dead=False):
        self.distros = list(distros)
        self.version = version
        self.tools = tools
        self.session = session
        self.list_rc = list_rc
        self.list_err = list_err
        self.prepared = prepared
        self.exec_rc = exec_rc
        self.dead = dead
        self.calls = []

    def run(self, args, *, stdin=None, timeout=wsl_mod.PROBE_TIMEOUT,
            cwd=None):
        argv = [str(a) for a in args]
        self.calls.append(argv)
        if self.dead:
            return wsl_mod.WslResult(None, b"", b"wsl.exe timed out",
                                     timed_out=True)
        if argv and argv[0] == "-l":
            if "-v" in argv:
                rows = ["  NAME   STATE   VERSION"]
                for index, name in enumerate(self.distros):
                    mark = "*" if index == 0 else " "
                    rows.append("%s %s   Running   %s"
                                % (mark, name, self.version))
                text = "\n".join(rows) + "\n"
                return wsl_mod.WslResult(0, text.encode("utf-16-le"), b"")
            if self.list_err or self.list_rc:
                return wsl_mod.WslResult(self.list_rc, b"",
                                         self.list_err.encode("utf-8"))
            text = "\n".join(self.distros) + "\n"
            return wsl_mod.WslResult(0, text.encode("utf-16-le"), b"")
        joined = "\n".join(argv)
        if "ASTRA_TOOLS" in joined:
            return self._text(self.tools)
        if "ASTRA_WSL_CHECK" in joined:
            return self._text(self.session)
        if "ASTRA_WSL_PREPARED" in joined:
            if self.prepared:
                return self._text("ASTRA_WSL_PREPARED=1\n")
            return wsl_mod.WslResult(1, b"", b"cannot create /var/lib")
        return wsl_mod.WslResult(self.exec_rc, b"", b"")

    @staticmethod
    def _text(text):
        return wsl_mod.WslResult(0, str(text).encode("utf-8"), b"")

    def script_calls(self):
        """Every guest command, as one blob (for content assertions)."""
        return "\n".join("\n".join(call) for call in self.calls)


def _windows_case(cls):
    """Run a detection test as if the host were Windows.

    `WslRuntimeBackend.probe()` refuses to be available off Windows, so the
    detection tests pin `detect_platform` - that is the only host fact the
    backend reads, and pinning it is what makes this file portable.
    """
    original = cls.setUp

    def setUp(self):
        original(self)
        patcher = mock.patch.object(wsl_mod, "detect_platform",
                                    return_value="windows")
        patcher.start()
        self.addCleanup(patcher.stop)

    cls.setUp = setUp
    return cls


@_windows_case
class TestWslDetection(unittest.TestCase):
    def backend(self, **kwargs):
        cfg = kwargs.pop("config", _Cfg())
        runner = FakeWsl(**kwargs)
        backend = WslRuntimeBackend(cfg, runner=runner)
        self.addCleanup(lambda: None)
        return backend, runner

    def test_healthy_probe_reports_the_runtime_identity(self):
        backend, _ = self.backend()
        info = backend.probe()
        self.assertTrue(info["available"], info["reason"])
        self.assertEqual(info["backend"], "wsl2")
        self.assertEqual(info["platform"], "windows")
        self.assertEqual(info["container"], "Ubuntu")
        self.assertEqual(info["distro"], "Ubuntu")
        self.assertEqual(info["shell"], "bash")
        self.assertEqual(info["workspace"], GUEST_WORKSPACE)
        self.assertEqual(info["home"], GUEST_HOME)
        self.assertEqual(info["guest_os"], "Ubuntu 24.04.3 LTS")
        self.assertEqual(info["distro_version"], "2")
        self.assertEqual(info["unshare"], "/usr/bin/unshare")
        self.assertEqual(info["python3"], "/usr/bin/python3")
        self.assertTrue(info["host_isolation"])
        self.assertEqual(info["reason"], "")
        self.assertEqual(info["details"]["windows_bin"], "absent")
        self.assertEqual(info["details"]["cmd_bin"], "absent")
        self.assertEqual(info["details"]["c_drive"], "hidden")

    def test_missing_wsl_exe_is_reported_never_worked_around(self):
        cfg = _Cfg({"RUNTIME_WSL_EXE": os.path.join(os.path.expanduser("~"),
                                                   "no-such-wsl.exe")})
        backend, _ = self.backend(config=cfg)
        info = backend.probe()
        self.assertFalse(info["available"])
        self.assertIn("wsl.exe was not found", info["reason"])
        self.assertIn("never falls back", info["hint"])
        with self.assertRaises(AstraRuntimeUnavailable):
            backend.require()
        # The failure names the backend the user must install - never a
        # host shell.
        self.assertEqual(info["backend"], "wsl2")

    def test_no_distributions_installed(self):
        backend, _ = self.backend(distros=())
        info = backend.probe()
        self.assertFalse(info["available"])
        self.assertIn("no WSL2 distribution is installed", info["reason"])
        self.assertIn("wsl --install -d Ubuntu", info["hint"])

    def test_the_configured_distribution_must_exist(self):
        backend, _ = self.backend(distros=("Debian", "Nexus-Node"))
        info = backend.probe()
        self.assertFalse(info["available"])
        self.assertIn("Ubuntu distribution is not installed", info["reason"])
        self.assertIn("RUNTIME_WSL_DISTRO", info["hint"])

    def test_a_wsl1_distribution_is_rejected(self):
        backend, _ = self.backend(version="1")
        info = backend.probe()
        self.assertFalse(info["available"])
        self.assertIn("requires WSL2", info["reason"])

    def test_a_distribution_that_cannot_be_listed(self):
        backend, _ = self.backend(list_rc=1, list_err="WSL_E_UNKNOWN")
        info = backend.probe()
        self.assertFalse(info["available"])
        self.assertIn("could not list distributions", info["reason"])

    def test_missing_guest_tools_are_named_with_their_package(self):
        tools = HEALTHY_TOOLS.replace("/usr/bin/unshare", "").replace(
            "/usr/bin/mount", "").replace("/usr/bin/python3", "")
        backend, _ = self.backend(tools=tools)
        info = backend.probe()
        self.assertFalse(info["available"])
        self.assertIn("unshare is not installed inside Ubuntu", info["reason"])
        self.assertIn("python3 is not installed inside Ubuntu", info["reason"])
        self.assertIn("util-linux", info["hint"])
        self.assertIn("python3", info["hint"])

    def test_a_distribution_that_starts_but_answers_nothing(self):
        backend, _ = self.backend(tools="")
        info = backend.probe()
        self.assertFalse(info["available"])
        self.assertIn("could not be started", info["reason"])

    def test_isolation_that_did_not_apply_is_a_failure(self):
        broken = HEALTHY_SESSION.replace("mnt_host_mounts=0",
                                         "mnt_host_mounts=3")
        backend, _ = self.backend(session=broken)
        info = backend.probe()
        self.assertFalse(info["available"])
        self.assertIn("could not be isolated", info["reason"])
        self.assertFalse(info["host_isolation"])

    def test_windows_reachability_is_a_failure(self):
        for key, value in (("windows_bin", "on_path"), ("cmd_bin", "on_path"),
                           ("c_drive", "visible")):
            broken = HEALTHY_SESSION.replace("%s=absent" % key,
                                             "%s=%s" % (key, value)).replace(
                "c_drive=hidden", "c_drive=%s" % value)
            backend, _ = self.backend(session=broken)
            info = backend.probe()
            self.assertFalse(info["available"], key)
            self.assertTrue(info["issues"], key)

    def test_a_wedged_wsl_is_unavailable_not_fatal(self):
        backend, _ = self.backend(dead=True)
        info = backend.probe()
        self.assertFalse(info["available"])
        self.assertTrue(info["reason"])

    def test_outside_windows_the_backend_says_so(self):
        with mock.patch.object(wsl_mod, "detect_platform",
                               return_value="linux"):
            info = WslRuntimeBackend(_Cfg(), runner=FakeWsl()).probe()
        self.assertFalse(info["available"])
        self.assertIn("requires Windows", info["reason"])

    def test_every_guest_command_is_executed_directly(self):
        backend, runner = self.backend()
        backend.probe()
        self.assertTrue(runner.calls)
        for call in runner.calls:
            if "-u" not in call:
                # `wsl.exe -l -q` / `-l -v`: management, not execution.
                continue
            self.assertEqual(call[0], "-d", call)
            self.assertEqual(call[call.index("-u") + 1], "root", call)
            # `-e` is what stops wsl.exe handing the command line to the
            # distribution's shell (which would expand $vars to nothing).
            self.assertEqual(call[call.index("-u") + 2], "-e", call)


class TestBackendSelection(unittest.TestCase):
    def test_auto_picks_the_backend_for_this_host(self):
        # The fake wsl.exe adapter is injected so the probe stays hermetic:
        # without it this test would spawn the real wsl.exe on a PC, making
        # the result depend on the machine rather than on the selection logic.
        engine = RuntimeEngine(_Cfg(), runner=FakeWsl())
        expected = BACKEND_WSL2 if detect_platform() == "windows" else (
            BACKEND_PROOT)
        self.assertEqual(engine.name, expected)
        self.assertEqual(engine.probe()["backend"], expected)

    def test_explicit_proot_is_honoured(self):
        backend = select_backend(_Cfg({"RUNTIME_BACKEND": "proot"}))
        self.assertIsInstance(backend, ProotRuntimeBackend)
        self.assertEqual(backend.name, "proot")

    def test_explicit_wsl2_is_honoured(self):
        backend = select_backend(_Cfg({"RUNTIME_BACKEND": "wsl2"}))
        self.assertIsInstance(backend, WslRuntimeBackend)
        self.assertEqual(backend.name, "wsl2")

    def test_the_wsl_alias_is_accepted(self):
        self.assertEqual(
            select_backend(_Cfg({"RUNTIME_BACKEND": "wsl"})).name, "wsl2")

    def test_an_unknown_backend_is_rejected_with_a_reason(self):
        backend = select_backend(_Cfg({"RUNTIME_BACKEND": "docker"}))
        self.assertIsInstance(backend, InvalidBackend)
        info = backend.probe()
        self.assertFalse(info["available"])
        self.assertIn("RUNTIME_BACKEND=docker", info["reason"])
        self.assertIn("auto", info["reason"])
        with self.assertRaises(AstraRuntimeUnavailable):
            backend.require()

    def test_proot_on_a_pc_gets_a_platform_specific_hint(self):
        if detect_platform() != "windows":
            self.skipTest("Windows-specific hint")
        info = select_backend(_Cfg({"RUNTIME_BACKEND": "proot"})).probe()
        self.assertEqual(info["backend"], "proot")
        if not info["available"]:
            self.assertIn("WSL2", info["hint"])
            self.assertIn("NOT", info["hint"])


class TestRuntimeEngineFacade(unittest.TestCase):
    def test_the_engine_reports_the_selected_backend(self):
        # Injected adapter: the engine's probe must be drivable without WSL.
        engine = RuntimeEngine(_Cfg({"RUNTIME_BACKEND": "wsl2"}),
                               runner=FakeWsl())
        self.assertEqual(engine.name, "wsl2")
        self.assertEqual(engine.container, "Ubuntu")
        self.assertEqual(engine.platform, detect_platform())
        info = engine.probe()
        self.assertEqual(info["backend"], engine.name)
        self.assertEqual(info["workspace"], GUEST_WORKSPACE)

    def test_an_explicit_prefix_still_means_proot(self):
        engine = RuntimeEngine(prefix="/nonexistent-prefix")
        self.assertEqual(engine.name, "proot")
        self.assertEqual(
            engine.base_rootfs(),
            os.path.join("/nonexistent-prefix", "var", "lib", "proot-distro",
                         "containers", "ubuntu", "rootfs"))
        self.assertFalse(engine.available())

    def test_each_backend_supplies_its_own_pty(self):
        # A POSIX host gets a real pty.fork() PTY; the Windows transport
        # drives a PTY that lives inside Ubuntu.
        self.assertIs(RuntimeEngine(prefix="/nonexistent").pty_class(),
                      PtyProcess)
        self.assertIs(WslRuntimeBackend(_Cfg()).pty_class(), WslPtyProcess)


class TestWslLayout(unittest.TestCase):
    def test_the_default_root_keeps_the_documented_path(self):
        backend = WslRuntimeBackend(_Cfg())
        base = os.path.join(os.path.expanduser("~"), ".astra", "runtime",
                            "default")
        self.assertEqual(
            backend.guest_source_dirs("default", base),
            ("/var/lib/astra/runtime/default/workspace",
             "/var/lib/astra/runtime/default/root",
             "/var/lib/astra/runtime/default/tmp"))
        # The Agent's view is ALWAYS the same three targets, on every
        # backend - that is what `AgentRuntime.binds` binds onto.
        self.assertEqual(backend.guest_dirs("default", base),
                         (GUEST_WORKSPACE, GUEST_HOME, GUEST_TMP))
        host = backend.host_dirs("default", base)
        for path in host:
            self.assertTrue(path.lower().startswith("\\\\wsl.localhost\\")
                            or path.lower().startswith("\\\\wsl$\\"), path)

    def test_another_runtime_root_can_never_collide(self):
        backend = WslRuntimeBackend(_Cfg())
        base = _tmpdir()
        try:
            workspace, home, tmp = backend.guest_source_dirs("default", base)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        self.assertTrue(workspace.startswith("/var/lib/astra/runtime/h-"),
                        workspace)
        self.assertTrue(home.endswith("/default/root"), home)
        self.assertTrue(tmp.endswith("/default/tmp"), tmp)

    def test_a_configured_root_relocates_the_tree(self):
        cfg = _Cfg({"RUNTIME_WSL_ROOT": "/srv/astra/"})
        backend = WslRuntimeBackend(cfg)
        self.assertEqual(backend.root, "/srv/astra")
        base = os.path.join(os.path.expanduser("~"), ".astra", "runtime")
        self.assertEqual(backend.guest_source_dirs("r1", base)[0],
                         "/srv/astra/r1/workspace")

    def test_the_workspace_can_be_overridden(self):
        cfg = _Cfg({"RUNTIME_WSL_WORKSPACE": "/work/thing/"})
        backend = WslRuntimeBackend(cfg)
        base = os.path.join(os.path.expanduser("~"), ".astra", "runtime")
        self.assertEqual(backend.guest_source_dirs("r1", base)[0],
                         "/work/thing")

    def test_unc_paths_round_trip(self):
        backend = WslRuntimeBackend(_Cfg())
        backend._unc = "\\\\wsl.localhost\\Ubuntu\\"
        host = backend.unc("/var/lib/astra/runtime/default/workspace")
        self.assertEqual(
            host,
            "\\\\wsl.localhost\\Ubuntu\\var\\lib\\astra\\runtime\\default"
            "\\workspace")
        self.assertEqual(
            backend.unc_to_guest(host),
            "/var/lib/astra/runtime/default/workspace")
        # A Windows path is NOT in this distribution, so it maps to nothing.
        self.assertEqual(backend.unc_to_guest("C:\\Users\\someone"), "")
        self.assertEqual(backend.unc_to_guest("\\\\wsl.localhost\\Debian\\x"),
                         "")

    def test_the_host_view_is_the_unc_view_of_the_guest_dir(self):
        backend = WslRuntimeBackend(_Cfg())
        backend._unc = "\\\\wsl.localhost\\Ubuntu\\"
        base = os.path.join(os.path.expanduser("~"), ".astra", "runtime",
                            "default")
        workspace, home, tmp = backend.host_dirs("default", base)
        self.assertEqual(backend.unc_to_guest(workspace),
                         "/var/lib/astra/runtime/default/workspace")
        self.assertEqual(backend.unc_to_guest(home),
                         "/var/lib/astra/runtime/default/root")
        self.assertEqual(backend.unc_to_guest(tmp),
                         "/var/lib/astra/runtime/default/tmp")

    def test_a_session_mounts_this_runtimes_own_tree(self):
        """The manager's binds must resolve back to THIS runtime's dirs.

        A regression guard: the guest side of a bind is the Agent-visible
        target (/workspace), while the SOURCE is this runtime's own tree
        inside Ubuntu - and a session that mounted the wrong source would
        have two runtimes sharing (or losing) each other's /tmp.
        """
        backend = WslRuntimeBackend(_Cfg())
        base = os.path.join(os.path.expanduser("~"), ".astra", "runtime",
                            "default")
        binds = list(zip(backend.host_dirs("default", base),
                         backend.guest_dirs("default", base)))
        self.assertEqual([guest for _, guest in binds],
                         [GUEST_WORKSPACE, GUEST_HOME, GUEST_TMP])
        self.assertEqual(backend._guest_dirs_from_binds(binds),
                         backend.guest_source_dirs("default", base))


class TestRuntimeBinds(unittest.TestCase):
    """`AgentRuntime.binds` is the join between the manager and a backend."""

    def test_the_guest_side_is_always_the_agent_visible_target(self):
        base = _tmpdir()
        try:
            for engine in (RuntimeEngine(_Cfg({"RUNTIME_BACKEND": "wsl2"})),
                           RuntimeEngine(prefix="/nonexistent-prefix")):
                runtime = AgentRuntime("default", engine,
                                       os.path.join(base, "default"))
                self.assertEqual([guest for _, guest in runtime.binds],
                                 [GUEST_WORKSPACE, GUEST_HOME, GUEST_TMP])
        finally:
            shutil.rmtree(base, ignore_errors=True)


@_windows_case
class TestWslLaunch(unittest.TestCase):
    def _backend(self, **kwargs):
        runner = FakeWsl(**kwargs)
        return WslRuntimeBackend(_Cfg(), runner=runner), runner

    def test_build_argv_isolates_and_binds_the_runtime_dirs(self):
        backend, _ = self._backend()
        backend.probe()
        base = _tmpdir()
        try:
            workspace, home, tmp = backend.host_dirs("default", base)
            guest_ws, guest_home, guest_tmp = backend.guest_source_dirs(
                "default", base)
            argv = backend.build_argv(
                binds=[(workspace, GUEST_WORKSPACE),
                       (home, GUEST_HOME), (tmp, GUEST_TMP)],
                cwd=GUEST_WORKSPACE, argv=["/bin/bash", "-l"])
        finally:
            shutil.rmtree(base, ignore_errors=True)
        self.assertTrue(argv[0].lower().endswith("wsl.exe"), argv[0])
        self.assertIn("-d", argv)
        self.assertIn("Ubuntu", argv)
        self.assertIn("-e", argv)
        self.assertTrue(any(str(a).endswith("unshare") for a in argv))
        self.assertIn("--propagation", argv)
        self.assertIn("private", argv)
        self.assertIn("/usr/bin/env", argv)
        self.assertIn("-i", argv)
        for path in (guest_ws, guest_home, guest_tmp):
            self.assertIn(path, argv)
        self.assertIn("PATH=" + GUEST_PATH, argv)
        self.assertIn("HOME=" + GUEST_HOME, argv)
        self.assertTrue(argv[-2:] == ["/bin/bash", "-l"], argv[-2:])

    def test_pty_argv_carries_the_bridge_config_in_one_argument(self):
        backend, _ = self._backend()
        backend.probe()
        base = _tmpdir()
        try:
            workspace, home, tmp = backend.host_dirs("default", base)
            argv = backend.pty_argv(
                binds=[(workspace, GUEST_WORKSPACE),
                       (home, GUEST_HOME), (tmp, GUEST_TMP)],
                cwd=GUEST_WORKSPACE, argv=["/bin/bash"], rows=40, cols=120)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        bridge = backend.bridge_path()
        self.assertIn(bridge, argv)
        encoded = argv[argv.index(bridge) + 1]
        config = json.loads(base64.b64decode(encoded).decode("utf-8"))
        self.assertEqual(config["argv"], ["/bin/bash"])
        self.assertEqual(config["rows"], 40)
        self.assertEqual(config["cols"], 120)
        self.assertEqual(config["cwd"], GUEST_WORKSPACE)
        self.assertEqual(config["umask"], "000")
        # The guest environment travels with the config - and it is the
        # minimal one, no host variable in sight.
        self.assertEqual(config["env"]["PATH"], GUEST_PATH)
        self.assertNotIn("ASTRA_HOST_SECRET_XYZ", json.dumps(config))
        self.assertNotIn("C:\\", json.dumps(config))
        # The bridge is the FIRST thing the session runs, before bash.
        self.assertLess(argv.index(bridge), len(argv) - 1)

    def test_run_bounds_the_command_inside_the_guest(self):
        backend, runner = self._backend()
        backend.probe()
        base = _tmpdir()
        try:
            workspace, home, tmp = backend.host_dirs("default", base)
            result = backend.run(
                binds=[(workspace, GUEST_WORKSPACE),
                       (home, GUEST_HOME), (tmp, GUEST_TMP)],
                cwd=GUEST_WORKSPACE, command="sleep 900", timeout=5)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        blob = runner.script_calls()
        self.assertIn("/usr/bin/timeout", blob)
        self.assertIn("--kill-after=5", blob)
        self.assertIn("sleep 900", blob)
        # ...and the caller still sees the command it asked for.
        self.assertEqual(result["command"], "sleep 900")

    def test_a_command_killed_by_the_guest_timeout_is_a_timeout(self):
        backend, runner = self._backend(exec_rc=124)
        backend.probe()
        base = _tmpdir()
        try:
            workspace, home, tmp = backend.host_dirs("default", base)
            result = backend.run(
                binds=[(workspace, GUEST_WORKSPACE),
                       (home, GUEST_HOME), (tmp, GUEST_TMP)],
                cwd=GUEST_WORKSPACE, command="sleep 900", timeout=7)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "timeout")
        self.assertIn("exceeded 7s", result["stderr"])
        self.assertEqual(result["command"], "sleep 900")

    def test_prepare_creates_the_dirs_and_installs_the_bridge(self):
        backend, runner = self._backend()
        base = _tmpdir()
        try:
            backend.prepare("default", base)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        blob = runner.script_calls()
        self.assertIn("ASTRA_WSL_PREPARED", blob)
        self.assertIn("chmod 0777", blob)
        self.assertIn(backend.bridge_path(), blob)
        # The in-guest PTY bridge is embedded verbatim (no file has to come
        # from the Windows filesystem).
        self.assertIn("TIOCSWINSZ", blob)
        self.assertIn("pty.fork", blob)
        self.assertIn("ASTRA_WSL_PREPARED=1", blob)

    def test_prepare_fails_closed_when_the_runtime_is_unavailable(self):
        broken = HEALTHY_SESSION.replace("mnt_host_mounts=0",
                                         "mnt_host_mounts=2")
        backend, _ = self._backend(session=broken)
        with self.assertRaises(AstraRuntimeUnavailable) as caught:
            backend.prepare("default", _tmpdir())
        self.assertIn("Agent Runtime unavailable", str(caught.exception))

    def test_prepare_reports_a_guest_failure(self):
        backend, _ = self._backend(prepared=False)
        with self.assertRaises(AstraRuntimeUnavailable) as caught:
            backend.prepare("default", _tmpdir())
        self.assertIn("could not prepare", str(caught.exception))

    def test_the_guest_environment_is_shared_and_minimal(self):
        env = guest_env()
        self.assertEqual(env["PATH"], GUEST_PATH)
        self.assertEqual(env["HOME"], GUEST_HOME)
        self.assertEqual(env["SHELL"], GUEST_SHELL)
        self.assertIn("astra", env["PS1"])
        # PIP_USER is NOT forced: it would break `pip install` inside a
        # virtualenv (pip refuses --user there). The runtime's own pip plan
        # passes --user explicitly, and PYTHONUSERBASE decides where it land
        # - so per-runtime privacy holds without breaking venvs.
        self.assertNotIn("PIP_USER", env)
        self.assertTrue(env["PYTHONUSERBASE"].startswith(GUEST_HOME))
        self.assertTrue(env["NPM_CONFIG_PREFIX"].startswith(GUEST_HOME))
        self.assertNotIn("USERPROFILE", env)
        self.assertNotIn("SystemRoot", env)


class TestProotBackendIsUnchanged(unittest.TestCase):
    """The Android/Termux backend must behave exactly as it did before the
    backend split - checked here without needing a real proot."""

    def _fake_prefix(self):
        prefix = _tmpdir()
        container = os.path.join(prefix, "var", "lib", "proot-distro",
                                 "containers", "ubuntu")
        os.makedirs(os.path.join(container, "rootfs", "usr", "bin"))
        os.makedirs(os.path.join(container, "sysdata"))
        bash = os.path.join(container, "rootfs", "usr", "bin", "bash")
        with open(bash, "w", encoding="utf-8") as fh:
            fh.write("")
        proot = os.path.join(prefix, "proot")
        with open(proot, "w", encoding="utf-8") as fh:
            fh.write("")
        return prefix, proot

    def test_a_missing_proot_is_reported_with_a_reason(self):
        backend = ProotRuntimeBackend(_Cfg(), prefix="/nonexistent-prefix")
        info = backend.probe()
        self.assertEqual(info["backend"], "proot")
        self.assertFalse(info["available"])
        self.assertIn("proot is not installed", info["reason"])
        self.assertIn("no proot-distro rootfs", info["reason"])
        with self.assertRaises(AstraRuntimeUnavailable):
            backend.require()

    def test_the_proot_argv_contract_is_unchanged(self):
        prefix, proot = self._fake_prefix()
        base = _tmpdir()
        try:
            backend = ProotRuntimeBackend(_Cfg({"RUNTIME_PROOT": proot}),
                                          prefix=prefix)
            info = backend.probe()
            self.assertTrue(info["available"], info["reason"])
            workspace = os.path.join(base, "workspace")
            argv = backend.build_argv(
                binds=[(workspace, GUEST_WORKSPACE)], cwd=GUEST_WORKSPACE,
                argv=["/bin/bash"])
        finally:
            shutil.rmtree(prefix, ignore_errors=True)
            shutil.rmtree(base, ignore_errors=True)
        # The launcher is exactly what the pre-backend-split proot engine
        # used: `env` resolved through PATH (so Termux's own $PREFIX/bin/env
        # is found) with a bare-name fallback, then `-i`. It is deliberately
        # NOT `/usr/bin/env` - that is the path inside a WSL2 distribution and
        # does not exist on Android, where the runtime lives under the Termux
        # prefix. Changing it would be a silent contract break.
        self.assertEqual(argv[0], shutil.which("env") or "env")
        self.assertEqual(argv[1], "-i")
        self.assertNotIn("/usr/bin/env", argv)
        self.assertIn("--kill-on-exit", argv)
        self.assertIn("--change-id=0:0", argv)
        self.assertIn("--rootfs=" + info["rootfs"], argv)
        self.assertIn("--bind=%s:%s" % (workspace, GUEST_WORKSPACE), argv)
        self.assertIn("--bind=/dev", argv)
        self.assertIn("--bind=/proc", argv)
        self.assertIn("--bind=/sys", argv)
        self.assertIn("PATH=" + GUEST_PATH, argv)
        self.assertEqual(backend.shell_argv(), [GUEST_SHELL])
        self.assertEqual(backend.exec_argv("id"), [GUEST_SHELL, "-c", "id"])
        self.assertEqual(backend.host_dirs("default", base),
                         (os.path.join(base, "workspace"),
                          os.path.join(base, "root"),
                          os.path.join(base, "tmp")))
        self.assertEqual(backend.guest_dirs("default", base),
                         (GUEST_WORKSPACE, GUEST_HOME, GUEST_TMP))
        self.assertEqual(backend.pty_class(), PtyProcess)
        self.assertEqual(backend.pty_argv(binds=[], cwd=GUEST_WORKSPACE)[0],
                         "env")


if __name__ == "__main__":
    unittest.main()
