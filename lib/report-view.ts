import type { Evidence, Report, TaskSpan, Agent } from './types';
import type { metrics } from './metrics';
type TokenMetricKeys = 'tokenTotals' | 'tokenUsageSessions' | 'finalOutputSessions' | 'finalOutputs' | 'tokenMetricSessions' | 'tokenMetricTotalTokens' | 'tokenMetricFinalOutputs' | 'tokensPerFinalOutput';
export type ReportMetrics = Omit<ReturnType<typeof metrics>, TokenMetricKeys> & Partial<Pick<ReturnType<typeof metrics>, TokenMetricKeys>>;
export type ReportView=Omit<Report,'snapshot'> & {metrics?:ReportMetrics;timezone?:string;activity?:{id:string;agent:Agent;title:string;tasks:TaskSpan[];userCount:number;toolCount:number}[];evidenceIndex?:(Evidence & {sessionTitle:string;agent:Agent})[]};
