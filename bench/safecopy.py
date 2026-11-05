"""Safe copy of an agent-controlled workspace into a trusted location.

The source tree is attacker-controlled (the agent can create symlinks, hardlink tricks,
FIFOs, absurd sizes, deep trees). We never follow links and never trust names:
- every entry is lstat'd; symlinks, devices, FIFOs and sockets are REJECTED (recorded),
- names must not be '.', '..', contain NUL or '/', and the resolved destination must stay
  inside the destination root,
- regular files are opened with O_NOFOLLOW and re-checked with fstat (anti TOCTOU swap),
  hardlinked files (st_nlink > 1) are copied by content, never re-linked,
- caps on file count, total bytes, per-file bytes and depth.
Heavy regenerable dirs (node_modules, target, .git objects) are skipped and listed.
"""
from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

# Credential / provider-config stores that must never be exported (they may hold the API key
# or the endpoint wiring). Matched against the file name.
SECRET_FILE_GLOBS = ("auth.json", "auth*.json", "models.json", "opencode.json", "kilo.json", "providers.json",
                     "models.yml", "models.yaml", "config.yaml", "config.yml", ".env", ".env.*", "credentials*",
                     "*.pem", "*.key", "trust.json", "secrets*", "*.token")
SECRET_DIR_NAMES = ("custom_providers", "settings", ".omp", ".ssh", ".aws", ".gnupg")

DEFAULT_SKIP_DIRS = ("node_modules", "target", "__pycache__", ".cache", ".npm", ".cargo", ".rustup")


class UnsafeSubmission(Exception):
    pass


@dataclass
class CopyReport:
    files: int = 0
    bytes: int = 0
    skipped_dirs: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)   # {path, reason}
    truncated: bool = False
    skipped_files: list[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return not self.rejected and not self.truncated

    def as_dict(self) -> dict:
        return {"files": self.files, "bytes": self.bytes, "skipped_dirs": self.skipped_dirs, "skipped_files": self.skipped_files,
                "rejected": self.rejected, "truncated": self.truncated, "safe": self.safe}


def safe_copy(src: str | Path, dst: str | Path, *, max_files: int = 20000, max_total: int = 256 << 20,
              max_file: int = 32 << 20, max_depth: int = 24, skip_dirs: tuple[str, ...] = DEFAULT_SKIP_DIRS,
              skip_file_globs: tuple[str, ...] = (), skip_dir_names: tuple[str, ...] = ()) -> CopyReport:
    src, dst = Path(src), Path(dst)
    rep = CopyReport()
    root_st = os.lstat(src)
    if not stat.S_ISDIR(root_st.st_mode):
        rep.rejected.append({"path": ".", "reason": "workspace root is not a real directory"})
        return rep
    dst.mkdir(parents=True, exist_ok=True)
    dst_real = os.path.realpath(dst)

    def walk(sdir: str, ddir: str, rel: str, depth: int) -> None:
        if depth > max_depth:
            rep.rejected.append({"path": rel, "reason": "too deep"})
            return
        try:
            names = sorted(os.listdir(sdir))
        except OSError as e:
            rep.rejected.append({"path": rel, "reason": f"unreadable dir: {e}"})
            return
        for name in names:
            r = f"{rel}/{name}" if rel else name
            if name in ("", ".", "..") or "\x00" in name or "/" in name:
                rep.rejected.append({"path": r, "reason": "illegal name"})
                continue
            sp, dp = os.path.join(sdir, name), os.path.join(ddir, name)
            try:
                st = os.lstat(sp)
            except OSError as e:
                rep.rejected.append({"path": r, "reason": f"lstat failed: {e}"})
                continue
            m = st.st_mode
            if stat.S_ISLNK(m):
                rep.rejected.append({"path": r, "reason": "symlink"})
            elif stat.S_ISDIR(m):
                if name in skip_dirs or name in skip_dir_names:
                    rep.skipped_dirs.append(r)
                    continue
                os.makedirs(dp, exist_ok=True)
                if not os.path.realpath(dp).startswith(dst_real + os.sep):
                    raise UnsafeSubmission(f"destination escape at {r}")
                walk(sp, dp, r, depth + 1)
            elif stat.S_ISREG(m):
                if any(fnmatch.fnmatch(name, g) for g in skip_file_globs):
                    rep.skipped_files.append(r)
                    continue
                if rep.files >= max_files or rep.bytes + st.st_size > max_total:
                    rep.truncated = True
                    rep.rejected.append({"path": r, "reason": "size/count cap exceeded"})
                    continue
                if st.st_size > max_file:
                    rep.rejected.append({"path": r, "reason": f"file larger than {max_file}"})
                    continue
                try:
                    fd = os.open(sp, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except OSError as e:
                    rep.rejected.append({"path": r, "reason": f"open failed (swapped to link?): {e}"})
                    continue
                try:
                    fst = os.fstat(fd)
                    if not stat.S_ISREG(fst.st_mode) or (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
                        rep.rejected.append({"path": r, "reason": "changed during copy"})
                        continue
                    out = os.open(dp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                    try:
                        n = 0
                        while True:
                            chunk = os.read(fd, 1 << 20)
                            if not chunk:
                                break
                            n += len(chunk)
                            if n > max_file:
                                raise UnsafeSubmission(f"{r} grew beyond cap while copying")
                            os.write(out, chunk)
                    finally:
                        os.close(out)
                    if fst.st_mode & 0o111:
                        os.chmod(dp, 0o755)
                    rep.files += 1
                    rep.bytes += n
                finally:
                    os.close(fd)
            else:
                rep.rejected.append({"path": r, "reason": "special file (fifo/device/socket)"})

    walk(str(src), str(dst), "", 0)
    return rep
