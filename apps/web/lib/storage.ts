import { getAuth } from "./auth";
import type { ChatAnswer } from "./types";

export type SessionCache = {
  query: string;
  answer: ChatAnswer;
  timestamp?: string;
};

function getUserId(): string {
  const auth = getAuth();
  return auth?.user_id || "anonymous";
}

export function getRecentKey(): string {
  return `kr-law-rag/recent-questions/${getUserId()}`;
}

export function getSessionCacheKey(): string {
  return `kr-law-rag/last-session/${getUserId()}`;
}

export function getHistoryKey(): string {
  return `kr-law-rag/history/${getUserId()}`;
}

export function loadHistory(): SessionCache[] {
  try {
    const raw = window.localStorage.getItem(getHistoryKey());
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

export function saveToHistory(item: SessionCache): SessionCache[] {
  const existing = loadHistory();
  const withTimestamp = { ...item, timestamp: new Date().toISOString() };
  const next = [
    withTimestamp,
    ...existing.filter((h) => h.query !== item.query),
  ].slice(0, 20);
  try {
    window.localStorage.setItem(getHistoryKey(), JSON.stringify(next));
  } catch {}
  return next;
}
