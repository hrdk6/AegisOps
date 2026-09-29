"""Post-remediation verification.

After the controller reports the action complete, the verifier observes
detector cycles (event-driven, no fixed sleeps) until the affected services
have been healthy for a sustained streak or the timeout expires. It then
compares SLO checks before vs after the action:

  RESOLVED   every check passes
  PARTIAL    violation score fell by >= 40% but checks still fail
  DEGRADED   violation score rose by > 20%
  NO_EFFECT  otherwise
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime

from aegis.domain.enums import VerificationOutcome
from aegis.domain.schemas import ServiceSignals, Verification, VerificationCheck
from aegis.engine.detector import Detector
from aegis.engine.slo import SLOBook

HEALTHY_STREAK = 4


def badness(s: ServiceSignals | None, slos: SLOBook) -> float:
    """Normalized SLO violation score for one service (0 = healthy)."""
    if s is None:
        return 0.0
    slo = slos.get(s.service)
    score = 0.0
    if s.error_ratio is not None and s.error_ratio > slo.max_error_ratio:
        score += min(5.0, s.error_ratio / max(slo.max_error_ratio, 1e-6) / 5)
    if s.p95_ms is not None and s.p95_ms > slo.p95_ms:
        score += min(5.0, s.p95_ms / slo.p95_ms - 1)
    if s.desired > 0 and s.ready < s.desired:
        score += (s.desired - s.ready) / s.desired * 2
    if s.desired == 0 and any(a.signal == "scaled_to_zero" for a in s.anomalies):
        score += 3.0
    score += 1.0 * bool(s.crashloop_pods) + 1.0 * bool(s.oom_recent)
    if s.cpu_util is not None and s.cpu_util > slo.cpu_util:
        score += 0.5
    if s.mem_util is not None and s.mem_util > slo.mem_util:
        score += 0.5
    if any(a.signal == "db_pool_saturation" for a in s.anomalies):
        score += 1.0
    return round(score, 4)


def checks_for(service: str, before: ServiceSignals | None, after: ServiceSignals | None,
               slos: SLOBook) -> list[VerificationCheck]:
    slo = slos.get(service)
    if after is None:
        return [VerificationCheck(name="telemetry", service=service, passed=False, detail="no signals after action")]
    out = []
    if after.rps is not None or slo.service in slos.services:
        out.append(VerificationCheck(name="error_ratio", service=service,
                                     passed=after.error_ratio is None or after.error_ratio <= slo.max_error_ratio,
                                     before=before.error_ratio if before else None, after=after.error_ratio,
                                     threshold=slo.max_error_ratio))
        out.append(VerificationCheck(name="latency_p95_ms", service=service,
                                     passed=after.p95_ms is None or after.p95_ms <= slo.p95_ms,
                                     before=before.p95_ms if before else None, after=after.p95_ms, threshold=slo.p95_ms))
    out.append(VerificationCheck(name="availability", service=service, passed=after.desired > 0 and after.ready >= after.desired,
                                 before=float(before.ready) if before else None, after=float(after.ready),
                                 threshold=float(after.desired), detail=f"{after.ready}/{after.desired} ready"))
    out.append(VerificationCheck(name="saturation", service=service,
                                 passed=(after.cpu_util or 0) <= slo.cpu_util and (after.mem_util or 0) <= slo.mem_util,
                                 before=before.cpu_util if before else None, after=after.cpu_util,
                                 detail=f"cpu {after.cpu_util}, mem {after.mem_util}"))
    out.append(VerificationCheck(name="stability", service=service, passed=not after.crashloop_pods and not after.oom_recent,
                                 detail=f"crashloop pods {after.crashloop_pods}, OOM {after.oom_recent}"))
    anomalies = [a.signal for a in after.anomalies]
    out.append(VerificationCheck(name="no_active_anomalies", service=service, passed=not anomalies,
                                 detail=", ".join(anomalies) or "none"))
    return out


class Verifier:
    def __init__(self, detector: Detector, slos: SLOBook, timeout_s: float, min_settle_s: float) -> None:
        self.detector = detector
        self.slos = slos
        self.timeout_s = timeout_s
        self.min_settle_s = min_settle_s

    async def verify(self, incident_id: str, action_id: str, services: list[str],
                     before: dict[str, ServiceSignals]) -> Verification:
        started = datetime.now(UTC)
        t0 = time.monotonic()
        streak = 0
        while time.monotonic() - t0 < self.timeout_s:
            await self.detector.next_cycle(timeout=15)
            latest = self.detector.latest
            ok = all(latest.get(s) is not None and not latest[s].anomalies for s in services)
            if time.monotonic() - t0 >= self.min_settle_s:
                streak = streak + 1 if ok else 0
                if streak >= HEALTHY_STREAK:
                    break
        after = self.detector.latest
        checks = [c for s in services for c in checks_for(s, before.get(s), after.get(s), self.slos)]
        b_score = sum(badness(before.get(s), self.slos) for s in services)
        a_score = sum(badness(after.get(s), self.slos) for s in services)
        if all(c.passed for c in checks):
            outcome = VerificationOutcome.RESOLVED
        elif a_score > b_score * 1.2 + 0.1:
            outcome = VerificationOutcome.DEGRADED
        elif b_score > 0 and a_score <= b_score * 0.6:
            outcome = VerificationOutcome.PARTIAL
        else:
            outcome = VerificationOutcome.NO_EFFECT
        failed = [f"{c.service}:{c.name}" for c in checks if not c.passed]
        summary = (f"{outcome.value}: violation score {b_score:.2f} -> {a_score:.2f}; "
                   + ("all SLO checks pass" if not failed else "failing: " + ", ".join(failed[:6])))
        return Verification(id="ver-" + secrets.token_hex(4), incident_id=incident_id, action_id=action_id, outcome=outcome,
                            checks=checks, summary=summary, started_at=started, completed_at=datetime.now(UTC))
