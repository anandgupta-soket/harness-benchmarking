"""Outcome classification. Pure; the controller applies it after verification and after
reading the external pod status (the only source of truth for whole-pod OOM kills).

Outcomes: passed | incorrect | oom | timeout | harness_error | provider_error | infra_error | cancelled

Precedence (documented in docs/kube-benchmark.md):
  cancelled > infra_error > timeout > passed > oom > provider_error > harness_error > incorrect

* infra_error: the measurement itself is untrustworthy (worker crash, cgroup hierarchy invalid,
  pod evicted/node loss, artifact collection interrupted without a result).
* timeout: the task's declared timeout fired; never rescued by a verifier pass.
* passed: trusted verifier passed (kernel oom events / agent non-zero exit are kept as flags).
* oom: container OOM-killed (external pod status) or cgroup oom_kill delta > 0, and not passed.
* provider_error: the model endpoint failed (HTTP 5xx/429, connection refused, upstream_error ...).
* harness_error: agent binary missing / crashed / non-zero exit with no provider signature.
* incorrect: the agent finished but the verifier failed (including unsafe submissions).
"""
from __future__ import annotations

import re
from typing import Any

PROVIDER_PATTERNS = re.compile(
    r"(HTTP[ /]?(5\d\d|429)|\b(502|503|504|429)\b.{0,40}(error|unavailable|upstream|rate)|upstream_error|"
    r"model is unavailable|rate.?limit|ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENOTFOUND|connection (refused|reset)|"
    r"Transport error|error sending request|error decoding response|fetch failed|socket hang up|"
    r"overloaded|insufficient_quota|invalid_api_key|401 Unauthorized|APIConnectionError|Service Unavailable)", re.I)


def looks_like_provider_error(*texts: str) -> bool:
    return any(t and PROVIDER_PATTERNS.search(t) for t in texts)


def pod_oom_killed(pod_status: dict[str, Any] | None) -> bool:
    """True if any container terminated (or last terminated) with reason OOMKilled."""
    if not pod_status:
        return False
    for cs in (pod_status.get("status", pod_status).get("containerStatuses") or []):
        for st in (cs.get("state", {}), cs.get("lastState", {})):
            t = st.get("terminated")
            if t and t.get("reason") == "OOMKilled":
                return True
    return False


def pod_failure_reason(pod_status: dict[str, Any] | None) -> str | None:
    if not pod_status:
        return None
    st = pod_status.get("status", pod_status)
    if st.get("reason"):                       # Evicted, NodeLost, DeadlineExceeded ...
        return str(st["reason"])
    for cs in (st.get("containerStatuses") or []):
        w = cs.get("state", {}).get("waiting")
        if w and w.get("reason") in ("ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "InvalidImageName"):
            return str(w["reason"])
    return None


def classify(result: dict[str, Any] | None, verification: dict[str, Any] | None,
             pod_status: dict[str, Any] | None = None, job_failed_reason: str | None = None,
             cancelled: bool = False) -> tuple[str, str]:
    """Return (outcome, detail)."""
    if cancelled:
        return "cancelled", "cancelled by operator"
    ext_oom = pod_oom_killed(pod_status)
    if result is None or not result.get("complete"):
        # No (complete) worker result: only the external view is available.
        if ext_oom:
            return "oom", "container OOMKilled (external pod status); no worker result"
        r = pod_failure_reason(pod_status) or job_failed_reason
        if r == "DeadlineExceeded":
            return "timeout", "Job activeDeadlineSeconds exceeded before worker finished"
        return "infra_error", f"no complete worker result ({r or 'pod ended without result'})"
    if result.get("worker_error"):
        return "infra_error", f"worker error: {result['worker_error']}"
    if not result.get("cgroup", {}).get("valid", False):
        return "infra_error", "cgroup hierarchy failed validation; resource metrics not trustworthy"
    if result.get("timed_out"):
        return "timeout", f"task timeout {result.get('timeout_s')}s reached; process tree killed"
    passed = bool(verification and verification.get("passed"))
    if passed:
        return "passed", "verifier passed"
    oom_events = (result.get("cgroup", {}).get("oom_kill_delta") or 0) > 0
    if ext_oom or oom_events:
        return "oom", "OOM kill observed (cgroup oom_kill delta=%s, pod OOMKilled=%s)" % (
            result.get("cgroup", {}).get("oom_kill_delta"), ext_oom)
    ag = result.get("agent", {})
    text = " ".join(str(ag.get(k) or "") for k in ("stderr_tail", "stdout_tail"))
    if ag.get("launch_failed"):
        return "harness_error", f"could not launch harness: {ag.get('launch_error')}"
    if ag.get("exit_code") not in (0, None) or ag.get("signal"):
        if looks_like_provider_error(text):
            return "provider_error", "harness exited non-zero with provider-error signature"
        return "harness_error", f"harness exited code={ag.get('exit_code')} signal={ag.get('signal')}"
    if looks_like_provider_error(text) and not (result.get("usage") or {}).get("total_tokens"):
        return "provider_error", "provider-error signature and no token usage recorded"
    if verification is None:
        return "infra_error", "verification missing"
    if verification.get("errors") and not verification.get("checks"):
        return "infra_error", "verifier itself errored: %s" % verification["errors"][:2]
    return "incorrect", "verifier failed: " + ",".join(c.get("name", "?") for c in verification.get("checks", []) if not c.get("passed"))[:300]
