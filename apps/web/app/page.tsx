"use client";

import { FormEvent, startTransition, useEffect, useRef, useState } from "react";
import { SiteShell } from "../components/site-shell";
import { apiRequest, apiRequestFormData, streamChat, API_BASE_URL } from "../lib/api";
import { getAuth } from "../lib/auth";
import { appendSavedAnswer } from "../lib/local-saved";
import {
  examplePrompts,
  trustBadges,
  ANALYSIS_STEPS,
  ANALYSIS_STEP_DURATIONS_SEC,
} from "../lib/constants";
import {
  getRecentKey,
  getSessionCacheKey,
  loadHistory,
  saveToHistory,
  type SessionCache,
} from "../lib/storage";
import type {
  ChatAnswer,
  FileReviewResponse,
  IntakeAnalysis,
  PrecedentDetailResponse,
} from "../lib/types";
import { CitationsList } from "../components/citations-list";

/* ── basis 렌더 헬퍼 (문자열/구조화 객체 둘 다 지원) ───────── */
function formatBasis(basis: unknown): string {
  if (!basis) return "";
  if (typeof basis === "string") return basis;
  if (typeof basis !== "object") return String(basis);
  const b = basis as Record<string, any>;
  const parts: string[] = [];
  for (const s of (b.statutes ?? []) as any[]) {
    const name = s?.name ?? "";
    const article = s?.article ?? "";
    const line = [name, article].filter(Boolean).join(" ");
    if (line) parts.push(line);
  }
  for (const c of (b.cases ?? []) as any[]) {
    const court = c?.court ?? "";
    const date = c?.date ?? "";
    const no = c?.case_no ?? "";
    const line = [court, date && `${date} 선고`, no && `${no} 판결`]
      .filter(Boolean)
      .join(" ");
    if (line) parts.push(line);
  }
  for (const o of (b.ordinances ?? []) as any[]) {
    const issuer = o?.issuer ?? "";
    const title = o?.title ?? "";
    const num = o?.number ?? "";
    const art = o?.article ?? "";
    const line = [issuer, title, num, art].filter(Boolean).join(" ");
    if (line) parts.push(line);
  }
  for (const v of (b.voluntary_codes ?? []) as any[]) {
    const issuer = v?.issuer ?? "";
    const title = v?.title ?? "";
    const art = v?.article ?? "";
    const line = [issuer, title, art].filter(Boolean).join(" ");
    if (line) parts.push(line);
  }
  return parts.join(" · ");
}

/* ── Deep-dive types ──────────────────────────────────────── */

type DeepDiveInputType =
  | "single_choice"
  | "multi_choice"
  | "yes_no"
  | "free_text"
  // 레거시 호환 — 구 프롬프트에서 내려올 수 있음
  | "choice"
  | "text";

type DeepDiveQuestion = {
  id: string;
  question: string;
  input_type: DeepDiveInputType;
  options?: string[];
  allow_free_text?: boolean;
  legal_reason: string;
};

type DeepDiveFollowUp = {
  type: "follow_up";
  message: string;
  questions: DeepDiveQuestion[];
  turn_index?: number;
  max_turns?: number;
  thread_id?: string;
  gatheredFacts?: Record<string, string>;
};

// 이제 /v1/deep-dive analysis 응답은 /v1/analyze-text 와 동일한 FileReviewResponse 구조.
type DeepDiveAnalysis = {
  type: "analysis";
  thread_id?: string;
  domain?: string;
  gatheredFacts?: Record<string, string>;
  turn_index?: number;
  force_analyzed?: boolean;
} & Partial<FileReviewResponse>;

type DeepDiveResponse = DeepDiveFollowUp | DeepDiveAnalysis;

type DeepDiveAnswerPayload = {
  question_id: string;
  question_text: string;
  input_type: DeepDiveInputType;
  selected_options: string[];
  free_text?: string | null;
};

type ConversationMessage = {
  role: "user" | "assistant";
  content: string;
};

/* ── Page ──────────────────────────────────────────────────── */

type UsageInfo = { used: number; limit: number; logged_in: boolean };

export default function HomePage() {
  const [query, setQuery] = useState("");
  const [answer, setAnswer] = useState<ChatAnswer | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);
  const [recentQuestions, setRecentQuestions] = useState<string[]>([]);
  const [usage, setUsage] = useState<UsageInfo | null>(null);
  const [showLimitModal, setShowLimitModal] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [history, setHistory] = useState<SessionCache[]>([]);
  const [expandedPrecedent, setExpandedPrecedent] = useState<string | null>(
    null,
  );
  const [precedentDetail, setPrecedentDetail] =
    useState<PrecedentDetailResponse | null>(null);
  const [precedentLoading, setPrecedentLoading] = useState(false);
  const [showMorePrecedents, setShowMorePrecedents] = useState(false);
  const [precedentPage, setPrecedentPage] = useState(0);
  const [feedbackSent, setFeedbackSent] = useState<"up" | "down" | null>(null);

  // Deep-dive conversation state
  const [deepDiveMode, setDeepDiveMode] = useState(false);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [conversation, setConversation] = useState<ConversationMessage[]>([]);
  const [followUp, setFollowUp] = useState<{
    message: string;
    questions: DeepDiveQuestion[];
    turn_index?: number;
    max_turns?: number;
    gatheredFacts?: Record<string, string>;
  } | null>(null);
  // follow-up 질문 묶음에 대한 사용자 응답을 일괄 수집(question_id → 응답) 하는 로컬 상태.
  // [다음] 클릭 시 서버로 한 번에 전송된다.
  const [followUpDraft, setFollowUpDraft] = useState<
    Record<
      string,
      { selected: string[]; freeText: string }
    >
  >({});
  const [analysis, setAnalysis] = useState<IntakeAnalysis | null>(null);
  const [deepDiveLoading, setDeepDiveLoading] = useState(false);
  const [analysisStep, setAnalysisStep] = useState("");
  const [analysisElapsedSec, setAnalysisElapsedSec] = useState(0);
  const [documentDraft, setDocumentDraft] = useState<{
    title: string;
    content: string;
    instructions: string;
    estimated_cost: string;
    disclaimer: string;
  } | null>(null);
  const [draftLoading, setDraftLoading] = useState(false);

  // File attachment state
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [isDraggingOver, setIsDraggingOver] = useState<boolean>(false);
  const [fileReview, setFileReview] = useState<any>(null);
  const [fileReviewLoading, setFileReviewLoading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const followupFileInputRef = useRef<HTMLInputElement>(null);
  const [forceQuickAnswer, setForceQuickAnswer] = useState(false);
  const [autoRoutedToReview, setAutoRoutedToReview] = useState(false);
  const [followupText, setFollowupText] = useState("");
  const [followupMode, setFollowupMode] = useState<"chat" | "review">("chat");

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const answerRef = useRef<HTMLDivElement>(null);

  const hasAnswer = answer !== null;

  /** 사용량 조회 (GET /v1/usage) — 남은 질문 횟수 표시용 */
  function fetchUsage() {
    apiRequest<UsageInfo>("/v1/usage")
      .then(setUsage)
      .catch(() => {});
  }

  useEffect(() => {
    fetchUsage();
    setHistory(loadHistory());

    const stored = window.localStorage.getItem(getRecentKey());
    if (stored) setRecentQuestions(JSON.parse(stored));

    const params = new URLSearchParams(window.location.search);
    const isNew = params.get("new");
    const qParam = params.get("q");

    if (isNew) {
      try {
        window.localStorage.removeItem(getSessionCacheKey());
      } catch {}
      window.history.replaceState({}, "", "/");
    } else if (qParam) {
      setQuery(qParam);
      window.history.replaceState({}, "", "/");
      submitQuestion(qParam);
    } else {
      const auth = getAuth();
      if (auth) {
        const cached = window.localStorage.getItem(getSessionCacheKey());
        if (cached) {
          try {
            const session: SessionCache = JSON.parse(cached);
            setQuery(session.query);
            setAnswer(session.answer);
            setSearched(true);
          } catch {
            /* ignore corrupt cache */
          }
        }
      }
    }
  }, []);

  /** 최근 질문 목록을 갱신하고 localStorage에 저장한다 (최대 6개) */
  function persistRecentQuestions(q: string) {
    const next = [q, ...recentQuestions.filter((item) => item !== q)].slice(
      0,
      6,
    );
    setRecentQuestions(next);
    window.localStorage.setItem(getRecentKey(), JSON.stringify(next));
  }

  /** 분석 단계 애니메이션 — 각 단계별 실측 기반 소요 시간(ANALYSIS_STEP_DURATIONS_SEC)으로 전환.
   * 서버가 단계별 이벤트를 내려주지 않는 현재 구조에서 자연스러운 체감을 위한 타이밍.
   * 동시에 1초 tick으로 경과시간(analysisElapsedSec)을 갱신하여 UI에서 mm:ss 표시.
   * cleanup 함수 반환(호출 시 모든 타이머·인터벌 취소). */
  function animateAnalysisSteps(): () => void {
    setAnalysisStep(ANALYSIS_STEPS[0]);
    setAnalysisElapsedSec(0);
    const startedAt = Date.now();

    const timeouts: ReturnType<typeof setTimeout>[] = [];
    let cumulative = 0;
    for (let i = 0; i < ANALYSIS_STEPS.length - 1; i++) {
      cumulative += ANALYSIS_STEP_DURATIONS_SEC[i] * 1000;
      const nextIdx = i + 1;
      timeouts.push(
        setTimeout(() => {
          setAnalysisStep(ANALYSIS_STEPS[nextIdx]);
        }, cumulative),
      );
    }

    const tickInterval = setInterval(() => {
      setAnalysisElapsedSec(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);

    return () => {
      timeouts.forEach(clearTimeout);
      clearInterval(tickInterval);
    };
  }

  /** 초 → mm:ss 포맷 (예: 73 → "01:13"). */
  function formatMMSS(totalSec: number): string {
    const safe = Math.max(0, Math.floor(totalSec));
    const mm = Math.floor(safe / 60).toString().padStart(2, "0");
    const ss = (safe % 60).toString().padStart(2, "0");
    return `${mm}:${ss}`;
  }

  // 분석 총 예상 시간(초). 단계 합 + 약간의 후처리 여유.
  const ANALYSIS_TOTAL_ETA_SEC = ANALYSIS_STEP_DURATIONS_SEC.reduce(
    (a, b) => a + b,
    0,
  );

  /**
   * 심층 검토 자동 분기 판단.
   * 600자 이상 + 구조적 신호(번호 매김, 항목 기호, 검토 키워드, 헤더 패턴) 2개 이상이면 true.
   * 계약서 검토 같은 긴 구조적 입력을 자동으로 심층 검토 파이프라인으로 라우팅한다.
   */
  function shouldUseDeepReview(q: string): boolean {
    if (forceQuickAnswer) return false;
    const text = q.trim();
    let signals = 0;
    // 번호 매김 (1. 2. 3. 또는 ① ② ③)
    if (/(^|\n)\s*(\d+\.|[①②③④⑤⑥⑦⑧⑨⑩])\s/.test(text)) signals++;
    // 항목 기호
    if (/(^|\n)\s*[-•·*]\s/.test(text)) signals++;
    // 검토 의도 키워드
    if (
      /(검토.{0,5}(요청|부탁|드립|필요)|법률.{0,3}검토|리스크|법령.{0,5}저촉|해당.{0,5}여부|가능성|의견을?\s*요청)/.test(
        text,
      )
    )
      signals++;
    // 헤더 패턴 (X. 제목)
    if ((text.match(/(^|\n)\s*\d+\.\s*[가-힣A-Za-z]/g) || []).length >= 3)
      signals++;
    return signals >= 2;
  }

  /**
   * 공통 deep-dive 호출 — 메시지 + 이번 턴 answers 를 서버로 전송하고
   * follow_up / analysis 응답을 분기 처리한다.
   */
  async function callDeepDive(args: {
    message: string;
    answers: DeepDiveAnswerPayload[];
    forceAnalyze?: boolean;
    persistQuery?: string;
  }) {
    const newConversation: ConversationMessage[] = [
      ...conversation,
      { role: "user" as const, content: args.message },
    ];
    setConversation(newConversation);

    const result = await apiRequest<DeepDiveResponse & { thread_id?: string }>(
      "/v1/deep-dive",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: args.message,
          conversation: newConversation,
          answers: args.answers,
          force_analyze: !!args.forceAnalyze,
          thread_id: threadId,
        }),
      },
    );

    if (result.thread_id) setThreadId(result.thread_id);

    if (result.type === "follow_up") {
      setFollowUp({
        message: result.message,
        questions: result.questions,
        turn_index: result.turn_index,
        max_turns: result.max_turns,
        gatheredFacts: result.gatheredFacts,
      });
      // follow-up 질문 묶음이 새로 들어오면 로컬 초안 초기화
      setFollowUpDraft({});
      setConversation([
        ...newConversation,
        { role: "assistant" as const, content: result.message },
      ]);
    } else if (result.type === "analysis") {
      // 대화형 분석 결과도 C(file review) 렌더 재사용 — fileReview 상태에 저장.
      setFileReview(result);
      setFollowUp(null);
      setFollowUpDraft({});
      setAnalysis(null);
      setDeepDiveMode(false);
      if (args.persistQuery) persistRecentQuestions(args.persistQuery);
      fetchUsage();
    }
  }

  /**
   * 메인 질문 제출 함수.
   * 흐름: 자동 분기(긴 구조적 요청이면 C 심층검토) → 대화형 첫 턴 POST /v1/deep-dive.
   * 이후 턴은 handleFollowUpSubmit() 으로 answers 일괄 전송.
   */
  async function submitQuestion(q: string) {
    if (!q.trim() || loading || deepDiveLoading || fileReviewLoading) return;

    // 자동 분기 — 길고 구조적인 검토 요청은 C 심층검토로
    if (shouldUseDeepReview(q)) {
      await submitTextReview(q);
      return;
    }

    setDeepDiveMode(true);
    setSearched(true);
    setDeepDiveLoading(true);
    setError(null);
    setAnswer(null);
    setAnalysis(null);
    setFollowUp(null);
    setFollowUpDraft({});

    const stopSteps = animateAnalysisSteps();

    try {
      await callDeepDive({
        message: q,
        answers: [],
        persistQuery: q,
      });
    } catch (err) {
      // deep-dive 엔드포인트 실패 시 SSE 스트리밍 채팅으로 폴백
      setDeepDiveMode(false);
      setDeepDiveLoading(false);
      stopSteps();
      await fallbackToStreamChat(q);
      return;
    } finally {
      setDeepDiveLoading(false);
      stopSteps();
    }
  }

  /** follow-up 질문 하나에 대한 응답 초안을 업데이트. */
  function updateFollowUpDraft(
    qid: string,
    patch: Partial<{ selected: string[]; freeText: string }>,
  ) {
    setFollowUpDraft((prev) => ({
      ...prev,
      [qid]: {
        selected: patch.selected ?? prev[qid]?.selected ?? [],
        freeText: patch.freeText ?? prev[qid]?.freeText ?? "",
      },
    }));
  }

  /** 단일 선택/yes_no: 클릭 시 해당 질문의 selected 를 단일 값으로 세팅. */
  function selectSingle(qid: string, value: string) {
    updateFollowUpDraft(qid, { selected: [value] });
  }

  /** 다중 선택: 기존 리스트에 토글. */
  function toggleMulti(qid: string, value: string) {
    setFollowUpDraft((prev) => {
      const cur = prev[qid]?.selected ?? [];
      const next = cur.includes(value)
        ? cur.filter((v) => v !== value)
        : [...cur, value];
      return {
        ...prev,
        [qid]: {
          selected: next,
          freeText: prev[qid]?.freeText ?? "",
        },
      };
    });
  }

  /**
   * 현재 follow-up 묶음의 사용자 응답을 서버로 일괄 전송.
   * forceAnalyze=true 이면 '이제 분석' 버튼 케이스 — 서버는 즉시 review_document 실행.
   */
  async function handleFollowUpSubmit(forceAnalyze = false) {
    if (!followUp || deepDiveLoading) return;

    const answers: DeepDiveAnswerPayload[] = followUp.questions.map((q) => {
      const draft = followUpDraft[q.id] ?? { selected: [], freeText: "" };
      return {
        question_id: q.id,
        question_text: q.question,
        input_type: q.input_type,
        selected_options: draft.selected,
        free_text: draft.freeText ? draft.freeText : null,
      };
    });

    // 사용자에게 보여줄 다음 턴 user-message: 응답 요약 텍스트
    const summaryText = answers
      .map((a) => {
        const picked = [...a.selected_options];
        if (a.free_text) picked.push(`(직접입력) ${a.free_text}`);
        const value = picked.length ? picked.join(", ") : "(응답 없음)";
        return `- ${a.question_text} → ${value}`;
      })
      .join("\n");

    setDeepDiveLoading(true);
    setError(null);
    const stopSteps = animateAnalysisSteps();
    try {
      await callDeepDive({
        message: forceAnalyze
          ? "(사용자 요청: 지금까지 수집된 정보로 분석 진행)"
          : summaryText || "(응답 없음)",
        answers,
        forceAnalyze,
      });
    } catch (err: any) {
      setError(err?.message || "분석 요청에 실패했습니다.");
    } finally {
      setDeepDiveLoading(false);
      stopSteps();
    }
  }

  /**
   * deep-dive 실패 시 일반 SSE 스트리밍 채팅으로 폴백.
   * POST /v1/chat → meta/token/citations/done 이벤트를 순차 처리하여
   * 답변을 점진적으로 화면에 표시한다.
   */
  async function fallbackToStreamChat(q: string) {
    setLoading(true);
    setError(null);
    setSearched(true);
    setAnswer(null);

    const partial: ChatAnswer = {
      summary: "",
      answer: "",
      grounded: false,
      citations: [],
      decision_factors: [],
      action_items: [],
      retrieved_chunk_ids: [],
      precedents: [],
      saved: false,
      warning: null,
    };

    let streamStarted = false;

    await streamChat(
      q,
      (meta) => {
        partial.summary = meta.summary;
        partial.grounded = meta.grounded;
        setAnswer({ ...partial });
        streamStarted = true;
        setTimeout(() => {
          answerRef.current?.scrollIntoView({
            behavior: "smooth",
            block: "start",
          });
        }, 100);
      },
      (text) => {
        partial.answer += text;
        setAnswer({ ...partial });
      },
      (citations) => {
        partial.citations = citations as ChatAnswer["citations"];
        setAnswer({ ...partial });
      },
      (done) => {
        const final = done as ChatAnswer;
        setAnswer(final);
        setSaved(false);
        setLoading(false);
        try {
          const cache: SessionCache = { query: q, answer: final };
          window.localStorage.setItem(
            getSessionCacheKey(),
            JSON.stringify(cache),
          );
        } catch {}
        persistRecentQuestions(q);
        fetchUsage();
        setHistory(saveToHistory({ query: q, answer: final }));
      },
      (errorMsg) => {
        if (errorMsg === "LIMIT_EXCEEDED") {
          setShowLimitModal(true);
        } else if (errorMsg === "LOGIN_REQUIRED") {
          setError("일시적으로 사용량이 초과되었습니다. 잠시 후 다시 시도해 주세요.");
        } else {
          setError(errorMsg);
        }
        setLoading(false);
      },
    );

    if (!streamStarted) {
      setLoading(false);
    }
  }

  /**
   * 새 질문 제출 직전, 이전 결과 상태를 일괄 정리.
   * 메인 검색창 제출은 항상 "신규 질문"으로 간주 — Perplexity·ChatGPT의 상단=신규 패턴.
   * 후속/추가 질문은 결과 카드 내부 별도 액션(handleFollowUp / handleFollowUpSubmit /
   * 1565줄 fileReview 후속 버튼 등)에서만 발생하므로, 여기서 잔류 상태를 비워도 안전.
   * 단 attachedFiles는 사용자가 의식적으로 붙여둔 자원이라 보존 — 자동 제거 시 놀람 방지.
   */
  function softResetForNewQuery() {
    setAnswer(null);
    setFileReview(null);
    setAnalysis(null);
    setFollowUp(null);
    setFollowUpDraft({});
    setConversation([]);
    setThreadId(null);
    setDocumentDraft(null);
    setError(null);
    setSaved(false);
    setFeedbackSent(null);
    setDeepDiveMode(false);
    setForceQuickAnswer(false);
    setAutoRoutedToReview(false);
    setExpandedPrecedent(null);
    setPrecedentDetail(null);
  }

  /** 폼 제출 핸들러 — 파일 첨부 여부에 따라 submitWithFiles() 또는 submitQuestion() 분기.
   * 결과가 떠 있는 상태에서 메인 검색창 제출 시 이전 결과를 자동 정리(softReset). */
  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (searched && (answer || fileReview || conversation.length > 0)) {
      softResetForNewQuery();
    }
    if (attachedFiles.length > 0) {
      submitWithFiles(query);
    } else {
      submitQuestion(query);
    }
  }

  /** 판례 카드 클릭 — 토글 방식으로 상세 조회 (GET /v1/precedents/{sourceId}/{caseId}) */
  async function handlePrecedentClick(sourceId: string, caseId: string) {
    const key = `${sourceId}/${caseId}`;
    if (expandedPrecedent === key) {
      setExpandedPrecedent(null);
      setPrecedentDetail(null);
      return;
    }
    setExpandedPrecedent(key);
    setPrecedentLoading(true);
    try {
      const detail = await apiRequest<PrecedentDetailResponse>(
        `/v1/precedents/${sourceId}/${caseId}`,
      );
      setPrecedentDetail(detail);
    } catch {
      setPrecedentDetail(null);
    } finally {
      setPrecedentLoading(false);
    }
  }

  /** 히스토리에서 이전 질문/답변을 복원한다 */
  function loadFromHistory(item: SessionCache) {
    setQuery(item.query);
    setAnswer(item.answer);
    setSearched(true);
    setShowHistory(false);
    setDeepDiveMode(false);
    setConversation([]);
    setFollowUp(null);
    setAnalysis(null);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  /** 현재 답변을 localStorage에 저장한다 */
  async function handleSave() {
    if (!answer || saved) return;
    setSaving(true);
    setError(null);
    try {
      appendSavedAnswer(query, answer);
      setSaved(true);
    } catch (saveError) {
      setError(
        saveError instanceof Error ? saveError.message : "저장에 실패했습니다.",
      );
    } finally {
      setSaving(false);
    }
  }

  /** 판단 포인트 클릭 → 해당 키워드를 포함한 추가 질문을 textarea에 세팅 */
  function handleFollowUp(factor: string) {
    const followUpText = `${query}\n\n추가 정보: ${factor}에 대해 — `;
    setQuery(followUpText);
    textareaRef.current?.focus();
    textareaRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
  }

  function handleDeepDive(locator: string, concern: string) {
    const text = `[${locator}] ${concern} — 이 부분을 더 자세히 설명해 주세요`;
    if (fileReview) {
      // 파일 검토 후속 — followup 입력에 자동 입력 후 보내기
      const followupInput = document.querySelector<HTMLInputElement>(
        ".followup-text-input",
      );
      if (followupInput) {
        followupInput.value = text;
        followupInput.focus();
        followupInput.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    } else {
      handleFollowUp(text);
    }
  }

  async function copyToClipboard(text: string, label?: string) {
    try {
      await navigator.clipboard.writeText(text);
      // 간단 토스트 — 기존 토스트 시스템 없으니 alert 대신 임시 표시
      const el = document.createElement("div");
      el.textContent = label ? `${label} 복사됨` : "복사됨";
      el.style.cssText =
        "position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1e3a5f;color:#fff;padding:8px 14px;border-radius:6px;font-size:13px;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,0.15);";
      document.body.appendChild(el);
      setTimeout(() => el.remove(), 1500);
    } catch {}
  }

  function handleChipClick(prompt: string) {
    setQuery(prompt);
    textareaRef.current?.focus();
  }

  /** 판단 블록(verdict) 렌더링 — 결론, 근거, 주요 근거, 반대 해석, 부적용 조항 표시 */
  function renderSingleJudgment(
    jj: any,
    key: number,
    headerLabel: string | null,
  ) {
    const verdictLabel: Record<string, string> = {
      likely_yes: "가능성 높음",
      likely_no: "해당 가능성 낮음",
      depends: "조건부",
      needs_more_info: "정보 부족",
      not_applicable: "해당 없음",
    };
    const badgeCls: Record<string, string> = {
      likely_yes: "verdict-yes",
      likely_no: "verdict-no",
      depends: "verdict-depends",
      needs_more_info: "verdict-info",
      not_applicable: "verdict-na",
    };
    return (
      <div className="judgment-block" key={key}>
        <div className="judgment-head">
          <span
            className={`verdict-badge ${badgeCls[jj.verdict] || "verdict-depends"}`}
          >
            {verdictLabel[jj.verdict] || "판단"}
          </span>
          <h3 className="judgment-title">
            💡 결론{headerLabel ? ` — ${headerLabel}` : ""}
          </h3>
        </div>
        {jj.short_answer && <p className="judgment-short">{jj.short_answer}</p>}
        <details className="judgment-more">
          <summary>근거·추가 정보</summary>
          {jj.reasoning && (
            <div className="judgment-section">
              <strong>판단 근거</strong>
              <p>{jj.reasoning}</p>
            </div>
          )}
          {Array.isArray(jj.key_authorities) && jj.key_authorities.length > 0 && (
            <div className="judgment-section">
              <strong>주요 근거</strong>
              <ul>
                {jj.key_authorities.map((a: any, i: number) => (
                  <li key={i}>
                    <span className={`auth-type auth-${a.type || "statute"}`}>
                      {a.type === "case" ? "판례" : "법령"}
                    </span>
                    <strong>{a.ref}</strong>
                    {a.point && <> — {a.point}</>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {Array.isArray(jj.missing_facts) && jj.missing_facts.length > 0 && (
            <div className="judgment-section">
              <strong>판단에 필요한 추가 사실</strong>
              <ul>
                {jj.missing_facts.map((f: string, i: number) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
            </div>
          )}
          {jj.typical_path && (
            <div className="judgment-section">
              <strong>일반적 해결 경로</strong>
              <p>{jj.typical_path}</p>
            </div>
          )}
          {Array.isArray(jj.contradicting_views) &&
            jj.contradicting_views.length > 0 && (
              <div className="judgment-section">
                <strong>반대 해석</strong>
                <ul>
                  {jj.contradicting_views.map((v: string, i: number) => (
                    <li key={i}>{v}</li>
                  ))}
                </ul>
              </div>
            )}
        </details>
      </div>
    );
  }

  function renderJudgmentBlock(j: any, rejectedFromFrame?: any[]) {
    if (!j || !j.is_judgment_question) return null;

    // judgments[] (신) 또는 최상위 단일 verdict (구)
    let judgmentsList: any[] = [];
    if (Array.isArray(j.judgments) && j.judgments.length > 0) {
      judgmentsList = j.judgments;
    } else if (j.verdict) {
      judgmentsList = [j];
    } else {
      return null;
    }

    const multi = judgmentsList.length > 1;

    const rejected = [
      ...(j.rejected_citations || []),
      ...(rejectedFromFrame || []),
    ];
    const seen = new Set<string>();
    const dedupedRejected = rejected.filter((r: any) => {
      const k = r?.ref || "";
      if (!k || seen.has(k)) return false;
      seen.add(k);
      return true;
    });

    return (
      <>
        {judgmentsList.map((jj, i) =>
          renderSingleJudgment(jj, i, multi ? jj.issue_label || null : null),
        )}
        {dedupedRejected.length > 0 && (
          <div className="judgment-block">
            <div className="judgment-section rejected-block">
              <strong>⚠ 질문에 인용됐으나 적용되지 않는 조항</strong>
              <ul>
                {dedupedRejected.map((r: any, i: number) => (
                  <li key={i}>
                    <strong>{r.ref}</strong>
                    {r.reason && <> — {r.reason}</>}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </>
    );
  }

  /**
   * 문서 검토 결과 하단의 후속 질문/재검토 제출 핸들러.
   * 1) 추가 파일 있으면 → submitWithFiles() (파일 재검토)
   * 2) 재검토 모드 → submitTextReview() (전체 재분석)
   * 3) 대화 모드 → submitQuestion() (대화형 추가 질문)
   */
  function handleFollowupSubmit() {
    const text = followupText.trim();
    if (loading || deepDiveLoading || fileReviewLoading) return;
    if (!text && attachedFiles.length === 0) return;

    const originalQ = query || "";

    // 1) 추가 파일이 있으면 항상 재검토 (analyze-file)
    if (attachedFiles.length > 0) {
      const fullQ = text
        ? `${originalQ}\n\n추가 맥락: ${text}`
        : originalQ ||
          "이전 검토 결과를 참고하여 추가 자료와 함께 재검토해주세요.";
      setFollowupText("");
      submitWithFiles(fullQ);
      return;
    }

    // 2) 재검토 모드 — analyze-text로 풀 파이프라인 재실행
    if (followupMode === "review") {
      const combined = `${originalQ}\n\n[추가 맥락]\n${text}`;
      setFollowupText("");
      submitTextReview(combined);
      return;
    }

    // 3) 대화 모드 — chat 경로
    setFollowupText("");
    submitQuestion(`이전 검토 결과를 참고하여 추가 검토: ${text}`);
  }

  /** 텍스트 심층 검토 — POST /v1/analyze-text로 전체 문서 검토 파이프라인 실행 */
  async function submitTextReview(q: string) {
    setSearched(true);
    setFileReviewLoading(true);
    setError(null);
    setAnswer(null);
    setAnalysis(null);
    setFollowUp(null);
    setFileReview(null);
    setAutoRoutedToReview(true);
    // 진행 피드백 — 화면 상단으로 스크롤해 로딩 인디케이터 보이게
    setTimeout(() => window.scrollTo({ top: 0, behavior: "smooth" }), 50);
    const stopSteps = animateAnalysisSteps();

    try {
      const result = await apiRequest<any>("/v1/analyze-text", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: q, question: "" }),
      });
      setFileReview(result);
      persistRecentQuestions(q);
      fetchUsage();
    } catch (err: any) {
      setError(err?.message || "심층 검토 중 오류가 발생했습니다.");
    } finally {
      setFileReviewLoading(false);
      stopSteps();
    }
  }

  /** 공용: 파일 배열을 attachedFiles 상태에 추가. 선택·드롭 양쪽에서 사용. */
  function addFiles(incoming: File[]) {
    if (!incoming.length) return;
    if (incoming.length + attachedFiles.length > 5) {
      setError("파일은 최대 5개까지 첨부할 수 있습니다.");
      return;
    }
    setAttachedFiles((prev) => [...prev, ...incoming]);
  }

  /** 파일 선택 핸들러 — 최대 5개 제한, 선택된 파일을 attachedFiles 상태에 추가 */
  function handleFileSelect(e: React.ChangeEvent<HTMLInputElement>) {
    addFiles(Array.from(e.target.files || []));
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  /** 드래그 오버 — 드롭 허용 */
  function handleDragOver(e: React.DragEvent<HTMLElement>) {
    e.preventDefault();
    e.stopPropagation();
    if (!isDraggingOver) setIsDraggingOver(true);
  }

  function handleDragLeave(e: React.DragEvent<HTMLElement>) {
    e.preventDefault();
    e.stopPropagation();
    // 자식 요소 간 leave 이벤트 무시: 실제 좌표가 컨테이너 밖일 때만 해제
    const related = e.relatedTarget as Node | null;
    if (!related || !(e.currentTarget as HTMLElement).contains(related)) {
      setIsDraggingOver(false);
    }
  }

  /** 드롭 — 파일을 attachedFiles에 추가 */
  function handleDrop(e: React.DragEvent<HTMLElement>) {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingOver(false);
    const files = Array.from(e.dataTransfer?.files || []);
    if (files.length) addFiles(files);
  }

  function removeFile(index: number) {
    setAttachedFiles((prev) => prev.filter((_, i) => i !== index));
  }

  /**
   * 파일 첨부 검토 제출 — POST /v1/analyze-file (multipart/form-data).
   * 이전 검토 결과가 있으면 요약을 previous_summary로 함께 전송하여 맥락 유지.
   */
  async function submitWithFiles(q: string) {
    if (attachedFiles.length === 0) return;

    setFileReviewLoading(true);
    setFileReview(null);
    setError(null);
    setSearched(true);
    setDeepDiveMode(false);

    const stopSteps = animateAnalysisSteps();

    try {
      const formData = new FormData();
      attachedFiles.forEach((f) => formData.append("files", f));
      formData.append("question", q || "이 문서를 검토해 주세요.");

      // 이전 검토 결과가 있으면 요약을 함께 전송
      if (fileReview) {
        const observations: any[] =
          Array.isArray(fileReview.observations) &&
          fileReview.observations.length > 0
            ? fileReview.observations
            : fileReview.clauseReviews || [];
        const prevSummary = [
          fileReview.summary || "",
          ...observations
            .filter(
              (o: any) =>
                (o.severity || o.risk_level) === "high" ||
                (o.severity || o.risk_level) === "medium",
            )
            .flatMap((o: any) => {
              const loc =
                o.locator || (o.clause_no != null ? `제${o.clause_no}조` : "");
              const issues = Array.isArray(o.issues) ? o.issues : [];
              if (issues.length === 0) {
                const text = o.concern || o.issue || "";
                return [
                  `${loc} [${o.severity || o.risk_level}]: ${text}`.slice(
                    0,
                    150,
                  ),
                ];
              }
              return issues
                .filter(
                  (it: any) =>
                    it.severity === "high" || it.severity === "medium",
                )
                .map((it: any) => {
                  const text = it.concern || it.issue || "";
                  return `${loc} [${it.severity}]: ${text}`.slice(0, 150);
                });
            }),
        ]
          .filter(Boolean)
          .join("\n");
        if (prevSummary) {
          formData.append("previous_summary", prevSummary);
        }
      }

      const result = await apiRequestFormData<any>("/v1/analyze-file", formData);
      setFileReview(result);
      fetchUsage();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "문서 검토에 실패했습니다.",
      );
    } finally {
      setFileReviewLoading(false);
      stopSteps();
    }
  }

  /** 모든 상태를 초기화하고 새 질문 모드로 전환한다 */
  function handleReset() {
    setQuery("");
    setAnswer(null);
    setSearched(false);
    setError(null);
    setSaved(false);
    setFeedbackSent(null);
    setDeepDiveMode(false);
    setThreadId(null);
    setConversation([]);
    setFollowUp(null);
    setAnalysis(null);
    setAnalysisStep("");
    setDocumentDraft(null);
    setDraftLoading(false);
    setAttachedFiles([]);
    setFileReview(null);
    setFileReviewLoading(false);
    setForceQuickAnswer(false);
    setAutoRoutedToReview(false);
    setFollowupText("");
    setFollowupMode("chat");
    try {
      window.localStorage.removeItem(getSessionCacheKey());
    } catch {}
    textareaRef.current?.focus();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  /** 서류 초안 생성 — POST /v1/document-draft. 분석 결과의 사실관계를 기반으로 서류 작성. */
  async function handleDocumentDraft(docType: string) {
    if (!analysis || draftLoading) return;
    setDraftLoading(true);
    setDocumentDraft(null);
    try {
      const result = await apiRequest<{
        title: string;
        content: string;
        instructions: string;
        estimated_cost: string;
        disclaimer: string;
      }>("/v1/document-draft", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          document_type: docType,
          facts: (analysis as any).gatheredFacts || {},
          domain: (analysis as any).domain || "일반",
        }),
      });
      setDocumentDraft(result);
    } catch {
      setError("서류 초안 생성에 실패했습니다. 다시 시도해 주세요.");
    } finally {
      setDraftLoading(false);
    }
  }

  /** 답변 피드백 전송 (POST /v1/feedback) — best-effort, 실패해도 UX 유지 */
  async function handleFeedback(rating: "up" | "down") {
    setFeedbackSent(rating);
    try {
      await apiRequest("/v1/feedback", {
        method: "POST",
        body: JSON.stringify({
          question: query,
          rating,
        }),
      });
    } catch {
      // Feedback is best-effort — don't break UX on failure
    }
  }

  return (
    <SiteShell>
      {/* ── History Sidebar ──────────────────────────────────── */}
      {showHistory && (
        <div className="history-overlay" onClick={() => setShowHistory(false)}>
          <aside
            className="history-sidebar"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="history-header">
              <h3>이전 질문</h3>
              <button
                className="history-close"
                onClick={() => setShowHistory(false)}
                type="button"
              >
                &times;
              </button>
            </div>
            <div className="history-list">
              {history.length === 0 && (
                <p className="muted-note">아직 질문 기록이 없습니다.</p>
              )}
              {history.map((item, i) => (
                <button
                  key={`${item.query}-${i}`}
                  className="history-item"
                  type="button"
                  onClick={() => loadFromHistory(item)}
                >
                  <span className="history-question">{item.query}</span>
                  <span className="history-date">
                    {item.timestamp
                      ? new Date(item.timestamp).toLocaleDateString("ko-KR")
                      : ""}
                  </span>
                </button>
              ))}
            </div>
          </aside>
        </div>
      )}

      {/* ── Search Hero ──────────────────────────────────────── */}
      <section
        className={`search-hero-section ${searched ? "search-hero-compact" : ""}`}
      >
        {!searched && (
          <div className="search-hero-intro">
            <h1 className="search-hero-title">법률 질문에서 행동까지</h1>
            <p className="search-hero-desc">
              상황을 분석하고, 유리한 점을 정리하고, 다음 행동을 알려드립니다.
            </p>
          </div>
        )}

        <form
          className={`search-form ${isDraggingOver ? "search-form-dragover" : ""}`}
          onSubmit={handleSubmit}
          onDragOver={handleDragOver}
          onDragEnter={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <div className="search-input-wrap">
            <textarea
              ref={textareaRef}
              className="search-input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              disabled={loading || deepDiveLoading || fileReviewLoading}
              placeholder={
                attachedFiles.length > 0
                  ? "첨부된 문서에 대해 질문하세요 (원하는 관점이 있으면 함께 적어주세요)"
                  : "법률 상황을 자유롭게 적어 주세요..."
              }
              rows={searched ? 2 : 3}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  if (searched && (answer || fileReview || conversation.length > 0)) {
                    softResetForNewQuery();
                  }
                  if (attachedFiles.length > 0) {
                    submitWithFiles(query);
                  } else {
                    submitQuestion(query);
                  }
                }
              }}
            />
            {/* 첨부 파일 표시 */}
            {attachedFiles.length > 0 && (
              <div className="attached-files">
                {attachedFiles.map((f, i) => (
                  <span key={`${f.name}-${i}`} className="attached-file-chip">
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                      <path
                        d="M9.333 1.333H4a1.333 1.333 0 0 0-1.333 1.334v10.666A1.333 1.333 0 0 0 4 14.667h8a1.333 1.333 0 0 0 1.333-1.334V5.333L9.333 1.333Z"
                        stroke="currentColor"
                        strokeWidth="1.2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                    {f.name}
                    <button
                      type="button"
                      className="attached-file-remove"
                      onClick={() => removeFile(i)}
                    >
                      &times;
                    </button>
                  </span>
                ))}
              </div>
            )}
            <div className="search-actions">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept=".txt,.md,.pdf,.docx,.doc,.hwp,.hwpx,.xlsx,.xls,.png,.jpg,.jpeg,.webp,.csv,.json"
                style={{ display: "none" }}
                onChange={handleFileSelect}
              />
              <button
                type="button"
                className={`file-attach-btn ${attachedFiles.length > 0 ? "file-attach-active" : ""}`}
                onClick={() => fileInputRef.current?.click()}
                aria-label="파일 첨부"
                title="문서 첨부 (PDF, DOCX, HWP, XLSX, 이미지 — 드래그앤드롭 지원)"
              >
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <path
                    d="M14 10v2.667A1.334 1.334 0 0 1 12.667 14H3.333A1.334 1.334 0 0 1 2 12.667V10M11.333 5.333 8 2 4.667 5.333M8 2v8"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
                {!searched && attachedFiles.length === 0 && (
                  <span className="file-attach-label">문서 첨부</span>
                )}
              </button>
              {history.length > 0 && (
                <button
                  type="button"
                  className="history-toggle-btn"
                  onClick={() => setShowHistory(true)}
                  aria-label="이전 질문"
                >
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <path
                      d="M8 3.333V8l3.333 2M14 8A6 6 0 1 1 2 8a6 6 0 0 1 12 0Z"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
              )}
              {/* TODO: 사용량 제한 임시 비활성화 — 복원 시 usage badge도 함께 복원 */}
              {searched && (
                <button
                  type="button"
                  className="search-reset-btn"
                  onClick={handleReset}
                >
                  새 질문
                </button>
              )}
              <button
                type="submit"
                className="search-submit-btn"
                disabled={
                  !query.trim() ||
                  loading ||
                  deepDiveLoading ||
                  fileReviewLoading
                }
              >
                {loading || deepDiveLoading || fileReviewLoading ? (
                  <span className="button-loading">
                    <span className="spinner" />
                    분석 중
                  </span>
                ) : (
                  <>
                    {attachedFiles.length > 0 ? "문서 검토" : "질문하기"}
                    <svg
                      width="16"
                      height="16"
                      viewBox="0 0 16 16"
                      fill="none"
                      aria-hidden="true"
                    >
                      <path
                        d="M3.333 8h9.334M8.667 4l4 4-4 4"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  </>
                )}
              </button>
            </div>
          </div>
        </form>

        {!searched && (
          <>
            <div className="trust-row">
              {trustBadges.map((badge) => (
                <span key={badge.label} className="trust-badge">
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 14 14"
                    fill="none"
                    aria-hidden="true"
                  >
                    <path
                      d="M11.667 3.5L5.25 9.917 2.333 7"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                  {badge.label}
                </span>
              ))}
            </div>

            <div className="prompt-chips">
              {examplePrompts.map((prompt) => (
                <button
                  key={prompt}
                  type="button"
                  className="prompt-chip"
                  onClick={() => handleChipClick(prompt)}
                >
                  {prompt}
                </button>
              ))}
            </div>
          </>
        )}
      </section>

      {/* ── Error Banner ─────────────────────────────────────── */}
      {error && (
        <div className="error-banner">
          <strong>요청 실패</strong>
          <p>{error}</p>
          <button
            className="error-retry-btn"
            type="button"
            onClick={() => {
              setError(null);
              submitQuestion(query);
            }}
          >
            다시 시도
          </button>
        </div>
      )}

      {/* ── Deep-dive Loading ────────────────────────────────── */}
      {deepDiveLoading && (
        <section className="deep-dive-loading">
          {conversation.length > 0 && (
            <div className="conversation-history">
              {conversation.map((msg, i) => (
                <div key={i} className={`chat-bubble chat-bubble-${msg.role}`}>
                  {msg.role === "assistant" ? "AI: " : ""}
                  {msg.content}
                </div>
              ))}
            </div>
          )}
          <div className="loading-panel">
            <div className="loading-panel-header">
              <div className="loading-orb">
                <span className="loading-orb-pulse" />
                <span className="loading-orb-ring" />
              </div>
              <div className="loading-panel-title">
                <h3>답변을 준비하고 있습니다</h3>
                <p>법령·판례를 검색해 근거 기반 답변을 생성합니다</p>
                <p className="loading-eta">
                  {analysisElapsedSec >= ANALYSIS_TOTAL_ETA_SEC
                    ? `마무리 중... (${formatMMSS(analysisElapsedSec)} 경과)`
                    : `${formatMMSS(analysisElapsedSec)} 경과 · 약 2~3분 소요`}
                </p>
              </div>
            </div>
            <ol className="loading-steps">
              {ANALYSIS_STEPS.map((step, i) => {
                const currentIdx = ANALYSIS_STEPS.indexOf(analysisStep);
                const status =
                  currentIdx < 0
                    ? i === 0
                      ? "active"
                      : "pending"
                    : i < currentIdx
                      ? "done"
                      : i === currentIdx
                        ? "active"
                        : "pending";
                return (
                  <li key={i} className={`loading-step loading-step-${status}`}>
                    <span className="loading-step-marker">
                      {status === "done" ? (
                        "✓"
                      ) : status === "active" ? (
                        <span className="spinner" />
                      ) : (
                        i + 1
                      )}
                    </span>
                    <span className="loading-step-label">
                      {step.replace("...", "")}
                    </span>
                  </li>
                );
              })}
            </ol>
            <div className="loading-progress">
              <div
                className="loading-progress-bar"
                style={{
                  width: `${Math.min(
                    99,
                    Math.round(
                      (analysisElapsedSec / ANALYSIS_TOTAL_ETA_SEC) * 100,
                    ),
                  )}%`,
                }}
              />
            </div>
          </div>
        </section>
      )}

      {/* ── File Review Result ──────────────────────────────── */}
      {fileReviewLoading && (
        <section className="result-section">
          <div className="loading-panel">
            <div className="loading-panel-header">
              <div className="loading-orb">
                <span className="loading-orb-pulse" />
                <span className="loading-orb-ring" />
              </div>
              <div className="loading-panel-title">
                <h3>
                  {autoRoutedToReview
                    ? "심층 검토 진행 중"
                    : "문서를 분석하고 있습니다"}
                </h3>
                <p>
                  사실관계 정리 → 제도 프레임 → 조항별 검토 → 판단·리스크 종합
                  순으로 진행합니다
                </p>
                <p className="loading-eta">
                  {analysisElapsedSec >= ANALYSIS_TOTAL_ETA_SEC
                    ? `마무리 중... (${formatMMSS(analysisElapsedSec)} 경과)`
                    : `${formatMMSS(analysisElapsedSec)} 경과 · 약 2~3분 소요`}
                </p>
              </div>
            </div>
            <ol className="loading-steps">
              {ANALYSIS_STEPS.map((step, i) => {
                const currentIdx = ANALYSIS_STEPS.indexOf(analysisStep);
                const status =
                  currentIdx < 0
                    ? i === 0
                      ? "active"
                      : "pending"
                    : i < currentIdx
                      ? "done"
                      : i === currentIdx
                        ? "active"
                        : "pending";
                return (
                  <li key={i} className={`loading-step loading-step-${status}`}>
                    <span className="loading-step-marker">
                      {status === "done" ? (
                        "✓"
                      ) : status === "active" ? (
                        <span className="spinner" />
                      ) : (
                        i + 1
                      )}
                    </span>
                    <span className="loading-step-label">
                      {step.replace("...", "")}
                    </span>
                  </li>
                );
              })}
            </ol>
            <div className="loading-progress">
              <div
                className="loading-progress-bar"
                style={{
                  width: `${Math.min(
                    99,
                    Math.round(
                      (analysisElapsedSec / ANALYSIS_TOTAL_ETA_SEC) * 100,
                    ),
                  )}%`,
                }}
              />
            </div>
          </div>
        </section>
      )}

      {fileReview && !fileReviewLoading && (
        <section className="result-section file-review-result">
          <button type="button" className="back-link" onClick={handleReset}>
            <svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              fill="none"
              aria-hidden="true"
            >
              <path
                d="M10 12L6 8l4-4"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            새 질문하기
          </button>

          <div className="file-review-header">
            <div className="file-review-title-row">
              <h2>
                {autoRoutedToReview ? "심층 검토 결과" : "문서 검토 결과"}
              </h2>
              {autoRoutedToReview && (
                <button
                  type="button"
                  className="mode-switch-chip"
                  onClick={() => {
                    setForceQuickAnswer(true);
                    setAutoRoutedToReview(false);
                    setFileReview(null);
                    submitQuestion(query || (fileReview?.summary ?? ""));
                  }}
                  title="짧은 대화형 답변으로 다시 받기"
                >
                  ⚡ 빠른 답변으로 보기
                </button>
              )}
            </div>
            <div className="file-review-meta">
              {fileReview.documentType && (
                <span className="badge badge-type">
                  {fileReview.documentType}
                </span>
              )}
              {fileReview.overallRisk && fileReview.overallRisk !== "n/a" && (
                <span className={`badge badge-risk-${fileReview.overallRisk}`}>
                  주의 정도:{" "}
                  {fileReview.overallRisk === "high"
                    ? "높음"
                    : fileReview.overallRisk === "medium"
                      ? "보통"
                      : "낮음"}
                </span>
              )}
              {fileReview.files && (
                <span className="badge">{fileReview.files.length}개 파일</span>
              )}
            </div>
            {(() => {
              const principals =
                fileReview.documentOverview?.principals || fileReview.parties;
              return Array.isArray(principals) && principals.length > 0 ? (
                <p className="file-review-parties">
                  주요 주체: {principals.join(" / ")}
                </p>
              ) : null;
            })()}
            {(() => {
              const standpoint =
                fileReview.documentOverview?.standpoint ||
                fileReview.review_perspective;
              return standpoint ? (
                <p className="file-review-perspective">
                  검토 관점: {standpoint}
                </p>
              ) : null;
            })()}
          </div>

          {renderJudgmentBlock(
            fileReview.judgment,
            fileReview.rejectedCitations,
          )}

          {fileReview.summary && (
            <div className="file-review-summary">
              <h3>요약</h3>
              <p>{fileReview.summary}</p>
            </div>
          )}

          {(() => {
            const kp: string[] =
              Array.isArray(fileReview.keyPoints) &&
              fileReview.keyPoints.length > 0
                ? fileReview.keyPoints
                : fileReview.key_negotiation_points || [];
            if (kp.length === 0) return null;
            return (
              <div className="negotiation-points">
                <h3>핵심 포인트</h3>
                <ol>
                  {kp.map((p: string, i: number) => (
                    <li key={i}>{p}</li>
                  ))}
                </ol>
              </div>
            );
          })()}

          {fileReview.actionItems && fileReview.actionItems.length > 0 && (
            <div className="action-items-section">
              <h3>행동 항목</h3>
              <ul>
                {fileReview.actionItems.map((a: string, i: number) => (
                  <li key={i}>{a}</li>
                ))}
              </ul>
            </div>
          )}

          {/* 누락 참조 문서 안내 배너 */}
          {(() => {
            const refItems = (fileReview.actionItems || []).filter(
              (ai: string) =>
                /별첨|부속|과업|명세서|첨부.*확인|추가.*자료|제공.*확인|함께.*제공/i.test(
                  ai,
                ),
            );
            if (refItems.length === 0) return null;
            return (
              <div className="missing-ref-banner">
                <div className="missing-ref-icon">
                  <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
                    <path
                      d="M10 6v4m0 4h.01M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </div>
                <div className="missing-ref-content">
                  <strong>
                    추가 자료가 있으면 더 정확한 검토가 가능합니다
                  </strong>
                  <ul>
                    {refItems.map((item: string, i: number) => (
                      <li key={i}>{item}</li>
                    ))}
                  </ul>
                </div>
              </div>
            );
          })()}

          {/* 세부 분석 — 기본 접힘. 펼치면 목적/제도프레임/검토축/상세/부족/외부/리스크 */}
          <details className="review-details-expander">
            <summary>
              <span>세부 분석 펼치기</span>
              <span className="review-details-meta">
                {[
                  fileReview.documentOverview?.purpose && "목적",
                  (fileReview.institutionalFrame ||
                    (fileReview.institutionalAxes?.length || 0) > 0) &&
                    "제도",
                  (fileReview.observations?.length ||
                    fileReview.clauseReviews?.length ||
                    0) > 0 &&
                    `상세 ${fileReview.observations?.length || fileReview.clauseReviews?.length}건`,
                  (fileReview.gaps?.length ||
                    fileReview.missingClauses?.length ||
                    0) > 0 &&
                    `부족 ${fileReview.gaps?.length || fileReview.missingClauses?.length}건`,
                  (fileReview.externalConsiderations?.length || 0) > 0 &&
                    `외부 ${fileReview.externalConsiderations.length}건`,
                  (fileReview.riskScenarios?.length || 0) > 0 &&
                    `리스크 ${fileReview.riskScenarios.length}건`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </summary>

            <div className="review-details-content">
              {fileReview.documentOverview?.purpose && (
                <div
                  className="institutional-frame"
                  style={{
                    borderLeftColor: "#16a34a",
                    background: "rgba(22,163,74,0.06)",
                  }}
                >
                  <h3 style={{ color: "#166534" }}>선언된 목적</h3>
                  <p className="institutional-frame-text">
                    {fileReview.documentOverview.purpose}
                  </p>
                </div>
              )}

              {(fileReview.institutionalFrame ||
                (Array.isArray(fileReview.institutionalAxes) &&
                  fileReview.institutionalAxes.length > 0)) && (
                <div className="institutional-frame">
                  <h3>제도 프레임</h3>
                  {fileReview.institutionalFrame && (
                    <p className="institutional-frame-text">
                      {fileReview.institutionalFrame}
                    </p>
                  )}
                  {Array.isArray(fileReview.institutionalAxes) &&
                    fileReview.institutionalAxes.length > 0 && (
                      <ul className="institutional-axes-list">
                        {fileReview.institutionalAxes.map(
                          (ax: any, i: number) => (
                            <li key={i}>
                              <strong>{ax.name}</strong>
                              {ax.why ? <> — {ax.why}</> : null}
                            </li>
                          ),
                        )}
                      </ul>
                    )}
                </div>
              )}

              {Array.isArray(fileReview.reviewAxes) &&
                fileReview.reviewAxes.length > 0 && (
                  <div className="review-axes-section">
                    <h3>이번 검토 축</h3>
                    <p>{fileReview.reviewAxes.join(" · ")}</p>
                  </div>
                )}

              {(() => {
                const observations: any[] =
                  Array.isArray(fileReview.observations) &&
                  fileReview.observations.length > 0
                    ? fileReview.observations
                    : fileReview.clauseReviews || [];
                if (observations.length === 0) return null;
                return (
                  <details className="sub-section" open>
                    <summary>
                      <span>상세 검토</span>
                      <span className="sub-section-count">
                        {observations.length}건
                      </span>
                    </summary>
                    <div className="clause-reviews">
                      {observations.map((o: any, i: number) => {
                        const rawIssues: any[] = Array.isArray(o.issues)
                          ? o.issues
                          : [];
                        const legacy =
                          o.concern || o.issue || o.suggestion
                            ? [
                                {
                                  severity: o.severity || o.risk_level,
                                  concern: o.concern,
                                  issue: o.issue,
                                  suggestion: o.suggestion,
                                  basis: o.basis,
                                  legal_basis: o.legal_basis,
                                },
                              ]
                            : [];
                        const issues = rawIssues.length ? rawIssues : legacy;
                        const [first, ...rest] = issues;
                        const sev = o.severity || o.risk_level || "ok";
                        const sevLabel = (s: string) =>
                          s === "high"
                            ? "HIGH"
                            : s === "medium"
                              ? "MED"
                              : s === "low"
                                ? "LOW"
                                : "OK";
                        const locator =
                          o.locator ||
                          (o.clause_no != null
                            ? String(o.clause_no).includes("조")
                              ? o.clause_no
                              : `제${o.clause_no}조`
                            : "");
                        const locatorTitle =
                          o.locator_title || o.clause_title || "";
                        const renderIssue = (it: any, key: number | string) => {
                          const text = it.concern || it.issue;
                          const basis = it.basis || it.legal_basis;
                          return (
                            <div
                              key={key}
                              className={`clause-issue-item sev-${it.severity || "low"}`}
                            >
                              {issues.length > 1 && (
                                <span
                                  className={`clause-issue-sev risk-${it.severity || "low"}`}
                                >
                                  {sevLabel(it.severity)}
                                </span>
                              )}
                              {text && (
                                <div className="clause-issue">
                                  <strong>문제:</strong> {text}
                                </div>
                              )}
                              {it.suggestion && (
                                <div className="clause-suggestion">
                                  <strong>제안:</strong> {it.suggestion}
                                  <button
                                    type="button"
                                    className="copy-btn"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      copyToClipboard(it.suggestion, "제안");
                                    }}
                                    aria-label="제안 복사"
                                  >
                                    📋
                                  </button>
                                </div>
                              )}
                              {basis && (
                                <div className="clause-legal-basis">
                                  근거: {formatBasis(basis)}
                                </div>
                              )}
                            </div>
                          );
                        };
                        const firstConcern =
                          first?.concern || first?.issue || "";
                        return (
                          <div
                            key={i}
                            className={`clause-review-card clause-risk-${sev}`}
                          >
                            <div className="clause-review-header">
                              <span className={`clause-risk-badge risk-${sev}`}>
                                {sevLabel(sev)}
                              </span>
                              <span className="clause-title">
                                {locator} {locatorTitle}
                              </span>
                              {issues.length > 1 && (
                                <span className="clause-issue-count">
                                  지적 {issues.length}건
                                </span>
                              )}
                            </div>
                            {o.original_text && (
                              <p className="clause-original">
                                &ldquo;{o.original_text}&rdquo;
                              </p>
                            )}
                            {first && renderIssue(first, 0)}
                            {rest.length > 0 && (
                              <details className="clause-issues-more">
                                <summary>
                                  + 다른 지적 {rest.length}건 보기
                                </summary>
                                {rest.map((it, j) => renderIssue(it, j + 1))}
                              </details>
                            )}
                            {(locator || firstConcern) && (
                              <button
                                type="button"
                                className="deep-dive-chip"
                                onClick={() =>
                                  handleDeepDive(
                                    locator || "이 부분",
                                    firstConcern,
                                  )
                                }
                              >
                                + 이 부분 더 알아보기
                              </button>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </details>
                );
              })()}

              {(() => {
                const gaps: any[] =
                  Array.isArray(fileReview.gaps) && fileReview.gaps.length > 0
                    ? fileReview.gaps
                    : (fileReview.missingClauses || []).map((m: any) => ({
                        topic: m.clause,
                        reason: m.reason,
                        suggestion: m.suggestion,
                      }));
                if (gaps.length === 0) return null;
                return (
                  <details className="sub-section">
                    <summary>
                      <span>부족·보완 사항</span>
                      <span className="sub-section-count">{gaps.length}건</span>
                    </summary>
                    <div className="missing-clauses">
                      {gaps.map((g: any, i: number) => (
                        <div key={i} className="missing-clause-card">
                          <strong>{g.topic}</strong>
                          {g.reason && <p>{g.reason}</p>}
                          {g.suggestion && (
                            <p className="clause-suggestion">
                              <strong>제안:</strong> {g.suggestion}
                              <button
                                type="button"
                                className="copy-btn"
                                onClick={() =>
                                  copyToClipboard(g.suggestion, "제안")
                                }
                                aria-label="제안 복사"
                              >
                                📋
                              </button>
                            </p>
                          )}
                          {g.topic && (
                            <button
                              type="button"
                              className="deep-dive-chip"
                              onClick={() =>
                                handleDeepDive(
                                  "부족·보완",
                                  `${g.topic} — ${g.reason || ""}`,
                                )
                              }
                            >
                              + 이 부분 더 알아보기
                            </button>
                          )}
                        </div>
                      ))}
                    </div>
                  </details>
                );
              })()}

              {Array.isArray(fileReview.externalConsiderations) &&
                fileReview.externalConsiderations.length > 0 &&
                (() => {
                  const externals = fileReview.externalConsiderations;
                  return (
                    <details className="sub-section">
                      <summary>
                        <span>문서 외 고려사항</span>
                        <span className="sub-section-count">
                          {externals.length}건
                        </span>
                      </summary>
                      <div className="external-considerations">
                        {externals.map((ec: any, i: number) => (
                          <div
                            key={i}
                            className="missing-clause-card external-card"
                          >
                            <div className="external-card-head">
                              {ec.section_title && (
                                <span className="external-section-chip">
                                  {ec.section_title}
                                </span>
                              )}
                              <strong>{ec.topic}</strong>
                            </div>
                            {ec.detail && <p>{ec.detail}</p>}
                            {ec.suggestion && (
                              <p className="clause-suggestion">
                                <strong>권고:</strong> {ec.suggestion}
                                <button
                                  type="button"
                                  className="copy-btn"
                                  onClick={() =>
                                    copyToClipboard(ec.suggestion, "권고")
                                  }
                                  aria-label="권고 복사"
                                >
                                  📋
                                </button>
                              </p>
                            )}
                            {ec.topic && (
                              <button
                                type="button"
                                className="deep-dive-chip"
                                onClick={() =>
                                  handleDeepDive(
                                    ec.section_title || "문서 외",
                                    ec.topic,
                                  )
                                }
                              >
                                + 이 부분 더 알아보기
                              </button>
                            )}
                          </div>
                        ))}
                      </div>
                    </details>
                  );
                })()}

              {Array.isArray(fileReview.riskScenarios) &&
                fileReview.riskScenarios.length > 0 && (
                  <details className="sub-section">
                    <summary>
                      <span>예상 리스크 시나리오</span>
                      <span className="sub-section-count">
                        {fileReview.riskScenarios.length}건
                      </span>
                    </summary>
                    <div className="risk-scenarios">
                      {fileReview.riskScenarios.map((s: any, i: number) => (
                        <div
                          key={i}
                          className={`risk-scenario-card sev-${s.severity || "low"}`}
                        >
                          <div className="risk-scenario-header">
                            <span
                              className={`clause-risk-badge risk-${s.severity || "low"}`}
                            >
                              {s.severity === "high"
                                ? "HIGH"
                                : s.severity === "medium"
                                  ? "MED"
                                  : "LOW"}
                            </span>
                            {Array.isArray(s.affected) &&
                              s.affected.length > 0 && (
                                <span className="risk-scenario-affected">
                                  {s.affected.join(", ")}
                                </span>
                              )}
                          </div>
                          <p className="risk-scenario-trigger">
                            <strong>상황:</strong> {s.trigger}
                          </p>
                          {s.root_cause && (
                            <p className="risk-scenario-cause">
                              <strong>원인:</strong> {s.root_cause}
                            </p>
                          )}
                          {s.suggestion && (
                            <p className="clause-suggestion">
                              <strong>예방:</strong> {s.suggestion}
                              <button
                                type="button"
                                className="copy-btn"
                                onClick={() =>
                                  copyToClipboard(s.suggestion, "예방안")
                                }
                                aria-label="예방안 복사"
                              >
                                📋
                              </button>
                            </p>
                          )}
                          {s.basis && (
                            <p className="clause-legal-basis">
                              근거: {formatBasis(s.basis)}
                            </p>
                          )}
                          {s.trigger && (
                            <button
                              type="button"
                              className="deep-dive-chip"
                              onClick={() =>
                                handleDeepDive("리스크 시나리오", s.trigger)
                              }
                            >
                              + 이 시나리오 더 알아보기
                            </button>
                          )}
                        </div>
                      ))}
                    </div>
                  </details>
                )}
            </div>
          </details>

          {fileReview.disclaimer && (
            <p className="file-review-disclaimer">{fileReview.disclaimer}</p>
          )}

          {/* 추가 질문/파일 첨부 */}
          <div className="file-review-followup">
            <p className="followup-label">
              추가 자료가 있으면 더 정확한 검토가 가능합니다.
            </p>
            <div className="followup-input-row">
              <input
                ref={followupFileInputRef}
                type="file"
                multiple
                accept=".txt,.md,.pdf,.docx,.doc,.hwp,.hwpx,.xlsx,.xls,.png,.jpg,.jpeg,.webp,.csv,.json"
                style={{ display: "none" }}
                onChange={(e) => {
                  const files = Array.from(e.target.files || []);
                  setAttachedFiles((prev) => [...prev, ...files]);
                  if (followupFileInputRef.current)
                    followupFileInputRef.current.value = "";
                }}
              />
              <button
                type="button"
                className="followup-attach-btn"
                onClick={() => followupFileInputRef.current?.click()}
              >
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                  <path
                    d="M14 10v2.667A1.334 1.334 0 0 1 12.667 14H3.333A1.334 1.334 0 0 1 2 12.667V10M11.333 5.333 8 2 4.667 5.333M8 2v8"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
                추가 파일
              </button>
              <input
                type="text"
                className="followup-text-input"
                placeholder={
                  followupMode === "review"
                    ? "추가 맥락을 입력하면 전체 재검토합니다..."
                    : "이 검토에 대해 추가 질문..."
                }
                value={followupText}
                onChange={(e) => setFollowupText(e.target.value)}
                disabled={loading || deepDiveLoading || fileReviewLoading}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    handleFollowupSubmit();
                  }
                }}
              />
              <div
                className="followup-mode-toggle"
                role="group"
                aria-label="후속 모드"
              >
                <button
                  type="button"
                  className={followupMode === "chat" ? "active" : ""}
                  onClick={() => setFollowupMode("chat")}
                  disabled={loading || deepDiveLoading || fileReviewLoading}
                  title="짧은 대화형 답변"
                >
                  대화
                </button>
                <button
                  type="button"
                  className={followupMode === "review" ? "active" : ""}
                  onClick={() => setFollowupMode("review")}
                  disabled={loading || deepDiveLoading || fileReviewLoading}
                  title="추가 맥락을 반영해 전체 재검토"
                >
                  재검토
                </button>
              </div>
              <button
                type="button"
                className="followup-submit-btn"
                onClick={handleFollowupSubmit}
                disabled={
                  loading ||
                  deepDiveLoading ||
                  fileReviewLoading ||
                  (!attachedFiles.length && !followupText.trim())
                }
              >
                {loading || deepDiveLoading || fileReviewLoading
                  ? "처리 중..."
                  : attachedFiles.length > 0
                    ? "추가 자료로 재검토"
                    : "보내기"}
              </button>
            </div>
            {attachedFiles.length > 0 && (
              <div className="attached-files" style={{ marginTop: 8 }}>
                {attachedFiles.map((f, i) => (
                  <span key={`${f.name}-${i}`} className="attached-file-chip">
                    {f.name}
                    <button
                      type="button"
                      className="attached-file-remove"
                      onClick={() =>
                        setAttachedFiles((prev) =>
                          prev.filter((_, idx) => idx !== i),
                        )
                      }
                    >
                      &times;
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>
        </section>
      )}

      {/* ── Deep-dive Follow-up Questions ────────────────────── */}
      {followUp && !deepDiveLoading && deepDiveMode && (
        <section className="deep-dive-section">
          <button type="button" className="back-link" onClick={handleReset}>
            <svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              fill="none"
              aria-hidden="true"
            >
              <path
                d="M10 12L6 8l4-4"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            새 질문하기
          </button>

          {/* Conversation history */}
          <div className="conversation-history">
            {conversation.map((msg, i) => (
              <div key={i} className={`chat-bubble chat-bubble-${msg.role}`}>
                {msg.content}
              </div>
            ))}
          </div>

          {/* AI message */}
          <div className="ai-message">
            <p>{followUp.message}</p>
          </div>

          {/* Follow-up 질문 묶음 — 모두 답한 뒤 [다음] 으로 일괄 전송 */}
          {typeof followUp.turn_index === "number" &&
            typeof followUp.max_turns === "number" && (
              <div className="follow-up-progress">
                {followUp.turn_index + 1} / {followUp.max_turns} 턴
              </div>
            )}
          {followUp.questions.map((q) => {
            const draft = followUpDraft[q.id] ?? {
              selected: [] as string[],
              freeText: "",
            };
            const type = q.input_type;
            const options = q.options ?? [];
            const showFreeText =
              type === "free_text" || (q.allow_free_text ?? false);
            return (
              <div key={q.id} className="follow-up-card">
                <span className="follow-up-reason">{q.legal_reason}</span>
                <p className="follow-up-question">{q.question}</p>
                <div className="follow-up-options">
                  {/* 단일 선택류 — yes_no / single_choice / (레거시)choice */}
                  {(type === "yes_no" ||
                    type === "single_choice" ||
                    type === "choice") && (
                    <>
                      {(type === "yes_no"
                        ? ["예", "아니오"]
                        : options
                      ).map((opt) => {
                        const isActive = draft.selected.includes(opt);
                        return (
                          <button
                            key={opt}
                            type="button"
                            className={`intake-btn${isActive ? " intake-btn-active" : ""}`}
                            onClick={() => selectSingle(q.id, opt)}
                          >
                            {opt}
                          </button>
                        );
                      })}
                    </>
                  )}

                  {/* 다중 선택 */}
                  {type === "multi_choice" &&
                    options.map((opt) => {
                      const isActive = draft.selected.includes(opt);
                      return (
                        <button
                          key={opt}
                          type="button"
                          className={`intake-btn${isActive ? " intake-btn-active" : ""}`}
                          onClick={() => toggleMulti(q.id, opt)}
                        >
                          {isActive ? "✓ " : ""}
                          {opt}
                        </button>
                      );
                    })}

                  {/* 자유 입력 (free_text 또는 allow_free_text=true 병행) */}
                  {showFreeText && (
                    <input
                      type="text"
                      className="follow-up-text-input"
                      placeholder={
                        type === "free_text"
                          ? "답변을 입력하세요"
                          : "직접 입력 (선택)"
                      }
                      value={draft.freeText}
                      onChange={(e) =>
                        updateFollowUpDraft(q.id, {
                          freeText: e.target.value,
                        })
                      }
                    />
                  )}
                </div>
              </div>
            );
          })}

          {/* 일괄 전송 액션 */}
          <div className="follow-up-actions">
            <button
              type="button"
              className="intake-btn intake-btn-submit"
              onClick={() => handleFollowUpSubmit(false)}
              disabled={deepDiveLoading}
            >
              다음
            </button>
            <button
              type="button"
              className="intake-btn intake-btn-secondary"
              onClick={() => handleFollowUpSubmit(true)}
              disabled={deepDiveLoading}
              title="추가 질문을 건너뛰고 지금까지 수집된 정보로 심층 분석을 실행합니다."
            >
              지금까지의 정보로 분석
            </button>
          </div>
        </section>
      )}

      {/* ── Deep-dive Analysis Result ────────────────────────── */}
      {analysis && deepDiveMode && !deepDiveLoading && (
        <section className="resolution-section">
          <button type="button" className="back-link" onClick={handleReset}>
            <svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              fill="none"
              aria-hidden="true"
            >
              <path
                d="M10 12L6 8l4-4"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            새 질문하기
          </button>

          {/* GPT Differentiation Banner */}
          <div className="differentiation-banner">
            <span>법조문 {analysis.citations?.length || 0}개 검증 인용</span>
            <span>판례 {analysis.precedents?.length || 0}건 실시간 검색</span>
            <span>사실관계 기반 맞춤 분석</span>
          </div>

          <div className="resolution-result">
            <h2 className="resolution-result-title">{analysis.summary}</h2>

            {/* Legal Position Gauge (deterministic, no LLM) */}
            {(analysis as any).legalPosition &&
              (() => {
                const pos = (analysis as any).legalPosition;
                const strengthLabel =
                  {
                    strong: "유리",
                    moderate: "보통",
                    weak: "불리",
                    unknown: "판단 보류",
                  }[pos.strength as string] || "판단 보류";
                const strengthColor =
                  {
                    strong: "var(--success)",
                    moderate: "var(--highlight)",
                    weak: "var(--error)",
                    unknown: "var(--text-tertiary)",
                  }[pos.strength as string] || "var(--text-tertiary)";
                return (
                  <div className="legal-position-card">
                    <div className="legal-position-header">
                      <span className="legal-position-label">
                        법적 포지션 (법조문 기반 확정 판단)
                      </span>
                      <span
                        className="legal-position-badge"
                        style={{ background: strengthColor }}
                      >
                        {strengthLabel}
                      </span>
                    </div>
                    <div className="legal-position-gauge">
                      <div
                        className="legal-position-gauge-fill"
                        style={{
                          width: `${pos.strengthScore}%`,
                          background: strengthColor,
                        }}
                      />
                    </div>
                    <div className="legal-position-facts">
                      {pos.deterministicFacts.map((f: any, i: number) => (
                        <div
                          key={i}
                          className={`legal-position-fact legal-position-fact-${f.status}`}
                        >
                          <span className="legal-position-fact-status">
                            {f.status === "favorable"
                              ? "✅"
                              : f.status === "cautionary"
                                ? "⚠️"
                                : "ℹ️"}
                          </span>
                          <div>
                            <strong>{f.fact}</strong>
                            <span className="legal-position-fact-basis">
                              {f.legalBasis}
                            </span>
                            <span className="legal-position-fact-impact">
                              {f.impact}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="legal-position-note">
                      이 판단은 AI 추측이 아닌 법조문 조건 매칭으로
                      결정적(deterministic)으로 계산되었습니다.
                    </div>
                  </div>
                );
              })()}

            {/* Overview stats */}
            <div className="resolution-stats">
              {analysis.winRate && (
                <div className="resolution-stat">
                  <span className="resolution-stat-icon">&#9878;&#65039;</span>
                  <span className="resolution-stat-label">
                    유사 판례 승소율
                  </span>
                  <strong className="resolution-stat-value">
                    {analysis.winRate}
                  </strong>
                </div>
              )}
              <div className="resolution-stat">
                <span className="resolution-stat-icon">&#128176;</span>
                <span className="resolution-stat-label">예상 비용</span>
                <strong className="resolution-stat-value">
                  {analysis.estimatedCost}
                </strong>
              </div>
              <div className="resolution-stat">
                <span className="resolution-stat-icon">&#9201;&#65039;</span>
                <span className="resolution-stat-label">예상 기간</span>
                <strong className="resolution-stat-value">
                  {analysis.estimatedTimeline}
                </strong>
              </div>
            </div>

            {/* Answer narrative */}
            <article className="answer-card answer-narrative-card">
              <p className="section-kicker">상황 분석</p>
              <p className="answer-prose">{analysis.answer}</p>
            </article>

            {/* Fact Analysis */}
            <article className="answer-card">
              <p className="section-kicker">사실관계 분석</p>

              {analysis.favorableFacts.length > 0 && (
                <div className="fact-group fact-group-favorable">
                  <h4 className="fact-group-title">유리한 사실</h4>
                  {analysis.favorableFacts.map((f, i) => (
                    <div key={i} className="fact-item">
                      <strong>{f.fact}</strong>
                      <span className="fact-basis">{f.legalBasis}</span>
                      <span className="fact-impact">{f.impact}</span>
                    </div>
                  ))}
                </div>
              )}

              {analysis.cautionaryFacts.length > 0 && (
                <div className="fact-group fact-group-cautionary">
                  <h4 className="fact-group-title">주의가 필요한 사실</h4>
                  {analysis.cautionaryFacts.map((f, i) => (
                    <div key={i} className="fact-item">
                      <strong>{f.fact}</strong>
                      <span className="fact-basis">{f.legalBasis}</span>
                      <span className="fact-impact">{f.impact}</span>
                    </div>
                  ))}
                </div>
              )}

              {analysis.recommendedEvidence.length > 0 && (
                <div className="fact-group fact-group-evidence">
                  <h4 className="fact-group-title">
                    추가로 확보하면 좋은 증거
                  </h4>
                  {analysis.recommendedEvidence.map((e, i) => (
                    <div key={i} className="fact-item">
                      <strong>{e.item}</strong>
                      <span className="fact-impact">{e.reason}</span>
                      <span className="fact-basis">
                        확보 방법: {e.howToGet}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </article>

            {/* Action Plan */}
            {analysis.actionPlan.length > 0 && (
              <article className="answer-card">
                <p className="section-kicker">행동 플랜</p>
                <div className="action-plan-list">
                  {analysis.actionPlan.map((step) => (
                    <div key={step.order} className="action-step">
                      <div className="action-step-num">{step.order}</div>
                      <div className="action-step-body">
                        <strong className="action-step-title">
                          {step.action}
                        </strong>
                        <div className="action-step-meta">
                          <span>&#9201;&#65039; {step.deadline}</span>
                          <span>&#128176; {step.cost}</span>
                          {step.agency && <span>&#127970; {step.agency}</span>}
                        </div>
                        {step.documents.length > 0 && (
                          <span className="action-step-docs">
                            필요 서류: {step.documents.join(", ")}
                          </span>
                        )}
                        <p className="action-step-detail">{step.detail}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </article>
            )}

            {/* Key Deadlines */}
            {analysis.keyDeadlines.length > 0 && (
              <article className="answer-card">
                <p className="section-kicker">놓치면 안 되는 기한</p>
                {analysis.keyDeadlines.map((d, i) => (
                  <div key={i} className="deadline-item">
                    <strong>{d.item}</strong>
                    <span className="deadline-date">{d.deadline}</span>
                    <span className="deadline-consequence">
                      {d.consequence}
                    </span>
                  </div>
                ))}
              </article>
            )}

            {/* Citations */}
            {analysis.citations.length > 0 && (
              <article className="answer-card">
                <p className="section-kicker">인용 조문</p>
                <CitationsList citations={analysis.citations} />
              </article>
            )}

            {/* Document Draft Section */}
            <article className="answer-card document-draft-section">
              <p className="section-kicker">서류 초안 생성</p>
              <p className="document-draft-desc">
                분석된 사실관계를 바탕으로 서류 초안을 자동 생성합니다.
              </p>
              <div className="document-draft-buttons">
                <input
                  type="text"
                  className="document-draft-input"
                  placeholder="서류 종류를 입력하세요 (예: 내용증명, 합의서, 고소장 등)"
                  onKeyDown={(e) => {
                    if (
                      e.key === "Enter" &&
                      (e.target as HTMLInputElement).value.trim()
                    ) {
                      handleDocumentDraft(
                        (e.target as HTMLInputElement).value.trim(),
                      );
                      (e.target as HTMLInputElement).value = "";
                    }
                  }}
                  disabled={draftLoading}
                />
                <button
                  type="button"
                  className="document-draft-btn"
                  disabled={draftLoading}
                  onClick={() => {
                    const input = document.querySelector<HTMLInputElement>(
                      ".document-draft-input",
                    );
                    if (input?.value.trim()) {
                      handleDocumentDraft(input.value.trim());
                      input.value = "";
                    }
                  }}
                >
                  <span className="document-draft-btn-text">
                    <strong>서류 초안 생성</strong>
                  </span>
                </button>
              </div>

              {draftLoading && (
                <div className="document-draft-loading">
                  <div className="spinner" />
                  <span>서류 초안을 생성하고 있습니다...</span>
                </div>
              )}

              {documentDraft && (
                <div className="document-draft-result">
                  <div className="document-draft-header">
                    <h4>{documentDraft.title}</h4>
                    <button
                      type="button"
                      className="document-draft-copy"
                      onClick={() => {
                        navigator.clipboard.writeText(documentDraft.content);
                      }}
                    >
                      복사
                    </button>
                  </div>
                  <pre className="document-draft-content">
                    {documentDraft.content}
                  </pre>
                  {documentDraft.instructions && (
                    <div className="document-draft-instructions">
                      <strong>사용 방법</strong>
                      <p>{documentDraft.instructions}</p>
                    </div>
                  )}
                  {documentDraft.estimated_cost && (
                    <span className="document-draft-cost">
                      예상 비용: {documentDraft.estimated_cost}
                    </span>
                  )}
                  <div className="document-draft-disclaimer">
                    {documentDraft.disclaimer ||
                      "이 초안은 참고용이며, 발송/제출 전 내용을 반드시 확인하세요."}
                  </div>
                </div>
              )}
            </article>

            {/* Disclaimer */}
            <div className="resolution-disclaimer">
              이 분석은 법률 정보이며 법률 조언이 아닙니다. 구체적 사안은
              변호사와 상담하세요.
            </div>
          </div>
        </section>
      )}

      {/* ── Loading Skeleton (for fallback streaming) ─────────── */}
      {loading && !hasAnswer && (
        <section className="answer-skeleton">
          <div className="skeleton-header">
            <div className="skeleton-line skeleton-line-short" />
            <div className="skeleton-line skeleton-line-long" />
          </div>
          <div className="skeleton-body">
            <div className="skeleton-block" />
            <div className="skeleton-sidebar" />
          </div>
        </section>
      )}

      {/* ── Answer Section (fallback streaming chat) ──────────── */}
      {hasAnswer && (
        <section className="answer-section" ref={answerRef}>
          <button type="button" className="back-link" onClick={handleReset}>
            <svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              fill="none"
              aria-hidden="true"
            >
              <path
                d="M10 12L6 8l4-4"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            새 질문하기
          </button>
          <div className="answer-section-header">
            <div className="answer-section-meta">
              <p className="section-kicker">
                {answer.warning ? "추가 사실 확인 필요" : "근거 기반 답변"}
              </p>
              <h2 className="answer-section-title">{answer.summary}</h2>
              {forceQuickAnswer && query && (
                <button
                  type="button"
                  className="mode-switch-chip"
                  style={{ marginTop: 8 }}
                  onClick={() => {
                    setForceQuickAnswer(false);
                    submitTextReview(query);
                  }}
                  title="구조화된 심층 검토로 다시 받기"
                >
                  🔍 심층 검토로 보기
                </button>
              )}
            </div>
            <div className="answer-section-actions">
              <span
                className={`status-pill ${answer.warning ? "status-caution" : "status-affirmed"}`}
              >
                {answer.warning ? "비율 단정 보류" : "근거 반영"}
              </span>
              <span className="status-pill status-neutral">{`${answer.citations.length}개 조문 근거`}</span>
              <button
                className={
                  saved ? "saved-button pill-btn" : "secondary-button pill-btn"
                }
                disabled={saving || saved}
                onClick={handleSave}
                type="button"
              >
                {saving ? "저장 중..." : saved ? "저장됨" : "저장"}
              </button>
            </div>
          </div>

          <div className="answer-body">
            <div className="answer-main">
              {renderJudgmentBlock(answer.judgment)}

              {/* Narrative */}
              <article className="answer-card answer-narrative-card">
                <p className="section-kicker">근거 기반 요약</p>
                <p
                  className={`answer-prose ${loading ? "streaming-cursor" : ""}`}
                >
                  {answer.answer}
                </p>
                {answer.warning && (
                  <div className="warning-callout">{answer.warning}</div>
                )}
              </article>

              {/* Decision Factors */}
              {answer.decision_factors.length > 0 && (
                <article className="answer-card">
                  <div className="section-header-row">
                    <p className="section-kicker">판단 포인트</p>
                    <p className="section-hint">클릭하면 추가 질문</p>
                  </div>
                  <div className="factor-list">
                    {answer.decision_factors.map((item) => (
                      <button
                        key={item}
                        className="factor-chip"
                        type="button"
                        onClick={() => handleFollowUp(item)}
                      >
                        <span className="factor-text">{item}</span>
                        <span className="factor-add">+ 추가 질문</span>
                      </button>
                    ))}
                  </div>
                </article>
              )}

              {/* Action Items */}
              {answer.action_items.length > 0 && (
                <article className="answer-card">
                  <p className="section-kicker">지금 할 일</p>
                  <ul className="answer-list">
                    {answer.action_items.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </article>
              )}

              {/* Feedback */}
              {!loading && (
                <div className="feedback-row">
                  <span className="feedback-label">
                    이 답변이 도움이 되었나요?
                  </span>
                  <div className="feedback-buttons">
                    <button
                      type="button"
                      className={`feedback-btn ${feedbackSent === "up" ? "feedback-btn-active" : ""}`}
                      disabled={!!feedbackSent}
                      onClick={() => handleFeedback("up")}
                    >
                      도움됨
                    </button>
                    <button
                      type="button"
                      className={`feedback-btn ${feedbackSent === "down" ? "feedback-btn-active" : ""}`}
                      disabled={!!feedbackSent}
                      onClick={() => handleFeedback("down")}
                    >
                      부정확
                    </button>
                  </div>
                  {feedbackSent && (
                    <span className="feedback-thanks">의견 감사합니다!</span>
                  )}
                </div>
              )}

              {/* Precedents */}
              {answer.precedents?.length > 0 &&
                (() => {
                  const precs = answer.precedents!;
                  const recommended = precs.slice(0, 3);
                  const rest = precs.slice(3);
                  const PAGE_SIZE = 5;
                  const restPage = rest.slice(
                    precedentPage * PAGE_SIZE,
                    (precedentPage + 1) * PAGE_SIZE,
                  );
                  const totalPages = Math.ceil(rest.length / PAGE_SIZE);

                  function renderPrecedentCard(
                    p: (typeof precs)[0],
                    isRecommended: boolean,
                  ) {
                    const truncated =
                      p.summary && p.summary.length > 100
                        ? `${p.summary.slice(0, 100)}...`
                        : p.summary;
                    const precKey = `${p.source_id}/${p.case_id}`;
                    const isExpanded = expandedPrecedent === precKey;
                    return (
                      <div key={p.case_id} className="precedent-card-wrap">
                        <button
                          type="button"
                          className={`precedent-card precedent-card-clickable ${isExpanded ? "precedent-card-active" : ""}`}
                          onClick={() =>
                            handlePrecedentClick(p.source_id, p.case_id)
                          }
                        >
                          <div className="precedent-card-header">
                            <strong>
                              {isRecommended && (
                                <span className="precedent-recommend-badge">
                                  추천
                                </span>
                              )}
                              {p.case_name}
                            </strong>
                            <div className="precedent-header-badges">
                              <span className="precedent-source-badge">
                                {p.source_label || "판례"}
                              </span>
                              {p.judgment_type && (
                                <span className="precedent-meta">
                                  {p.judgment_type}
                                </span>
                              )}
                            </div>
                          </div>
                          <span className="precedent-court">
                            {p.court_name}
                            {p.judgment_date ? ` · ${p.judgment_date}` : ""}
                          </span>
                          {!isExpanded && truncated && (
                            <p className="precedent-summary">{truncated}</p>
                          )}
                        </button>
                        {isExpanded && (
                          <div className="precedent-detail-inline">
                            {precedentLoading ? (
                              <p className="muted-note">
                                판례 상세 불러오는 중...
                              </p>
                            ) : precedentDetail?.sections?.length ? (
                              precedentDetail.sections.map((sec, i) => (
                                <div
                                  key={i}
                                  className="precedent-detail-section"
                                >
                                  <p className="section-kicker">{sec.label}</p>
                                  <div
                                    className="precedent-detail-content"
                                    dangerouslySetInnerHTML={{
                                      __html: sec.content,
                                    }}
                                  />
                                </div>
                              ))
                            ) : (
                              <p className="muted-note">
                                상세 내용을 불러올 수 없습니다.
                              </p>
                            )}
                            {p.external_url && (
                              <a
                                href={p.external_url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="precedent-external-link"
                              >
                                법제처에서 전문 보기
                              </a>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  }

                  return (
                    <article className="answer-card">
                      <p className="section-kicker">
                        유사 판례 ({precs.length}건)
                      </p>
                      <div className="precedent-list">
                        {recommended.map((p) => renderPrecedentCard(p, true))}
                      </div>
                      {rest.length > 0 && (
                        <>
                          <button
                            type="button"
                            className="precedent-more-toggle"
                            onClick={() => {
                              setShowMorePrecedents(!showMorePrecedents);
                              setPrecedentPage(0);
                            }}
                          >
                            {showMorePrecedents
                              ? "접기"
                              : `나머지 ${rest.length}건 더보기`}
                          </button>
                          {showMorePrecedents && (
                            <div className="precedent-rest">
                              <div className="precedent-list">
                                {restPage.map((p) =>
                                  renderPrecedentCard(p, false),
                                )}
                              </div>
                              {totalPages > 1 && (
                                <div className="precedent-pagination">
                                  <button
                                    type="button"
                                    disabled={precedentPage === 0}
                                    onClick={() =>
                                      setPrecedentPage((p) => p - 1)
                                    }
                                  >
                                    &#9664;
                                  </button>
                                  <span>
                                    {precedentPage + 1} / {totalPages}
                                  </span>
                                  <button
                                    type="button"
                                    disabled={precedentPage >= totalPages - 1}
                                    onClick={() =>
                                      setPrecedentPage((p) => p + 1)
                                    }
                                  >
                                    &#9654;
                                  </button>
                                </div>
                              )}
                            </div>
                          )}
                        </>
                      )}
                      <p className="precedent-disclaimer">
                        판례 정보는 참고용이며, 정확한 법적 판단은 전문가 상담이
                        필요합니다.
                      </p>
                    </article>
                  );
                })()}
            </div>

            {/* Citations Sidebar */}
            <aside className="answer-sidebar">
              <h4 className="evidence-rail-title">직접 인용된 조문</h4>
              <div className="evidence-scroll">
                <CitationsList citations={answer.citations} />
              </div>
            </aside>
          </div>
        </section>
      )}

      {/* ── Limit Exceeded Modal ─────────────────────────────── */}
      {showLimitModal && (
        <div className="modal-overlay" onClick={() => setShowLimitModal(false)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <h3 className="modal-title">무료 질문 한도 초과</h3>
            <p className="modal-desc">
              무료 질문 횟수({usage?.limit ?? "?"}회)를 모두 사용했습니다.
              <br />
              추가 질문이 필요하시면 문의해 주세요.
            </p>
            <div className="modal-actions">
              <button
                className="modal-close-btn"
                type="button"
                onClick={() => setShowLimitModal(false)}
              >
                닫기
              </button>
            </div>
          </div>
        </div>
      )}
    </SiteShell>
  );
}
