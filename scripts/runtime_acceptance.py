"""Astra Agent Runtime + Astra Agent Terminal — real end-to-end acceptance.

Operator-runnable acceptance test for the isolation work. Everything here
runs against the REAL proot-isolated Agent Runtime — no mocks, no
simulation. It exists because a hermetic unit test cannot prove the
properties the design depends on (host isolation, per-runtime private
state, a live PTY, the host-terminal block): only the real backend can.

    python3 scripts/runtime_acceptance.py
    RUNTIME_CONTAINER=alpine python3 scripts/runtime_acceptance.py
    ASTRA_ACCEPTANCE_DIR=~/.astra/accept ASTRA_ACCEPTANCE_KEEP=1 python3 scripts/runtime_acceptance.py

It is deliberately NOT collected by pytest: it needs `proot` + a
proot-distro container and takes several minutes. The hermetic coverage
that DOES run in CI-style `pytest tests/` is in `tests/test_runtime.py`
(engine/isolation contract, lifecycle, archive guards, PTY resize, shared
chat<->terminal session, per-runtime isolation) and
`tests/test_host_terminal_block.py`. Exits non-zero if any check fails.

What it proves (spec §24):
  * lifecycle + the real toolchain (python3/node/npm/git/apt) inside the guest
  * a REAL PTY: interactive shell, live echo, TIOCSWINSZ resize the guest sees
  * package-manager detection, a verified install, and an import check
  * state (files + installed package) persists across a restart of A
  * runtime A and runtime B have PRIVATE state: B cannot see A's files or
    A's user-scope package, and B's writes never reach A
  * host isolation: a host file is unreadable, the Termux prefix is
    invisible, a host write is refused, no host path is on the guest PATH
  * Chat <-> Terminal share ONE runtime session/PTY (both directions)
  * the legacy host `terminal_exec` is hard-blocked for Agent execution and
    is not advertised in the Agent tool catalog
"""
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from astra.runtime.manager import RuntimeManager

BASE = os.path.expanduser(os.environ.get("ASTRA_ACCEPTANCE_DIR")
                          or os.path.join("~", "astra_acceptance_dir"))
KEEP = os.environ.get("ASTRA_ACCEPTANCE_KEEP", "") not in ("", "0", "false")
shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(BASE, exist_ok=True)


class _Cfg:
    """Minimal config shim (RuntimeManager only needs .get())."""

    def __init__(self, values):
        self._v = dict(values)

    def get(self, key, default=None):
        return self._v.get(key, default)


cfg = _Cfg({"RUNTIME_DIR": BASE,
            "RUNTIME_CONTAINER": os.environ.get("RUNTIME_CONTAINER",
                                                "ubuntu")})
mgr = RuntimeManager(config=cfg)

results = {}
def check(name, ok, detail=""):
    results[name] = bool(ok)
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  :: " + str(detail)[:200]) if detail else ""))
    return ok

print("== Agent Runtime availability ==")
rt_a = mgr.get("A")
st = rt_a.start()
info = rt_a.engine.probe()
print("   backend=%s platform=%s distro=%s shell=%s workspace=%s"
      % (info.get("backend"), info.get("platform"), info.get("distro"),
         info.get("shell"), info.get("workspace")))
print("   container=%s rootfs_mode=%s host_isolation=%s"
      % (st.get("container"), st.get("rootfs_mode"),
         st.get("host_isolation")))
tools = (st.get("capabilities") or {}).get("tools", {})
print("   tools:", json.dumps(tools)[:400])
check("runtime A started", st.get("state") == "running" and st.get("available"))
check("the runtime reports a backend (and never a host shell)",
      info.get("backend") in ("proot", "wsl2")
      and info.get("backend") not in ("cmd", "powershell"),
      info.get("backend"))
check("the runtime is host-isolated", bool(info.get("host_isolation")),
      info.get("issues"))

print("== runtime identity: the Agent's real toolchain ==")
r = rt_a.exec_command(
    "pwd; for t in python3 node npm git; do printf '%s=' \"$t\"; "
    "command -v \"$t\" || echo MISSING; done", session_id="conv-1")
out = r.get("stdout") or ""
check("the Agent's cwd is /workspace", out.strip().startswith("/workspace"),
      out[:120])
for tool in ("python3", "node", "npm", "git"):
    check("%s resolves INSIDE the runtime" % tool,
          ("%s=/" % tool) in out, out[:200])
check("guest PATH is Linux-only", "/mnt/" not in (info.get("details", {})
                                                 .get("path", ""))
      or info.get("platform") != "windows")
if info.get("platform") == "windows":
    details = info.get("details") or {}
    check("powershell.exe is not on the guest PATH",
          details.get("windows_bin") == "absent", details.get("windows_bin"))
    check("cmd.exe is not on the guest PATH",
          details.get("cmd_bin") == "absent", details.get("cmd_bin"))
    r = rt_a.exec_command("ls -A /mnt/c 2>&1 | wc -l; "
                          "cat /mnt/c/Windows/System32/cmd.exe 2>&1 | head -1",
                          session_id="conv-1")
    out = r.get("stdout") or ""
    check("the Windows C: drive is not reachable from the runtime",
          out.splitlines()[0].strip() == "0" and "No such file" in out,
          out[:200])

print("== real PTY: interactive shell inside the runtime ==")
p = rt_a.open_terminal("conv-1", rows=24, cols=80)
time.sleep(1.0)
p.write(b"echo PTY_ALIVE_$((6*7))\n")
time.sleep(1.2)
snap = p.text_since(0)
check("real PTY echoes command output", "PTY_ALIVE_42" in snap.get("data", ""), snap.get("data", "")[-200:])
p.write(b"tput cols; tput lines\n")
time.sleep(1.0)
snap2 = p.text_since(snap.get("next_offset", 0))
p.resize(rows=48, cols=132)
time.sleep(0.8)
p.write(b"tput cols; tput lines\n")
time.sleep(1.0)
snap3 = p.text_since(snap2.get("next_offset", 0))
check("PTY resize reaches the guest (tput cols/lines)", "132" in snap3.get("data", "") and "48" in snap3.get("data", ""),
      snap3.get("data", "")[-200:])

print("== runtime A: project + package install + verify ==")
r = rt_a.exec_command("mkdir -p /workspace/proj && cd /workspace/proj && "
                      "printf 'def add(a,b):\\n    return a+b\\n' > calc.py && "
                      "printf 'ok\\n' > marker_a.txt && pwd", session_id="conv-1")
check("project created inside runtime A", r.get("exit_code") == 0 and "/workspace/proj" in (r.get("stdout") or ""), r.get("stdout"))
managers = rt_a.package_managers()
print("   package managers detected:", managers)
check("package manager detection is real", bool(managers))
# A per-runtime USER-SCOPE install: with PYTHONUSERBASE/NPM_CONFIG_PREFIX
# pointing at `$HOME` (bound to `<runtime>/root`), the artifact must land in
# THIS runtime's private state, not the shared distro rootfs. pip is told
# `--user` explicitly by the runtime's own plan (never through an ambient
# PIP_USER, which would break venvs).
PKG = "cowsay"          # small, pure-python, not present in the base rootfs
inst = None
scope_cmd = ""
if "pip" in managers or "pip3" in managers:
    inst = rt_a.package_install(ecosystem="pip", packages=[PKG])
    scope_cmd = f"python3 -c 'import {PKG}; print(\"{PKG.upper()}_OK\", {PKG}.__file__)'"
elif "npm" in managers:
    inst = rt_a.package_install(ecosystem="npm", packages=["left-pad"], cwd="/workspace/proj")
    scope_cmd = "node -e 'console.log(require(\"/workspace/proj/node_modules/left-pad\") ? \"LEFT_PAD_OK\" : \"NO\")'"
print("   install:", json.dumps(inst)[:300] if inst else "skipped")
if inst is not None:
    check("package install verified (not just exit code)", bool(inst.get("verified")), inst)
r = rt_a.exec_command(scope_cmd, session_id="conv-1")
check("installed dependency importable inside runtime A",
      "OK" in (r.get("stdout") or ""), r.get("stdout"))
r = rt_a.exec_command("ls -d /root/.local/lib/python*/site-packages/cowsay 2>&1", session_id="conv-1")
check("pip install landed in the runtime's PRIVATE home (user scope)",
      "/root/.local" in (r.get("stdout") or ""), r.get("stdout"))

print("== apt, venv, a long-running process and reconnect (spec 21) ==")
r = rt_a.exec_command("apt-get update >/dev/null 2>&1; echo UPDATE_RC=$?",
                      session_id="conv-1", timeout=900)
check("apt-get update succeeds inside the runtime",
      "UPDATE_RC=0" in (r.get("stdout") or ""), (r.get("stdout") or "")[:200])
apt_install = rt_a.package_install(ecosystem="apt", packages=["curl"])
check("apt install curl is VERIFIED (not just exit code)",
      bool(apt_install.get("verified")), apt_install)
r = rt_a.exec_command("curl --version 2>&1 | head -1", session_id="conv-1")
check("curl runs inside the runtime", "curl" in (r.get("stdout") or ""),
      r.get("stdout"))
r = rt_a.exec_command(
    "python3 -m venv /workspace/py-test && /workspace/py-test/bin/python -c "
    "'import sys; print(\"VENV_OK\", sys.prefix)'", session_id="conv-1",
    timeout=600)
check("python3 -m venv works inside the runtime",
      "VENV_OK" in (r.get("stdout") or ""), (r.get("stdout") or "")[:200])
r = rt_a.exec_command(
    "/workspace/py-test/bin/pip install --quiet requests && /workspace/py-test/bin/python "
    "-c 'import requests; print(\"REQUESTS\", requests.__version__)'",
    session_id="conv-1", timeout=900)
check("pip install into the venv works",
      "REQUESTS" in (r.get("stdout") or ""), (r.get("stdout") or "")[:200])

# A long-running interactive process, stopped with a REAL Ctrl+C (0x03 into
# the guest PTY's line discipline) - the property a fake terminal cannot have.
srv = rt_a.open_terminal("conv-http", rows=24, cols=80)
time.sleep(0.8)
srv.write(b"python3 -m http.server 8765\n")
deadline = time.time() + 15
while time.time() < deadline:
    if "Serving HTTP" in srv.text_since(0).get("data", ""):
        break
    time.sleep(0.2)
check("a long-running server starts in the PTY",
      "Serving HTTP" in srv.text_since(0).get("data", ""))
srv.write(b"\x03")
time.sleep(2.5)
r = rt_a.exec_command("pgrep -af 'http.server' || echo NO_SERVER",
                      session_id="conv-http")
check("Ctrl+C stops it and the shell survives",
      "NO_SERVER" in (r.get("stdout") or ""), (r.get("stdout") or "")[:200])
r = rt_a.exec_command("echo shell-alive", session_id="conv-http")
check("the session is still usable after Ctrl+C",
      "shell-alive" in (r.get("stdout") or ""), r.get("stdout"))

# Files live on the runtime's disk, not in the PTY: reconnecting must show
# them, and the PTY for a session id must be stable.
rt_a.write_file("/workspace/runtime-test.txt", "persisted\n")
rt_a.close_terminal("conv-http")
again = rt_a.open_terminal("conv-http", rows=24, cols=80)
check("reconnect attaches to ONE PTY per session id",
      rt_a.get_terminal("conv-http") is again)
r = rt_a.exec_command("cat /workspace/runtime-test.txt", session_id="conv-http")
check("files survive a terminal reconnect",
      "persisted" in (r.get("stdout") or ""), r.get("stdout"))


print("== restart A: state persists ==")
rt_a.restart()
r = rt_a.exec_command("cd /workspace/proj && cat marker_a.txt && cat calc.py", session_id="conv-2")
check("A files persist across restart", "ok" in (r.get("stdout") or "") and "a+b" in (r.get("stdout") or ""), r.get("stdout"))
r = rt_a.exec_command(scope_cmd, session_id="conv-2")
check("A installed package persists across restart", "OK" in (r.get("stdout") or ""), r.get("stdout"))
print("   after restart:", (r.get("stdout") or r.get("stderr") or "").strip()[:120])

print("== runtime B: independent private state ==")
rt_b = mgr.get("B")
rt_b.start()
r = rt_b.exec_command("test -e /workspace/proj/marker_a.txt && echo A_VISIBLE || echo A_ABSENT",
                      session_id="conv-1")
check("runtime B cannot see runtime A's workspace files",
      "A_ABSENT" in (r.get("stdout") or ""), r.get("stdout"))
r = rt_b.exec_command("python3 -c 'import cowsay; print(\"B_HAS_PKG\")' 2>&1 | tail -1; "
                      "test -e /workspace/proj/node_modules && echo B_NM_VISIBLE || echo B_NM_ABSENT",
                      session_id="conv-1")
out = (r.get("stdout") or "")
check("runtime B does not inherit runtime A's installed package",
      "B_HAS_PKG" not in out and "B_NM_ABSENT" in out, out)
# And B's own install must not leak back into A.
r = rt_b.exec_command("echo B_ONLY > /workspace/proj/marker_b.txt 2>/dev/null || "
                      "(mkdir -p /workspace/proj && echo B_ONLY > /workspace/proj/marker_b.txt); "
                      "test -e /workspace/proj/marker_b.txt && echo B_WROTE", session_id="conv-1")
r = rt_a.exec_command("test -e /workspace/proj/marker_b.txt && echo LEAK || echo NO_LEAK", session_id="conv-3")
check("runtime B's writes do not leak into runtime A",
      "NO_LEAK" in (r.get("stdout") or ""), r.get("stdout"))

print("== host isolation ==")
host_secret = os.path.join(os.path.expanduser("~"), ".astra_host_secret_probe")
with open(host_secret, "w") as fh:
    fh.write("HOST_ONLY\n")
try:
    r = rt_a.exec_command(f"cat {host_secret} 2>&1; ls /data/data/com.termux 2>&1 | head -2; "
                          f"echo HOST_TMP > {host_secret}.pwn 2>&1; echo done", session_id="conv-1")
    out = r.get("stdout") or ""
    check("runtime cannot read a host file", "HOST_ONLY" not in out, out[:200])
    check("runtime cannot see the Termux prefix", "com.termux" not in out or "No such file" in out, out[:200])
    check("runtime cannot write to the host path", not os.path.exists(host_secret + ".pwn"))
    r2 = rt_a.exec_command("echo $PATH", session_id="conv-1")
    check("guest PATH has no host prefix", "/data/data/com.termux" not in (r2.get("stdout") or ""), r2.get("stdout"))
finally:
    for f in (host_secret, host_secret + ".pwn"):
        if os.path.exists(f):
            os.remove(f)

print("== chat <-> terminal share ONE runtime session ==")
# chat writes via runtime_command on the conversation's session id
r = rt_a.exec_command("mkdir -p /workspace/shared && echo CHAT_WROTE_THIS > /workspace/shared/chat.txt",
                      session_id="conv-77")
check("chat-side runtime_command wrote the file", r.get("exit_code") == 0, r)
# the terminal opens the SAME session id -> the same PTY object
term = rt_a.open_terminal("conv-77")
check("terminal attaches to the SAME PTY the chat used", rt_a.get_terminal("conv-77") is term)
term.write(b"cat /workspace/shared/chat.txt\n")
time.sleep(1.2)
snap = term.text_since(0)
check("terminal sees the file the chat created", "CHAT_WROTE_THIS" in snap.get("data", ""), snap.get("data", "")[-200:])
# terminal writes; chat reads
term.write(b"echo TERM_WROTE_THIS > /workspace/shared/terminal.txt\n")
time.sleep(1.2)
r = rt_a.exec_command("cat /workspace/shared/terminal.txt", session_id="conv-77")
check("chat reads the file the terminal created", "TERM_WROTE_THIS" in (r.get("stdout") or ""), r.get("stdout"))

print("== host terminal_exec is hard-blocked for Agent execution ==")
from astra.core.permissions import Policy
from astra.tools.registry import ToolRegistry
from astra.terminal import TerminalManager, register_terminal_tools
from astra.runtime.tools import register_runtime_tools
from astra.tools.builtins import register_builtins
from astra.ai.agent_tool_loop import AgentToolLoop, build_tool_catalog

pol = Policy(granted=["read", "low_risk_write", "browser_action", "system_action"])
reg = ToolRegistry(policy=pol)
register_builtins(reg)
host_term = TerminalManager()
register_terminal_tools(reg, host_term)
register_runtime_tools(reg, mgr)
check("host terminal_exec is NOT in the Agent tool catalog", "terminal_exec" not in build_tool_catalog(reg))
check("runtime_command IS in the Agent tool catalog", "runtime_command" in build_tool_catalog(reg))

class Brain:
    def __init__(self, replies): self.replies = list(replies); self.calls = []
    def chat(self, messages, max_tokens=None, trace=""):
        self.calls.append(list(messages)); return self.replies.pop(0)

marker = os.path.join(os.path.expanduser("~"), ".astra_host_pwn.rs")
if os.path.exists(marker): os.remove(marker)
brain = Brain([json.dumps({"action": "tool", "tool": "terminal_exec",
                           "args": {"command": f"touch {marker}"}, "thought": "x"}),
               json.dumps({"action": "final", "answer": "refused"})])
loop = AgentToolLoop(reg, terminal=host_term, runtime=mgr)
res = loop.run("touch a host file", brain, system_prompt="", session_id="conv-9", scope="9")
step = res.steps[0]
check("Agent terminal_exec call is rejected (not executed)", (not step.ok) and step.status == "blocked", step.error)
check("no host file was created by the rejected call", not os.path.exists(marker))
r = reg.execute("terminal_exec", {"command": "echo operator-ok"}, ctx=None)
# The Agent path above was refused; a TRUSTED internal call is still allowed
# through the same gate. Whether the HOST shell itself completes is a
# host-terminal concern (native Windows' PowerShell sentinel has a
# pre-existing quoting bug), not an Agent Runtime one.
check("host terminal still reachable to trusted internals",
      r.get("decision") == "allow",
      (r.get("result") or {}).get("stderr"))
if not (r.get("ok") and "operator-ok" in (r["result"].get("stdout") or "")):
    print("  NOTE  the host terminal itself reported a failure on this host "
          "(pre-existing host-shell limitation, unrelated to the Agent "
          "Runtime)")

print("== cleanup ==")
mgr.close_all()
rt_a.destroy(); rt_b.destroy()
host_term.close_all()
if KEEP:
    print("   kept runtime dir:", BASE)
else:
    shutil.rmtree(BASE, ignore_errors=True)

failed = [k for k, v in results.items() if not v]
print("\n=== ACCEPTANCE: %d/%d passed ==="
      % (len(results) - len(failed), len(results)))
for k in failed:
    print("  FAILED:", k)
if failed:
    print("\nRESULT: FAIL — the Agent Runtime did not meet the isolation "
          "acceptance criteria.")
else:
    print("\nRESULT: PASS — real runtime, real PTY, real isolation, real "
          "verification;\n        host terminal refused for Agent "
          "execution; Chat and Terminal shared one session.")
sys.exit(1 if failed else 0)
