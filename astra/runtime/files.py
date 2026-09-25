"""File operations inside the Agent Runtime.

Every path a caller names is a *guest* path (`/workspace/project/main.py`).
`RuntimePaths` maps it onto the runtime's host-side workspace directory and
refuses anything that would leave it, so no tool — and no AI that talks to a
tool — can address a host file even by accident.

Storage layout (host side)::

    <runtime_dir>/
        workspace/      -> /workspace   (projects, the "project root")
        root/           -> /root        (shell state: .bashrc, caches)
        tmp/            -> /tmp
        uploads/        (staging for imported files; never bound into guest)

Archives are extracted with the standard library (zipfile/tarfile) *in
process*, never by shelling out to the host's `tar`, and every member is
validated first:

* absolute member names (`/etc/passwd`) are rejected,
* any member that resolves outside the destination (`../`, or a symlink
  pointing out) is rejected,
* symlinks/hardlinks are extracted as links only when their target stays
  inside the destination,
* member count and total uncompressed size are bounded.

That is what stops a malicious `project.zip` from writing to the host.
"""
from __future__ import annotations

import os
import shutil
import stat
import tarfile
import time
import zipfile

from astra.core.exceptions import ValidationError

GUEST_WORKSPACE = "/workspace"
GUEST_HOME = "/root"
GUEST_TMP = "/tmp"

DEFAULT_MAX_FILE_MB = 512
DEFAULT_MAX_MEMBERS = 20000
DEFAULT_MAX_EXTRACT_MB = 2048

# Guest roots the agent may address, and the host dir each maps to.
_GUEST_ROOTS = (GUEST_WORKSPACE, GUEST_HOME, GUEST_TMP)

_LINK_SUFFIXES = {".lnk"}


def _norm_guest(path: str) -> str:
    """Normalise a guest path to an absolute, `..`-free form."""
    raw = str(path or "").strip()
    if not raw:
        raise ValidationError("path required")
    raw = raw.replace("\\", "/")
    if not raw.startswith("/"):
        raw = GUEST_WORKSPACE + "/" + raw
    parts: list[str] = []
    for piece in raw.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if not parts:
                raise ValidationError(
                    f"path escapes the runtime workspace: {path}")
            parts.pop()
            continue
        parts.append(piece)
    return "/" + "/".join(parts)


class RuntimePaths:
    """Guest↔host path mapping with workspace containment enforced.

    `allow_outside_workspace=False` (the default for file tools) restricts
    every operation to /workspace — the runtime's project root. Shell state
    (/root) and temp (/tmp) are only addressable when a caller explicitly
    opts in, which the runtime-internal helpers do.
    """

    def __init__(self, workspace: str, home: str, tmp: str, *,
                 max_file_mb: int = DEFAULT_MAX_FILE_MB,
                 max_members: int = DEFAULT_MAX_MEMBERS,
                 max_extract_mb: int = DEFAULT_MAX_EXTRACT_MB):
        self.host = {
            GUEST_WORKSPACE: os.path.abspath(workspace),
            GUEST_HOME: os.path.abspath(home),
            GUEST_TMP: os.path.abspath(tmp),
        }
        self.max_file_bytes = max(1, int(max_file_mb)) * 1024 * 1024
        self.max_members = max(1, int(max_members))
        self.max_extract_bytes = max(1, int(max_extract_mb)) * 1024 * 1024
        # Best effort, exactly like `AgentRuntime._makedirs`: on Windows the
        # runtime's directories live inside the WSL2 distribution and are
        # reached through its UNC path, which can be unavailable when the
        # distribution is not installed or not running. The backend reports
        # that condition; an unpaved path must not crash bootstrapping, and
        # the individual operations below surface a normal error instead.
        for path in self.host.values():
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                pass

    # -- mapping ------------------------------------------------------------
    def split(self, guest_path: str) -> tuple[str, str]:
        """Return (guest_root, host_root) for a guest path."""
        guest = _norm_guest(guest_path)
        for root in sorted(self.host, key=len, reverse=True):
            if guest == root or guest.startswith(root + "/"):
                return root, self.host[root]
        raise ValidationError(
            f"path is outside the runtime (allowed: {', '.join(_GUEST_ROOTS)}): "
            f"{guest_path}")

    def resolve(self, guest_path: str, *, must_exist: bool = False) -> str:
        """Map a guest path to a host path, verifying containment.

        Containment is re-checked on the FINAL absolute path (realpath, so a
        symlink planted inside the workspace that points at the host is
        caught too) — not just on the textual form.
        """
        root, host_root = self.split(guest_path)
        guest = _norm_guest(guest_path)
        rel = guest[len(root):].lstrip("/")
        host_path = os.path.normpath(os.path.join(host_root, rel))
        if not self._inside(host_path, host_root):
            raise ValidationError(f"path escapes the runtime: {guest_path}")
        real_root = os.path.realpath(host_root)
        probe = host_path
        # Walk up to the nearest existing ancestor and realpath that, so a
        # symlinked intermediate directory cannot smuggle us out.
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        if os.path.exists(probe):
            if not self._inside(os.path.realpath(probe), real_root):
                raise ValidationError(
                    f"path escapes the runtime through a symlink: {guest_path}")
        if must_exist and not os.path.exists(host_path):
            raise ValidationError(f"no such file in runtime: {guest}")
        return host_path

    @staticmethod
    def _inside(path: str, root: str) -> bool:
        path = os.path.normpath(path)
        root = os.path.normpath(root)
        return path == root or path.startswith(root + os.sep)

    def guest_of(self, host_path: str) -> str:
        for guest_root, host_root in self.host.items():
            if self._inside(host_path, host_root):
                rel = os.path.relpath(host_path, host_root)
                return guest_root if rel == "." else f"{guest_root}/{rel}"
        return host_path


# -- read-only inspection ---------------------------------------------------

def directory_list(paths: RuntimePaths, path: str = GUEST_WORKSPACE, *,
                   limit: int = 500) -> dict:
    host = paths.resolve(path, must_exist=True)
    if not os.path.isdir(host):
        raise ValidationError(f"not a directory: {path}")
    entries = []
    with os.scandir(host) as it:
        for entry in it:
            if len(entries) >= max(1, int(limit)):
                break
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append({
                "name": entry.name,
                "path": paths.guest_of(entry.path),
                "type": ("symlink" if entry.is_symlink()
                         else "directory" if entry.is_dir(follow_symlinks=False)
                         else "file"),
                "size": st.st_size,
                "mode": oct(stat.S_IMODE(st.st_mode)),
                "modified": st.st_mtime,
                "modified_iso": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            })
    entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
    return {"path": paths.guest_of(host), "entries": entries,
            "count": len(entries)}


def file_info(paths: RuntimePaths, path: str) -> dict:
    host = paths.resolve(path, must_exist=True)
    st = os.lstat(host)
    kind = ("symlink" if stat.S_ISLNK(st.st_mode)
            else "directory" if stat.S_ISDIR(st.st_mode) else "file")
    info = {"path": paths.guest_of(host), "type": kind,
            "size": st.st_size, "mode": oct(stat.S_IMODE(st.st_mode)),
            "modified": st.st_mtime,
            "modified_iso": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(st.st_mtime))}
    if kind == "symlink":
        target = os.readlink(host)
        info["target"] = target
        try:
            info["escapes_runtime"] = not paths._inside(
                os.path.realpath(os.path.join(os.path.dirname(host), target)),
                os.path.realpath(paths.host[GUEST_WORKSPACE]))
        except OSError:
            info["escapes_runtime"] = True
    return info


def read_text(paths: RuntimePaths, path: str, *, max_bytes: int = 200_000) -> dict:
    host = paths.resolve(path, must_exist=True)
    if os.path.isdir(host):
        raise ValidationError(f"is a directory: {path}")
    size = os.path.getsize(host)
    with open(host, "rb") as fh:
        data = fh.read(max(1, int(max_bytes)))
    return {"path": paths.guest_of(host), "size": size,
            "truncated": size > len(data),
            "text": data.decode("utf-8", "replace")}


def read_binary(paths: RuntimePaths, path: str, *,
                max_bytes: int = 50_000_000) -> dict:
    """Read a file inside the runtime as raw bytes, base64-encoded.

    Counterpart to `read_text` for non-text output (images, audio, video,
    documents, archives, ...) that a tool created inside the isolated
    runtime and that needs to travel back out to the caller (e.g. so it can
    be attached to a chat reply as a viewable/downloadable artifact). The
    same guest-workspace containment as every other file op here applies:
    only /workspace, /root and /tmp are addressable, and the resolved path
    is re-checked against symlink escapes.
    """
    import base64
    host = paths.resolve(path, must_exist=True)
    if os.path.isdir(host):
        raise ValidationError(f"is a directory: {path}")
    size = os.path.getsize(host)
    if size > max(1, int(max_bytes)):
        raise ValidationError(
            f"file exceeds the {max(1, int(max_bytes)) // (1024 * 1024)} MB "
            f"download limit: {path} ({size} bytes)")
    with open(host, "rb") as fh:
        data = fh.read()
    return {"path": paths.guest_of(host), "size": size,
            "content_base64": base64.b64encode(data).decode("ascii")}


# -- mutation ---------------------------------------------------------------

def make_directory(paths: RuntimePaths, path: str, *, parents: bool = True) -> dict:
    host = paths.resolve(path)
    os.makedirs(host, exist_ok=True) if parents else os.mkdir(host)
    return {"path": paths.guest_of(host), "created": True}


def write_text(paths: RuntimePaths, path: str, text: str, *,
               overwrite: bool = True) -> dict:
    host = paths.resolve(path)
    if os.path.exists(host) and not overwrite:
        raise ValidationError(f"already exists: {path}")
    os.makedirs(os.path.dirname(host) or paths.host[GUEST_WORKSPACE],
                exist_ok=True)
    data = text.encode("utf-8")
    if len(data) > paths.max_file_bytes:
        raise ValidationError(
            f"file exceeds the runtime limit of "
            f"{paths.max_file_bytes // (1024 * 1024)} MB")
    with open(host, "wb") as fh:
        fh.write(data)
    return {"path": paths.guest_of(host), "size": len(data)}


def copy_path(paths: RuntimePaths, source: str, destination: str) -> dict:
    src = paths.resolve(source, must_exist=True)
    dst = paths.resolve(destination)
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    return {"source": paths.guest_of(src), "destination": paths.guest_of(dst)}


def move_path(paths: RuntimePaths, source: str, destination: str) -> dict:
    src = paths.resolve(source, must_exist=True)
    dst = paths.resolve(destination)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    return {"source": paths.guest_of(src), "destination": paths.guest_of(dst)}


# Directories that must never be removed by a tool, however the path is spelled.
_PROTECTED = {GUEST_WORKSPACE, GUEST_HOME, GUEST_TMP}


def remove_path(paths: RuntimePaths, path: str, *, recursive: bool = False) -> dict:
    guest = _norm_guest(path)
    if guest in _PROTECTED:
        raise ValidationError(f"refusing to remove runtime root: {guest}")
    host = paths.resolve(guest, must_exist=True)
    if os.path.isdir(host) and not os.path.islink(host):
        if not recursive:
            raise ValidationError(f"is a directory (pass recursive=true): {path}")
        shutil.rmtree(host)
    else:
        os.remove(host)
    return {"path": guest, "removed": True}


# -- archives ---------------------------------------------------------------

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
                    ".tar.xz", ".txz")


def is_archive(filename: str) -> bool:
    low = str(filename or "").lower()
    return low.endswith(ARCHIVE_SUFFIXES)


def _safe_member(destination: str, name: str, *, dest_real: str) -> str | None:
    """Return the host path a member would land on, or None if it escapes."""
    if not name or name.startswith("/") or name.startswith("\\"):
        return None
    if os.path.isabs(name) or (len(name) > 1 and name[1] == ":"):
        return None
    target = os.path.normpath(os.path.join(destination, name))
    if target != destination and not target.startswith(destination + os.sep):
        return None
    if not RuntimePaths._inside(target, dest_real):
        return None
    return target


def extract_archive(paths: RuntimePaths, archive: str, destination: str = "",
                    *, strip_components: int = 0) -> dict:
    """Extract a zip/tar archive into the runtime, safely.

    Members are validated BEFORE anything is written, so an archive that
    tries to escape leaves nothing behind; the archive is read and written
    in a single pass (a zip/tar handle is not usable after its `with` block
    closes, which is why planning and writing are not separate phases).
    """
    src = paths.resolve(archive, must_exist=True)
    dest_guest = _norm_guest(destination or GUEST_WORKSPACE)
    dest_host = paths.resolve(dest_guest)
    os.makedirs(dest_host, exist_ok=True)
    dest_real = os.path.realpath(dest_host)

    low = src.lower()
    if low.endswith(".zip"):
        return _extract_zip(paths, src, dest_host, dest_guest, dest_real,
                            int(strip_components or 0))
    if any(low.endswith(s) for s in (".tar", ".tar.gz", ".tgz", ".tar.bz2",
                                     ".tbz2", ".tar.xz", ".txz")):
        return _extract_tar(paths, src, dest_host, dest_guest, dest_real,
                            int(strip_components or 0))
    raise ValidationError(
        f"unsupported archive type: {os.path.basename(src)} "
        f"(supported: {', '.join(ARCHIVE_SUFFIXES)})")


def _raw_name_is_safe(name: str) -> bool:
    """Reject a member whose name is absolute or traverses, BEFORE any
    component stripping.

    Stripping `../` or a leading `/` first would launder a hostile name into
    a harmless one (`/tmp/x` -> `tmp/x`), silently accepting the very
    archive the guard exists to reject.
    """
    raw = str(name or "").replace("\\", "/")
    if not raw:
        return False
    if raw.startswith("/"):
        return False
    if len(raw) > 1 and raw[1] == ":":
        return False
    depth = 0
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


def _strip_prefix(name: str, strip: int) -> str:
    parts = [p for p in str(name).replace("\\", "/").split("/")
             if p not in ("", ".")]
    return "/".join(parts[strip:]) if len(parts) > strip else ""


def _write_member(paths: RuntimePaths, dest_host: str, dest_real: str,
                  name: str, kind: str, *, target: str = "", reader=None
                  ) -> tuple[str | None, dict | None]:
    """Write one validated member; returns (guest_path, skip_reason)."""
    host_target = _safe_member(dest_host, name, dest_real=dest_real)
    if host_target is None:
        raise ValidationError(f"archive member escapes the destination: {name}")
    if kind == "dir":
        os.makedirs(host_target, exist_ok=True)
        return None, None
    os.makedirs(os.path.dirname(host_target) or dest_host, exist_ok=True)
    if kind == "link":
        resolved = os.path.normpath(
            os.path.join(os.path.dirname(host_target), target or ""))
        if not RuntimePaths._inside(resolved, dest_real):
            return None, {"name": name,
                          "reason": "link target escapes destination"}
        if os.path.lexists(host_target):
            os.remove(host_target)
        os.symlink(target, host_target)
        return None, None
    with open(host_target, "wb") as out:
        remaining = paths.max_extract_bytes
        while remaining > 0:
            chunk = reader.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)
    return paths.guest_of(host_target), None


def _extract_zip(paths, src, dest_host, dest_guest, dest_real, strip) -> dict:
    extracted: list[str] = []
    skipped: list[dict] = []
    total = 0
    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        if len(infos) > paths.max_members:
            raise ValidationError(
                f"archive has too many members ({len(infos)} > "
                f"{paths.max_members})")
        # Validate every name first: a hostile archive must not be able to
        # write half of itself and then fail.
        for info in infos:
            if not _raw_name_is_safe(info.filename):
                raise ValidationError(
                    f"archive member escapes the destination: {info.filename}")
            name = _strip_prefix(info.filename, strip)
            if not name:
                continue
            if _safe_member(dest_host, name, dest_real=dest_real) is None:
                raise ValidationError(
                    f"archive member escapes the destination: {info.filename}")
        for info in infos:
            name = _strip_prefix(info.filename, strip)
            if not name:
                continue
            total += max(0, info.file_size)
            if total > paths.max_extract_bytes:
                raise ValidationError(
                    f"archive expands beyond the "
                    f"{paths.max_extract_bytes // (1024 * 1024)} MB limit")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                target = zf.read(info).decode("utf-8", "replace")
                guest, skip = _write_member(paths, dest_host, dest_real, name,
                                            "link", target=target)
            elif info.filename.endswith("/") or stat.S_ISDIR(mode):
                guest, skip = _write_member(paths, dest_host, dest_real, name,
                                            "dir")
            else:
                with zf.open(info) as handle:
                    guest, skip = _write_member(
                        paths, dest_host, dest_real, name, "file",
                        reader=handle)
            if skip:
                skipped.append(skip)
            elif guest:
                extracted.append(guest)
    return {"archive": paths.guest_of(src), "destination": dest_guest,
            "extracted": extracted, "count": len(extracted),
            "skipped": skipped, "skipped_count": len(skipped)}


def _extract_tar(paths, src, dest_host, dest_guest, dest_real, strip) -> dict:
    extracted: list[str] = []
    skipped: list[dict] = []
    total = 0
    with tarfile.open(src, "r:*") as tf:
        members = tf.getmembers()
        if len(members) > paths.max_members:
            raise ValidationError(
                f"archive has too many members ({len(members)} > "
                f"{paths.max_members})")
        for member in members:
            if not _raw_name_is_safe(member.name):
                raise ValidationError(
                    f"archive member escapes the destination: {member.name}")
            name = _strip_prefix(member.name, strip)
            if name and _safe_member(dest_host, name, dest_real=dest_real) is None:
                raise ValidationError(
                    f"archive member escapes the destination: {member.name}")
        for member in members:
            name = _strip_prefix(member.name, strip)
            if not name:
                continue
            if member.isdir():
                guest, skip = _write_member(paths, dest_host, dest_real, name,
                                            "dir")
            elif member.issym() or member.islnk():
                guest, skip = _write_member(paths, dest_host, dest_real, name,
                                            "link", target=member.linkname)
            elif member.isfile():
                total += max(0, member.size)
                if total > paths.max_extract_bytes:
                    raise ValidationError(
                        f"archive expands beyond the "
                        f"{paths.max_extract_bytes // (1024 * 1024)} MB limit")
                handle = tf.extractfile(member)
                if handle is None:
                    continue
                with handle:
                    guest, skip = _write_member(
                        paths, dest_host, dest_real, name, "file",
                        reader=handle)
            else:
                continue
            if skip:
                skipped.append(skip)
            elif guest:
                extracted.append(guest)
    return {"archive": paths.guest_of(src), "destination": dest_guest,
            "extracted": extracted, "count": len(extracted),
            "skipped": skipped, "skipped_count": len(skipped)}


# -- import / upload --------------------------------------------------------

def import_file(paths: RuntimePaths, source_host_path: str, destination: str, *,
                overwrite: bool = True) -> dict:
    """Copy a host file INTO the runtime (upload/import).

    This is the only place host bytes are allowed to enter, and it is a
    deliberate, bounded, one-directional copy: the caller names a
    destination inside the runtime and the file is written there. Nothing
    is ever executed on the host.
    """
    source = os.path.abspath(source_host_path)
    if not os.path.isfile(source):
        raise ValidationError(f"upload source not found: {source_host_path}")
    size = os.path.getsize(source)
    if size > paths.max_file_bytes:
        raise ValidationError(
            f"file exceeds the runtime limit of "
            f"{paths.max_file_bytes // (1024 * 1024)} MB")
    guest = _norm_guest(destination)
    if os.path.basename(guest) in ("", ".", ".."):
        guest = os.path.join(guest, os.path.basename(source))
    elif destination.endswith("/"):
        guest = os.path.join(guest, os.path.basename(source))
    host_target = paths.resolve(guest)
    if os.path.isdir(host_target):
        host_target = os.path.join(host_target, os.path.basename(source))
    if os.path.exists(host_target) and not overwrite:
        raise ValidationError(f"already exists: {paths.guest_of(host_target)}")
    os.makedirs(os.path.dirname(host_target), exist_ok=True)
    shutil.copyfile(source, host_target)
    return {"path": paths.guest_of(host_target), "size": size,
            "source_name": os.path.basename(source)}
