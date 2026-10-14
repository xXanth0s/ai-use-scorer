import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { Report, Session } from '../lib/types';
import { analystTools, fromLocalReport, fromPublishedReport, getEvidence, listSessions, queryMetrics, searchEvidence } from '../lib/analyst';

const session = { id: 'demo-a', nativeId: 'demo-a', agent: 'claude', title: 'Add --since', cwd: '/w', path: '', startedAt: '2026-10-14T10:00:00Z', updatedAt: '2026-10-14T11:00:00Z', parentId: null, source: 'scorer', hidden: false, userCount: 1, assistantCount: 0, toolCount: 1, tools: ['Bash'], malformedLines: 0, truncated: false, fingerprint: 'x',
  events: [
    { id: 'demo-a:3000', sessionId: 'demo-a', line: 3000, timestamp: '2026-10-14T10:00:00Z', kind: 'user', text: 'add --since to ingest and run pytest' },
    { id: 'demo-a:3001', sessionId: 'demo-a', line: 3001, timestamp: '2026-10-14T10:01:00Z', kind: 'tool_call', tool: 'Bash', text: 'uv run pytest -q' },
    { id: 'metric:42', sessionId: 'demo-a', line: 9000042, timestamp: '2026-10-14T23:00:00Z', kind: 'metric', text: 'V = 0.67 share (n=6)' + 'x'.repeat(2000) },
  ],
  tasks: [{ id: 'demo-a:t3', start: '2026-10-14T10:00:00Z', end: '2026-10-14T10:05:00Z', aborted: false, reason: 'turn_duration' }] } as unknown as Session;
const report = { id: 'scorer-demo-2026-10-14', status: 'complete', createdAt: '2026-10-14T23:00:00Z', name: 'scorer demo · 2026-10-14', role: 'self · 2026-10-14', sessionIds: ['demo-a'], snapshot: [session], error: null, coverage: [],
  content: { title: 't', summary: 's', findings: [{ dimension: 'Verification judgment', rating: 'Effective', confidence: 'Moderate', assessment: 'ok', evidenceIds: ['metric:42'] }], moments: [], opportunities: [], limitations: [], followUps: [] },
  scorerMetrics: { tiles: [{ key: 'V', evidence: [{ id: 'demo-a:3000', sessionId: 'demo-a', excerpt: 'add --since' }] }], flat: { V: 0.67 } } } as unknown as Report;

test('analyst tools read sessions, metrics and evidence from a local report', async () => {
  const src = fromLocalReport(report);
  assert.deepEqual(listSessions(src).sessions.map(s => [s.id, s.tasks, s.toolCount]), [['demo-a', 1, 1]]);
  const m = queryMetrics(src) as unknown as { scorerMetrics: { flat: { V: number } }; reportMetrics: { sessions: number }; findings: { evidenceIds: string[] }[] };
  assert.equal(m.scorerMetrics.flat.V, 0.67);
  assert.equal(m.reportMetrics.sessions, 1);
  assert.deepEqual(m.findings[0].evidenceIds, ['metric:42']);
  const [metric, missing] = getEvidence(src, ['metric:42', 'nope:1']) as { kind?: string; text?: string; error?: string }[];
  assert.equal(metric.kind, 'metric');
  assert.ok(metric.text!.length <= 1201);
  assert.equal(missing.error, 'unknown evidence id');
  assert.deepEqual(searchEvidence(src, 'PYTEST').map(e => e.id), ['demo-a:3000', 'demo-a:3001']);
  assert.deepEqual(searchEvidence(src, 'pytest', 'tool_call').map(e => e.id), ['demo-a:3001']);
  const tools = analystTools(src);
  assert.deepEqual(Object.keys(tools).sort(), ['get_evidence', 'list_sessions', 'query_metrics', 'search_evidence']);
});

test('analyst source from a hosted published report uses evidenceIndex and scorer tile evidence', () => {
  const src = fromPublishedReport({ id: 'r', name: 'n', role: 'self', content: null, scorerMetrics: report.scorerMetrics as unknown,
    activity: [{ id: 'demo-a', agent: 'claude', title: 'Add --since', tasks: [{}], userCount: 1, toolCount: 1 }],
    evidenceIndex: [{ id: 'demo-a:3001', sessionId: 'demo-a', sessionTitle: 'Add --since', kind: 'tool_call', timestamp: 't', text: 'uv run pytest -q' }] });
  assert.deepEqual([...src.evidence.keys()].sort(), ['demo-a:3000', 'demo-a:3001']);
  assert.equal(listSessions(src).sessions[0].assistantCount, null);
});
