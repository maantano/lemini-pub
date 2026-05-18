import { getAuth } from "./auth";
import type { ChatAnswer, SavedAnswer } from "./types";

/** 사용자별 저장된 답변의 localStorage 키를 생성한다. */
function getSavedKey(): string {
  const auth = getAuth();
  const userId = auth?.user_id || "anonymous";
  return `kr-law-rag/saved-answers/${userId}`;
}

function readSavedAnswers(): SavedAnswer[] {
  if (typeof window === "undefined") {
    return [];
  }
  const raw = window.localStorage.getItem(getSavedKey());
  if (!raw) {
    return [];
  }
  return JSON.parse(raw) as SavedAnswer[];
}

/** 답변을 localStorage에 저장한다. 중복 확인 → 최대 30개 유지. */
export function appendSavedAnswer(question: string, answer: ChatAnswer): SavedAnswer[] {
  const existing = readSavedAnswers();
  const isDuplicate = existing.some(
    (item) => item.question === question && item.answer === answer.answer,
  );
  if (isDuplicate) {
    return existing;
  }
  const auth = getAuth();
  const next: SavedAnswer = {
    id: `${Date.now()}`,
    user_id: auth?.user_id ?? null,
    question,
    summary: answer.summary,
    answer: answer.answer,
    citations: answer.citations,
    created_at: new Date().toISOString(),
  };
  const items = [next, ...existing].slice(0, 30);
  window.localStorage.setItem(getSavedKey(), JSON.stringify(items));
  return items;
}
