/** Public FastAPI contracts. Provider configuration never enters the dashboard. */
export type Depth = 'quick' | 'standard' | 'deep';
export type RunStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
export type StepStatus = 'pending' | 'completed' | 'partial' | 'failed' | 'skipped';
export interface Source {
  source_id: string;
  original_url: string;
  resolved_url: string | null;
  title: string;
  content: string;
  retrieved_at: string;
  published_at: string | null;
  modified_at: string | null;
}
export interface Evidence { evidence_id: string; source_id: string; excerpt: string; note?: string }
export interface Claim {
  claim_id: string;
  claim: string;
  verdict: 'supported' | 'contradicted' | 'insufficient_evidence';
  rationale: string;
  evidence_ids: string[];
  passage_assessments?: { evidence_id: string; relation: 'supports' | 'contradicts' | 'irrelevant'; explanation: string }[];
}
export interface Report {
  query: string;
  title: string;
  status: 'complete' | 'partial' | 'insufficient_evidence';
  body_markdown: string;
  sources: Source[];
  evidence: Evidence[];
  claims: Claim[];
  citations: Record<string, string>;
  uncertainties: string[];
  limitations: string[];
  research_iteration: number;
  usage: Record<string, number | string | null>;
}
export interface RunSummary {
  id: string;
  topic: string;
  instructions: string | null;
  depth: Depth;
  status: RunStatus;
  current_step: string | null;
  progress: number;
  iteration: number;
  step_status: Record<string, StepStatus>;
  created_at: string;
  updated_at: string;
  error: string | null;
  error_code?: string | null;
  resume_count?: number;
}
export interface RunDetail extends RunSummary { report: Report | null; submission_reused?: boolean; resumable?: boolean; resume_blocked_reason?: string | null }
export interface RunPage { items: RunSummary[]; total: number; limit: number; offset: number }
export interface CreateResearch { topic: string; instructions: string | null; depth: Depth }
export interface User { id: string; email: string; name: string; is_admin?: boolean }
export interface AuthSession { user: User; csrf_token: string }
export interface HistoryFilters { q: string; status: RunStatus | ''; depth: Depth | ''; created_from: string; created_to: string }
export interface UsageTotals {
  submissions: number; reported_tokens: number | null; admitted_tokens: number; llm_calls: number; searches: number;
  estimated_cost_usd: number | null; runs_without_reported_usage: number; runs_without_cost: number;
  charged_tokens: number; reserved_tokens: number; charged_cost_usd: number | null; reserved_cost_usd: number | null;
}
export interface UsageDashboard {
  period_start: string; resets_at: string; active_runs: number;
  limits: { daily_runs: number; daily_tokens: number; daily_cost_usd: number | null; max_active_runs: number };
  remaining: { runs: number; tokens: number; cost_usd: number | null; active_slots: number };
  today: UsageTotals; lifetime: UsageTotals;
}
export interface AdminOverview {
  counts: Record<RunStatus, number>; oldest_queued_seconds: number; failed_last_24h: number;
  insufficient_last_24h: number; unknown_provider_calls: number; day_start: string;
  estimated_cost_usd: number | null; estimated_reserved_exposure_usd: number | null;
  unknown_cost_runs: number; spending_alert_usd: number;
  alerts: { code: string; severity: string; message: string }[];
}
export interface AdminRun { id: string; status: RunStatus; depth: Depth; current_step: string | null; progress: number; created_at: string; error_code: string | null; resume_count: number }
export interface AdminRunPage { items: AdminRun[]; total: number; limit: number; offset: number }
