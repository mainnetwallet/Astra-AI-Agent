"""Package managers INSIDE the Agent Runtime.

Detection, installation and — mandatory — verification. Nothing here ever
touches the host: every command is executed through the runtime engine, so
`apt-get install` runs Ubuntu's apt inside the rootfs, not Termux's, and
`npm install` writes into the runtime's `/workspace`.

The rule this module exists to enforce (spec §16): *never claim success
without verification*. `install()` returns `verified` plus the exact
evidence (`verifier` command, exit code, output) that produced it, and a
successful exit code on its own is not treated as proof.
"""
from __future__ import annotations

import shlex

from astra.core.exceptions import ValidationError
from astra.runtime.engine import GUEST_WORKSPACE

DETECT_TIMEOUT = 60.0
INSTALL_TIMEOUT = 1800.0
VERIFY_TIMEOUT = 120.0

# One probe, one round trip: each line prints `NAME=<version or ->` so
# detection costs a single guest invocation instead of a dozen.
DETECT_PROBE = r"""
for t in python3 pip3 pip node npm yarn pnpm git apt-get apt apk dpkg \
         gcc make curl wget; do
  if command -v "$t" >/dev/null 2>&1; then
    v=$("$t" --version 2>&1 | head -1 | tr -d '\r')
    printf '%s=%s\n' "$t" "$v"
  else
    printf '%s=-\n' "$t"
  fi
done
printf 'os=%s\n' "$(cat /etc/os-release 2>/dev/null | sed -n 's/^PRETTY_NAME=//p' | tr -d '\"')"
printf 'arch=%s\n' "$(uname -m)"
"""


def detect(engine, binds, cwd: str = GUEST_WORKSPACE, *,
           timeout: float = DETECT_TIMEOUT) -> dict:
    """Report which tools/package managers actually exist in the runtime."""
    result = engine.run(binds=binds, cwd=cwd, command=DETECT_PROBE,
                        timeout=timeout)
    tools: dict[str, str] = {}
    meta: dict[str, str] = {}
    for line in (result.get("stdout") or "").splitlines():
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if name in ("os", "arch"):
            meta[name] = value
        elif value and value != "-":
            tools[name] = value
    managers = [m for m in ("apt-get", "apk", "pip3", "pip", "npm", "yarn",
                            "pnpm", "git") if m in tools]
    return {
        "available": result.get("ok", False),
        "tools": tools,
        "managers": managers,
        "missing": [m for m in ("apt-get", "apk", "pip3", "pip", "npm",
                                "yarn", "pnpm", "git") if m not in tools],
        "os": meta.get("os", ""),
        "arch": meta.get("arch", ""),
        "exit_code": result.get("exit_code"),
        "error": result.get("stderr", ""),
    }


# -- which command implements which package operation -----------------------

def _pip_command(managers: list[str]) -> str:
    if "python3" in managers or "pip3" in managers:
        return "python3 -m pip"
    return "pip"


def plan_install(*, ecosystem: str, packages: list[str], managers: list[str],
                 global_scope: bool = False) -> dict:
    """Choose the command for an install request, or explain why it can't.

    `ecosystem` is the friendly name the Gateway/Provider uses
    ("npm", "pip", "apt", "apk", "git"); the plan names the concrete
    command and the verifier that will run afterwards.
    """
    eco = str(ecosystem or "").strip().lower()
    pkgs = [str(p).strip() for p in (packages or []) if str(p).strip()]
    if eco == "git":
        if "git" not in managers:
            return {"supported": False, "reason": "git is not installed in the runtime"}
        if not pkgs:
            return {"supported": False, "reason": "git clone requires a repository URL"}
        url = shlex.quote(pkgs[0])
        dest = shlex.quote(pkgs[1]) if len(pkgs) > 1 else ""
        return {"supported": True, "ecosystem": "git", "packages": pkgs,
                "command": f"git clone {url}" + (f" {dest}" if dest else ""),
                "verifier": _git_verifier(pkgs),
                "verifier_label": "repository exists"}
    if eco == "npm":
        if "npm" not in managers:
            return {"supported": False, "reason": "npm is not installed in the runtime"}
        if not pkgs:
            return {"supported": False, "reason": "npm install requires package names"}
        flags = "-g" if global_scope else ""
        names = " ".join(shlex.quote(p) for p in pkgs)
        return {"supported": True, "ecosystem": "npm", "packages": pkgs,
                "command": f"npm install {flags} --no-fund --no-audit {names}".replace("  ", " "),
                "verifier": _npm_verifier(pkgs, global_scope),
                "verifier_label": "package present"}
    if eco in ("pip", "python"):
        if not pkgs:
            return {"supported": False, "reason": "pip install requires package names"}
        names = " ".join(shlex.quote(p) for p in pkgs)
        has_pip = any(m in managers for m in ("pip3", "pip")) or (
            "python3" in managers)
        if not has_pip:
            # Spec §11: a missing manager is reported clearly, and installed
            # INSIDE the runtime when that is safely supported. A Debian/
            # Ubuntu rootfs can always bootstrap pip from its own apt — that
            # is a runtime-internal install, never a host one.
            if "apt-get" not in managers:
                return {"supported": False,
                        "reason": "pip is not installed in the runtime and "
                                  "there is no apt-get to bootstrap it with"}
            bootstrap = ("apt-get update && apt-get install -y python3-pip "
                         "python3-venv")
            base = "python3 -m pip"
            return {"supported": True, "ecosystem": "pip", "packages": pkgs,
                    "bootstrap": bootstrap,
                    "bootstrap_label": "installing pip into the runtime",
                    "command": f"{base} install --break-system-packages {names}",
                    "verifier": _pip_verifier(pkgs, base),
                    "verifier_label": "module imports"}
        base = _pip_command(managers)
        break_flag = " --break-system-packages" if "pip3" in managers else ""
        return {"supported": True, "ecosystem": "pip", "packages": pkgs,
                "command": f"{base} install{break_flag} {names}",
                "verifier": _pip_verifier(pkgs, base),
                "verifier_label": "module imports"}
    if eco in ("apt", "apt-get"):
        if "apt-get" not in managers:
            return {"supported": False,
                    "reason": "apt-get is not installed in the runtime "
                              "(this is not a Debian/Ubuntu rootfs)"}
        if not pkgs:
            return {"supported": False, "reason": "apt install requires package names"}
        names = " ".join(shlex.quote(p) for p in pkgs)
        return {"supported": True, "ecosystem": "apt", "packages": pkgs,
                "command": f"apt-get update && apt-get install -y {names}",
                "verifier": _dpkg_verifier(pkgs),
                "verifier_label": "package installed"}
    if eco == "apk":
        if "apk" not in managers:
            return {"supported": False,
                    "reason": "apk is not installed in the runtime "
                              "(this is not an Alpine rootfs)"}
        if not pkgs:
            return {"supported": False, "reason": "apk add requires package names"}
        names = " ".join(shlex.quote(p) for p in pkgs)
        return {"supported": True, "ecosystem": "apk", "packages": pkgs,
                "command": f"apk add {names}",
                "verifier": _apk_verifier(pkgs),
                "verifier_label": "package installed"}
    return {"supported": False,
            "reason": f"unsupported ecosystem '{ecosystem}' "
                      f"(use npm, pip, apt, apk or git)"}


def _npm_verifier(packages: list[str], global_scope: bool) -> str:
    if global_scope:
        names = " ".join(shlex.quote(p) for p in packages)
        return f"npm ls -g --depth=0 {names}"
    checks = " && ".join(
        f"[ -d node_modules/{shlex.quote(p)} ]" for p in packages)
    return checks or "true"


def _pip_verifier(packages: list[str], base: str = "") -> str:
    # Map PyPI distribution names to importable module names for the
    # handful where they differ; everything else is a direct import.
    alias = {"pillow": "PIL", "beautifulsoup4": "bs4", "pyyaml": "yaml",
             "opencv-python": "cv2", "python-dateutil": "dateutil",
             "scikit-learn": "sklearn", "psycopg2-binary": "psycopg2"}
    mods = []
    for pkg in packages:
        name = pkg.split("[")[0].split("==")[0].split(">=")[0].strip()
        mods.append(alias.get(name.lower(), name.replace("-", "_")))
    imports = "; ".join(f"import {m}" for m in mods)
    # Verify with the INTERPRETER, not pip: `python3 -m pip -c ...` is not a
    # thing, and the real question is "can this Python import the module?".
    return f"python3 -c {shlex.quote(imports + '; print(\"verify-ok\")')}"


def _dpkg_verifier(packages: list[str]) -> str:
    return " && ".join(f"dpkg -s {shlex.quote(p)} >/dev/null 2>&1"
                       for p in packages)


def _apk_verifier(packages: list[str]) -> str:
    return " && ".join(f"apk info -e {shlex.quote(p)} >/dev/null 2>&1"
                       for p in packages)


def _git_verifier(packages: list[str]) -> str:
    dest = packages[1] if len(packages) > 1 else ""
    if not dest:
        # Derive the default directory git would have used.
        url = packages[0].rstrip("/")
        dest = url.rsplit("/", 1)[-1]
        if dest.endswith(".git"):
            dest = dest[:-4]
    return (f"[ -d {shlex.quote(dest)}/.git ] && "
            f"git -C {shlex.quote(dest)} rev-parse --is-inside-work-tree")


def verify(engine, binds, *, verifier: str, cwd: str = GUEST_WORKSPACE,
           timeout: float = VERIFY_TIMEOUT) -> dict:
    """Run a verifier command in the runtime; `verified` is the exit status."""
    if not verifier:
        return {"verified": False, "reason": "no verifier available"}
    result = engine.run(binds=binds, cwd=cwd, command=verifier,
                        timeout=timeout)
    ok = bool(result.get("ok"))
    return {"verified": ok, "command": verifier,
            "exit_code": result.get("exit_code"),
            "stdout": (result.get("stdout") or "")[-1500:],
            "stderr": (result.get("stderr") or "")[-1500:],
            "reason": "" if ok else "verification command failed"}


def install(engine, binds, *, ecosystem: str, packages: list[str],
            managers: list[str], cwd: str = GUEST_WORKSPACE,
            global_scope: bool = False, timeout: float = INSTALL_TIMEOUT,
            on_event=None) -> dict:
    """Install packages in the runtime, then verify the install."""
    plan = plan_install(ecosystem=ecosystem, packages=packages,
                        managers=managers, global_scope=global_scope)
    if not plan.get("supported"):
        return {"ok": False, "installed": False, "verified": False,
                "ecosystem": ecosystem, "packages": packages,
                "error": plan.get("reason", "unsupported install"),
                "reason": plan.get("reason", "unsupported install")}

    def emit(kind, **data):
        if on_event is not None:
            try:
                on_event(kind, **data)
            except Exception:
                pass

    emit("runtime.package.install.started", ecosystem=plan["ecosystem"],
         packages=packages, command=plan["command"])
    if plan.get("bootstrap"):
        emit("runtime.package.install.started", ecosystem="bootstrap",
             packages=[plan.get("bootstrap_label", "bootstrap")],
             command=plan["bootstrap"])
        boot = engine.run(binds=binds, cwd=cwd, command=plan["bootstrap"],
                          timeout=timeout)
        if not boot.get("ok"):
            emit("runtime.package.install.failed", ecosystem="bootstrap")
            return {"ok": False, "installed": False, "verified": False,
                    "ecosystem": plan["ecosystem"], "packages": packages,
                    "command": plan["command"],
                    "bootstrap": plan["bootstrap"],
                    "exit_code": boot.get("exit_code"),
                    "stdout": (boot.get("stdout") or "")[-4000:],
                    "stderr": (boot.get("stderr") or "")[-4000:],
                    "error": "could not bootstrap the package manager inside "
                             "the runtime: " +
                             (boot.get("stderr") or "")[-500:]}
    result = engine.run(binds=binds, cwd=cwd, command=plan["command"],
                        timeout=timeout)
    if not result.get("ok"):
        emit("runtime.package.install.failed", ecosystem=plan["ecosystem"],
             packages=packages, exit_code=result.get("exit_code"))
        return {"ok": False, "installed": False, "verified": False,
                "ecosystem": plan["ecosystem"], "packages": packages,
                "command": plan["command"],
                "exit_code": result.get("exit_code"),
                "stdout": (result.get("stdout") or "")[-4000:],
                "stderr": (result.get("stderr") or "")[-4000:],
                "error": (result.get("stderr") or "install command failed")[-500:]}

    check = verify(engine, binds, verifier=plan["verifier"], cwd=cwd)
    payload = {
        "ok": bool(check.get("verified")),
        "installed": True,
        "verified": bool(check.get("verified")),
        "ecosystem": plan["ecosystem"],
        "packages": packages,
        "command": plan["command"],
        "exit_code": result.get("exit_code"),
        "stdout": (result.get("stdout") or "")[-4000:],
        "stderr": (result.get("stderr") or "")[-2000:],
        "verification": check,
        "verifier_label": plan.get("verifier_label", ""),
    }
    if check.get("verified"):
        emit("runtime.package.install.completed", ecosystem=plan["ecosystem"],
             packages=packages)
    else:
        emit("runtime.package.install.failed", ecosystem=plan["ecosystem"],
             packages=packages, verified=False)
        payload["error"] = ("install command succeeded but verification "
                            "failed — treating this as NOT installed")
    return payload
