"""PVC capacity accounting.

local-path provisioning does NOT enforce the requested size (it is just a host
directory), so statvfs alone would report the whole node disk. We therefore compute

    quota_total = configured size (20Gi)
    used        = actual bytes under the data dir (du-style walk, no symlink following)
    quota_free  = quota_total - used
    fs_free     = statvfs free bytes of the underlying filesystem
    free        = min(quota_free, fs_free)
    free_frac   = free / min(quota_total, fs_total)

Dispatch stops when free_frac < min_free_fraction (default 0.20).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path

UNITS = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}


def parse_size(s: str) -> int:
    m = re.match(r"^(\d+)(Ki|Mi|Gi|Ti)$", str(s))
    if not m:
        raise ValueError(f"bad size {s!r}")
    return int(m.group(1)) * UNITS[m.group(2)]


def dir_usage(root: str | Path) -> int:
    """Allocated bytes under root (st_blocks*512 so sparse files/overhead count honestly)."""
    total = 0
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += st.st_blocks * 512
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
            total += 0
        except OSError:
            continue
    return total


@dataclass
class StorageStatus:
    quota_total: int
    used: int
    fs_total: int
    fs_free: int
    free: int
    free_fraction: float
    min_free_fraction: float
    ok: bool

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate(quota_total: int, used: int, fs_total: int, fs_free: int, min_free_fraction: float) -> StorageStatus:
    cap = min(quota_total, fs_total) if fs_total else quota_total
    free = max(0, min(quota_total - used, fs_free))
    frac = free / cap if cap else 0.0
    return StorageStatus(quota_total, used, fs_total, fs_free, free, frac, min_free_fraction, frac >= min_free_fraction)


def check(data_dir: str | Path, size: str, min_free_fraction: float) -> StorageStatus:
    sv = os.statvfs(data_dir)
    return evaluate(parse_size(size), dir_usage(data_dir), sv.f_blocks * sv.f_frsize, sv.f_bavail * sv.f_frsize, min_free_fraction)
