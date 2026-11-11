import type { Report, RunDetail, UsageDashboard } from '../types';
import report from './report.json';

export const RUN_ID = '12345678-1234-4234-8234-123456789abc';
export function makeUsage(): UsageDashboard {
  const totals = { submissions: 2, reported_tokens: 1200, admitted_tokens: 3000, llm_calls: 4, searches: 3,
    estimated_cost_usd: .01, runs_without_reported_usage: 1, runs_without_cost: 1, charged_tokens: 3000,
    reserved_tokens: 150000, charged_cost_usd: .01, reserved_cost_usd: null };
  return { period_start: '2026-11-10T00:00:00Z', resets_at: '2026-11-11T00:00:00Z', active_runs: 1,
    limits: { daily_runs: 20, daily_tokens: 1000000, daily_cost_usd: null, max_active_runs: 3 },
    remaining: { runs: 18, tokens: 847000, cost_usd: null, active_slots: 2 }, today: totals,
    lifetime: { ...totals, submissions: 5, reported_tokens: 9000 } };
}
export function makeRun(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: RUN_ID, topic: 'How does retrieval ground an answer?', instructions: null,
    depth: 'standard', status: 'completed', current_step: 'writer', progress: 1,
    iteration: 1, step_status: { planner: 'completed', search: 'completed', evidence: 'completed',
      verification: 'completed', writer: 'completed', retry: 'skipped' },
    created_at: '2026-11-10T09:00:00Z', updated_at: '2026-11-10T09:01:00Z',
    error: null, report: structuredClone(report) as Report, ...overrides,
  };
}
