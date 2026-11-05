"""kubectl access: over ``ssh <host> kubectl ...``, a local kubectl, or in-cluster.

Arguments are always passed as an argv list; for ssh they are shell-quoted with
shlex.join, so nothing from config/tasks is ever interpreted by a shell.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from typing import Any, Callable, Sequence

MUTATING_VERBS = {"apply", "create", "delete", "patch", "annotate", "label", "replace", "scale", "exec", "cp", "run", "edit", "set", "cordon", "drain"}
ARG_OK = re.compile(r"^[^\x00\n\r]*$")


class KubeError(Exception):
    pass


class ReadOnlyViolation(KubeError):
    pass


Runner = Callable[[Sequence[str], bytes | None, float | None], subprocess.CompletedProcess]


def _default_runner(argv: Sequence[str], stdin: bytes | None, timeout: float | None) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), input=stdin, capture_output=True, timeout=timeout, check=False)


class Kube:
    def __init__(self, access: str, namespace: str, ssh_host: str | None = None, context: str | None = None,
                 runner: Runner | None = None, read_only: bool = False):
        if access not in ("ssh", "local-kubectl", "in-cluster"):
            raise KubeError(f"bad access {access}")
        self.access, self.namespace, self.ssh_host, self.context = access, namespace, ssh_host, context
        self.runner = runner or _default_runner
        self.read_only = read_only

    @classmethod
    def from_config(cls, cfg, **kw) -> "Kube":
        k = cfg.data["kube"]
        return cls(k["access"], k["namespace"], k.get("ssh_host"), k.get("context"), **kw)

    # ---- argv construction
    def kubectl_argv(self, args: Sequence[str], namespaced: bool = True) -> list[str]:
        for a in args:
            if not isinstance(a, str) or not ARG_OK.match(a):
                raise KubeError(f"illegal kubectl argument {a!r}")
        argv = ["kubectl"]
        if self.context and self.access != "in-cluster":
            argv += ["--context", self.context]
        if namespaced:
            argv += ["-n", self.namespace]
        return argv + list(args)

    def full_argv(self, args: Sequence[str], namespaced: bool = True, tty: bool = False) -> list[str]:
        k = self.kubectl_argv(args, namespaced)
        if self.access == "ssh":
            opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15"]
            return ["ssh", *opts, *(["-t"] if tty else []), self.ssh_host or "", shlex.join(k)]
        return k

    def _guard(self, args: Sequence[str]) -> None:
        verb = next((a for a in args if not a.startswith("-")), "")
        if self.read_only and verb in MUTATING_VERBS:
            raise ReadOnlyViolation(f"read-only Kube refused mutating verb {verb!r}")

    # ---- execution
    def run(self, args: Sequence[str], stdin: bytes | None = None, timeout: float | None = 120,
            namespaced: bool = True, check: bool = True) -> subprocess.CompletedProcess:
        self._guard(args)
        p = self.runner(self.full_argv(args, namespaced), stdin, timeout)
        if check and p.returncode != 0:
            raise KubeError(f"kubectl {' '.join(args[:3])} failed rc={p.returncode}: {(p.stderr or b'').decode(errors='replace')[-800:]}")
        return p

    def popen(self, args: Sequence[str], **kw) -> subprocess.Popen:
        """Streaming (logs -f, exec stream). Callers own the process."""
        self._guard(args)
        return subprocess.Popen(self.full_argv(args), **kw)

    # ---- helpers
    def apply(self, objs: Sequence[dict]) -> str:
        doc = {"apiVersion": "v1", "kind": "List", "items": list(objs)}
        return self.run(["apply", "-f", "-"], stdin=json.dumps(doc).encode()).stdout.decode()

    def create(self, obj: dict) -> bool:
        """Create; returns False if it already exists (idempotent dispatch)."""
        p = self.run(["create", "-f", "-"], stdin=json.dumps(obj).encode(), check=False)
        if p.returncode == 0:
            return True
        err = (p.stderr or b"").decode(errors="replace")
        if "AlreadyExists" in err:
            return False
        raise KubeError(f"create failed: {err[-800:]}")

    def get_json(self, kind: str, name: str | None = None, selector: str | None = None, check: bool = True) -> dict | None:
        args = ["get", kind] + ([name] if name else []) + (["-l", selector] if selector else []) + ["-o", "json"]
        p = self.run(args, check=False)
        if p.returncode != 0:
            err = (p.stderr or b"").decode(errors="replace")
            if "NotFound" in err and not check:
                return None
            raise KubeError(f"get {kind} {name or ''} failed: {err[-500:]}")
        return json.loads(p.stdout or b"{}")

    def delete(self, kind: str, name: str | None = None, selector: str | None = None, background: bool = True) -> None:
        args = ["delete", kind] + ([name] if name else []) + (["-l", selector] if selector else []) + \
               ["--ignore-not-found", "--wait=false"] + (["--cascade=background"] if background else [])
        self.run(args)

    def annotate_cm(self, name: str, key: str, value: str) -> None:
        patch = json.dumps({"data": {key: value}})
        self.run(["patch", "configmap", name, "--type", "merge", "-p", patch])

    def logs(self, pod: str, container: str | None = None, tail: int | None = None) -> str:
        args = ["logs", pod] + (["-c", container] if container else []) + (["--tail", str(tail)] if tail else [])
        return self.run(args, check=False).stdout.decode(errors="replace")

    def pods_for(self, selector: str) -> list[dict[str, Any]]:
        return (self.get_json("pods", selector=selector) or {}).get("items", [])
