import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import type { Report, Session, SessionSummary } from './types';
export const dataDir = resolve(/* turbopackIgnore: true */ process.env.AI_SESSION_DATA_DIR || (process.env.HDYAI_HOSTED==='1'?'/tmp/hdyai-hosted':'.local'));
mkdirSync(dataDir, { recursive: true, mode: 0o700 });
const globalDb = globalThis as typeof globalThis & { sessionDb?: DatabaseSync };
export function db() {
  if (!globalDb.sessionDb) {
    const d = new DatabaseSync(resolve(dataDir, 'sessions.sqlite'));
    d.exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=10000;
      CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT NOT NULL, fingerprint TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);`);
    globalDb.sessionDb = d;
  }
  return globalDb.sessionDb;
}
export function saveSession(s: Session) { db().prepare('INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, fingerprint=excluded.fingerprint').run(s.id, JSON.stringify(s), s.fingerprint); }
export function getSession(id: string): Session | null { const r = db().prepare('SELECT data FROM sessions WHERE id=?').get(id); return r ? JSON.parse(String(r.data)) : null; }
export function fingerprint(id: string) { return db().prepare('SELECT fingerprint FROM sessions WHERE id=?').get(id)?.fingerprint; }
export function listSessions(options:{allSources?:boolean}={}): SessionSummary[] {
  const entireOnly=!options.allSources&&getState('settings')?.nativeFallback===false;
  return db().prepare('SELECT data FROM sessions').all().map(r => {
    const { events: _events, path: _path, fingerprint: _fp, ...s } = JSON.parse(String(r.data)) as Session;
    return s;
  }).filter(s => !s.hidden && (s.userCount+s.assistantCount+s.toolCount)>0&&(!entireOnly||s.capture?.provider==='entire'||s.capture?.provider==='hooks')).sort((a,b) => b.updatedAt.localeCompare(a.updatedAt));
}
export function saveReport(r: Report) { db().prepare('INSERT INTO reports VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data').run(r.id, JSON.stringify(r), r.createdAt); }
export function getReport(id: string): Report | null { const r = db().prepare('SELECT data FROM reports WHERE id=?').get(id); return r ? JSON.parse(String(r.data)) : null; }
export function listReports() { return db().prepare('SELECT data FROM reports ORDER BY created_at DESC').all().map(r => { const {snapshot: _s, ...report} = JSON.parse(String(r.data)) as Report; return report; }); }
export function setState(key: string, value: unknown) { db().prepare('INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(key, JSON.stringify(value)); }
export function getState(key: string) { const r = db().prepare('SELECT value FROM state WHERE key=?').get(key); return r ? JSON.parse(String(r.value)) : null; }
