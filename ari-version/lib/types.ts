export type Agent = 'codex' | 'claude';
export type EvidenceKind = 'user' | 'assistant' | 'tool_call' | 'tool_result' | 'metric';
export type Evidence = { id: string; sessionId: string; line: number; timestamp: string; kind: EvidenceKind; text: string; tool?: string; callId?: string; error?: boolean; truncated?: boolean };
export type TaskSpan = { id: string; start: string; end: string | null; aborted: boolean; reason: string | null };
// Input includes cache reads/writes; cache fields are subsets, never additional totals.
export type TokenTotals = { inputTokens: number; outputTokens: number; cachedInputTokens: number; cacheWriteInputTokens: number; totalTokens: number };
export type TokenUsageRecord = TokenTotals & { id: string; timestamp: string };
export type SessionTokenUsage = { records: TokenUsageRecord[]; finalOutputs: { id: string; timestamp: string }[]; finalOutputCountKnown: boolean };
export type Session = { id: string; nativeId: string; agent: Agent; title: string; cwd: string; path: string; startedAt: string; updatedAt: string; parentId: string | null; source: string; hidden: boolean; userCount: number; assistantCount: number; toolCount: number; tools: string[]; malformedLines: number; truncated: boolean; events: Evidence[]; fingerprint: string; tasks: TaskSpan[]; tokenUsage?: SessionTokenUsage; capture?: {provider:'entire'|'hooks';repository?:string;storage:'staged'|'checkpoint'|'live'|'snapshot'} };
export type SessionSummary = Omit<Session, 'events' | 'path' | 'fingerprint'>;
export type Finding = { dimension: string; rating: 'Strongly demonstrated' | 'Effective' | 'Needs support' | 'Insufficient evidence'; confidence: 'High' | 'Moderate' | 'Low'; assessment: string; evidenceIds: string[] };
export type Moment = { title: string; observation: string; attribution: 'Candidate' | 'Agent' | 'Shared' | 'Unknown'; evidenceIds: string[] };
export type ReportContent = { title: string; summary: string; findings: Finding[]; moments: Moment[]; opportunities: { title: string; suggestion: string; evidenceIds: string[] }[]; limitations: string[]; followUps: string[] };
export type AnalysisTelemetry = {stage:'preparing'|'agent'|'codex'|'validating'|'complete'|'failed';startedAt:string;finishedAt?:string;queueMs:number;prepareMs?:number;codexMs?:number;agentMs?:number;engine?:'eve';model?:string;reasoning?:'low';validateMs?:number;totalMs?:number;inputCharacters?:number;sampledRecords?:number;availableRecords?:number;inputTokens?:number;outputTokens?:number};
export type Report = { id: string; status: 'queued' | 'running' | 'complete' | 'failed'; createdAt: string; name: string; role: string; sessionIds: string[]; snapshot: Session[]; content: ReportContent | null; error: string | null; coverage: string[]; analysis?:AnalysisTelemetry; scorerMetrics?: ScorerMetrics };
// Scorer harness bridge (docs/spec/bridge-contract.md). Self-contained so tiles render without evidenceIndex.
export type ScorerEvidence = { id: string; sessionId: string; excerpt: string };
export type ScorerTile = { key: string; label: string; value: number | null; unit: 'share' | 'min' | 'count' | 'tokens_k' | 'usd'; display: string; sub?: string; baseline?: number | null; evidence: ScorerEvidence[] };
export type ScorerMetrics = { day: string; tz: string; source: 'scorer'; demo: boolean; verdict: string; tiles: ScorerTile[]; breakdown: { cCauses: Record<string, number>; costByModel: { model: string; usd: number; tokens: number }[] }; flat: Record<string, number | null>; lanes?: ScorerLane[]; interrupts?: ScorerInterrupt[] };
export type ScorerLane = { sessionId: string; agent: Agent; repo: string | null; branch: string | null; cwd: string | null };
export type ScorerInterrupt = { sessionId: string; at: string; evidenceId: string | null };
