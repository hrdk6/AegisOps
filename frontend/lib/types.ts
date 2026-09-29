export type Anomaly = { signal: string; value: number; baseline: number | null; threshold: number | null; reason: string; since: string };

export type Signals = {
  service: string; at: string; rps: number | null; rps_baseline: number | null; error_ratio: number | null; p95_ms: number | null;
  cpu_util: number | null; mem_util: number | null; throttle_ratio: number | null; db_pool_util: number | null;
  restarts_recent: number; oom_recent: number; crashloop_pods: number; ready: number; desired: number; status: string;
  anomalies: Anomaly[];
};

export type IncidentSummary = {
  id: string; title: string; severity: string; status: string; detected_at: string; onset_at: string | null;
  resolved_at: string | null; affected_services: string[]; root_service: string | null; category: string | null;
  confidence: number | null; outcome: string | null; summary: string; iteration: number; human_interventions: number;
  duration_seconds: number;
};

export type Hypothesis = {
  category: string; component: string; statement: string; supporting_evidence: string[]; contradicting_evidence: string[];
  confidence: number; trigger_change: string | null; edge: string | null; rule_score: number; source: string;
};

export type Diagnosis = {
  id: string; summary: string; hypotheses: Hypothesis[]; selected: Hypothesis | null; confidence: number;
  affected_services: string[]; blast_radius: string[]; uncertainty: string; method: string;
  model_metadata: Record<string, unknown>; created_at: string; iteration: number;
};

export type Candidate = {
  id: string; action_type: string; target: { kind: string; namespace: string; name: string }; params: Record<string, unknown>;
  rationale: string; expected_effect: string; affected_components: string[]; rollback_strategy: string; prerequisites: string[];
  efficacy: number; confidence: number; addresses: string; source: string; simulatable: boolean; policy_allowed: boolean | null;
  requires_approval: boolean | null; risk_level: string | null; risk_score: number | null; policy_reasons: string[]; utility: number;
};

export type Plan = { id: string; candidates: Candidate[]; selected_index: number | null; escalation_reason: string | null; created_at: string };

export type ActionView = {
  id: string; incident_id: string; action_type: string; target: { kind: string; name: string; namespace: string };
  params: Record<string, unknown>; phase: string; risk_level: string | null; risk_score: number | null; requires_approval: boolean;
  decision: { allowed?: boolean; requiresApproval?: boolean; riskLevel?: string; riskScore?: number; reasons?: string[];
    checks?: { name: string; passed: boolean; detail?: string }[] };
  message: string; revert_of: string | null; simulation_id: string | null; rationale: string; outcome: string | null;
  category: string | null; created_at: string; started_at: string | null; completed_at: string | null;
};

export type SimMetrics = { requests: number; errors: number; errorRate: number; p50Ms: number; p95Ms: number; p99Ms: number;
  throughputRps: number; cpuMillicores: number; memoryMb: number; memoryGrowthMb: number };

export type Simulation = {
  id: string; action_type: string; target: { name: string }; phase: string; verdict: string | null; baseline: SimMetrics | null;
  candidate: SimMetrics | null; summary: string; reason: string; created_at: string;
};

export type Check = { name: string; service: string | null; passed: boolean; before: number | null; after: number | null;
  threshold: number | null; detail: string };
export type Verification = { id: string; action_id: string; outcome: string; checks: Check[]; summary: string; completed_at: string };

export type Approval = { id: string; incident_id: string; incident_title?: string | null; severity?: string | null; status: string;
  requested_at: string; decided_at: string | null; decided_by: string | null; reason: string | null;
  context: { candidate?: Candidate; diagnosis?: string; confidence?: number; evidence?: string[]; simulation?: string | null;
    decision?: ActionView["decision"] } };

export type IncidentDetail = IncidentSummary & {
  symptoms: { service: string; signal: string; value: number; reason: string; since: string }[];
  diagnosis: Diagnosis | null; plan: Plan | null; actions: ActionView[]; approvals: Approval[]; simulations: Simulation[];
  verifications: Verification[]; has_postmortem: boolean; evidence_count: number;
};

export type TimelineEvent = { id: number; ts: string; type: string; message: string; actor: string; data: Record<string, unknown> };

export type Evidence = {
  id: string; kind: string; service: string | null; signal: string | null; title: string; summary: string;
  data: Record<string, unknown> & { series?: [number, number][] }; source: { system: string; query?: string | null; url?: string | null };
  observed_at: string; score: number; iteration: number;
};

export type GraphNode = { name: string; tier?: string | null; status?: string; replicas?: number; ready?: number };
export type GraphEdge = { from: string; to: string; declared: boolean; observedMetrics: boolean; observedTraces: boolean; status: string };
export type Topology = { nodes: GraphNode[]; edges: GraphEdge[]; rootCandidates?: string[] };
