"use client";

import { useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";

import { SiteShell } from "../../../components/site-shell";
import { API_BASE_URL } from "../../../lib/api";

type PolicyStatus = "enabled" | "disabled" | "partial";
type DataQualityStatus = "trusted" | "partial" | "missing";
type DataQualityItem = {
  label: string;
  status: DataQualityStatus;
  detail: string;
  total_rows: number;
  today_count: number;
  last_seen_at: string | null;
};

type DashboardData = {
  schema_mode: "legacy" | "current";
  generated_at: string;
  event_tracking_started_at: string | null;
  runtime: {
    env: string;
    api_base_url: string;
    app_base_url: string;
    timezone: string;
    daily_question_limit: number;
    feature_flags: Record<string, boolean>;
  };
  today: {
    date: string;
    events: number;
    answered_conversations: number;
    counted_requests: number;
    count_gap: number;
    active_users: number;
    guest_sessions: number;
    reviews: number;
    drafts: number;
    feedback: number;
    logins: number;
    new_users: number;
  };
  operations: Array<{
    event_type: string;
    label: string;
    today: number;
    last_7d: number;
    last_seen_at: string | null;
  }>;
  questions: {
    daily_trend: Array<{
      date: string;
      total: number;
      chat: number;
      deep_dive: number;
      reviews: number;
      drafts: number;
      searches: number;
      feedback: number;
      logins: number;
    }>;
  };
  conversations: {
    today_completed: number;
    last_7d_completed: number;
    daily_trend: Array<{
      date: string;
      total: number;
    }>;
    last_completed_at: string | null;
  };
  users: {
    total_registered: number;
    total_all: number;
    total_active: number;
    guest_sessions: number;
    recent_signups: { nickname: string; created_at: string }[];
  };
  legacy_usage: {
    today_counted_requests: number;
    today_active_users: number;
    by_users: number;
    by_guests: number;
    total: number;
  };
  cache: {
    response_cache_rows: number;
    precedent_cache_rows: number;
    chat_history_rows: number;
  };
  quality: {
    feedback_total: number;
    saved_answers_total: number;
  };
  data_quality: Record<string, DataQualityItem>;
  storage: {
    law_db_mb: number;
    vector_mb: number;
    state_db_mb: number;
  };
  recent_activity: Array<{
    event_type: string;
    label: string;
    channel: string;
    actor_kind: string;
    created_at: string;
    summary: string;
  }>;
  recent_questions: { question: string; created_at: string }[];
  notes: string[];
  policy_status: Record<
    string,
    {
      label: string;
      status: PolicyStatus;
      detail: string;
    }
  >;
};

const FEATURE_FLAG_LABELS: Record<string, string> = {
  vector_search: "벡터 검색",
  server_chat_history: "서버 저장 답변",
  admin_upload_ui: "관리자 업로드 UI",
  article_segment: "조문 세분화",
  appendix_search: "별첨 검색",
};

const POLICY_STATUS_LABELS: Record<PolicyStatus, string> = {
  enabled: "활성",
  disabled: "비활성",
  partial: "부분 적용",
};

const DATA_QUALITY_LABELS: Record<DataQualityStatus, string> = {
  trusted: "신뢰 가능",
  partial: "보조 지표",
  missing: "미수집",
};

const ADMIN_DASHBOARD_PROXY_PATH = "/api/admin/dashboard/";

function formatDateTime(value: string | null | undefined) {
  if (!value) return "-";
  return new Date(value).toLocaleString("ko-KR");
}

function formatShortDateTime(value: string | null | undefined) {
  if (!value) return "-";
  return new Date(value).toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatCount(value: number) {
  return value.toLocaleString("ko-KR");
}

function formatMetric(value: number, suffix = "") {
  return Number.isFinite(value) ? `${formatCount(value)}${suffix}` : "미제공";
}

function actorLabel(actorKind: string) {
  if (actorKind === "authenticated") return "로그인";
  if (actorKind === "guest") return "비로그인";
  return actorKind;
}

function dataQualityPill(status: DataQualityStatus): PolicyStatus {
  if (status === "trusted") return "enabled";
  if (status === "partial") return "partial";
  return "disabled";
}

function metricValue(value: number, missing = false) {
  if (missing) return "미수집";
  return Number.isFinite(value) ? formatCount(value) : "미제공";
}

function asNumber(value: unknown, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asString(value: unknown, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function asNullableString(value: unknown) {
  return typeof value === "string" ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeDataQuality(
  raw: unknown,
  {
    isLegacyResponse,
    todayEvents,
    answeredConversations,
    countedRequests,
    chatHistoryTotal,
  }: {
    isLegacyResponse: boolean;
    todayEvents: number;
    answeredConversations: number;
    countedRequests: number;
    chatHistoryTotal: number;
  },
) {
  const quality = isRecord(raw) ? raw : {};
  const requestEventsRaw = isRecord(quality.request_events) ? quality.request_events : {};
  const chatHistoryRaw = isRecord(quality.chat_history) ? quality.chat_history : {};
  const usageCountsRaw = isRecord(quality.usage_counts) ? quality.usage_counts : {};

  const requestEvents: DataQualityItem = {
    label: asString(requestEventsRaw.label, "운영 이벤트"),
    status: (requestEventsRaw.status as DataQualityStatus) || (todayEvents > 0 ? "trusted" : "missing"),
    detail: asString(
      requestEventsRaw.detail,
      isLegacyResponse
        ? "현재 연결된 API는 request_events 기반 운영 이벤트를 아직 내려주지 않습니다."
        : "응답에 data_quality가 없어 기본값으로 보정했습니다.",
    ),
    total_rows: asNumber(requestEventsRaw.total_rows, 0),
    today_count: asNumber(requestEventsRaw.today_count, todayEvents),
    last_seen_at: asNullableString(requestEventsRaw.last_seen_at),
  };

  const chatHistory: DataQualityItem = {
    label: asString(chatHistoryRaw.label, "답변 완료 기록"),
    status: (chatHistoryRaw.status as DataQualityStatus)
      || (isLegacyResponse ? "partial" : answeredConversations > 0 ? "trusted" : "partial"),
    detail: asString(
      chatHistoryRaw.detail,
      isLegacyResponse
        ? "구버전 응답에서는 오늘 완료 수를 직접 주지 않아 최근 질문 목록에서 보이는 범위만 반영했습니다."
        : "답변 완료 수는 chat_history 기준으로 보정했습니다.",
    ),
    total_rows: asNumber(chatHistoryRaw.total_rows, chatHistoryTotal),
    today_count: asNumber(chatHistoryRaw.today_count, answeredConversations),
    last_seen_at: asNullableString(chatHistoryRaw.last_seen_at),
  };

  const usageCounts: DataQualityItem = {
    label: asString(usageCountsRaw.label, "한도 카운터"),
    status: (usageCountsRaw.status as DataQualityStatus) || "partial",
    detail: asString(
      usageCountsRaw.detail,
      "한도 카운트는 usage_counts 기준 보조 지표입니다.",
    ),
    total_rows: asNumber(usageCountsRaw.total_rows, 0),
    today_count: asNumber(usageCountsRaw.today_count, countedRequests),
    last_seen_at: asNullableString(usageCountsRaw.last_seen_at),
  };

  return {
    request_events: requestEvents,
    chat_history: chatHistory,
    usage_counts: usageCounts,
  };
}

function normalizeDashboardData(raw: unknown): DashboardData {
  const source = isRecord(raw) ? raw : {};
  const legacyQuestionsSource = isRecord(source.questions) ? source.questions : {};
  const legacyUsageRaw = isRecord(source.legacy_usage) ? source.legacy_usage : {};
  const todayRaw = isRecord(source.today) ? source.today : {};
  const runtimeRaw = isRecord(source.runtime) ? source.runtime : {};
  const usersRaw = isRecord(source.users) ? source.users : {};
  const cacheRaw = isRecord(source.cache) ? source.cache : {};
  const qualityRaw = isRecord(source.quality) ? source.quality : {};
  const storageRaw = isRecord(source.storage) ? source.storage : {};
  const conversationsRaw = isRecord(source.conversations) ? source.conversations : {};
  const isLegacyResponse = !isRecord(source.data_quality) && typeof source.chat_history === "number";

  const normalizedRecentQuestions = Array.isArray(source.recent_questions)
    ? source.recent_questions.map((item) => {
        const row = isRecord(item) ? item : {};
        return {
          question: asString(row.question, ""),
          created_at: asString(row.created_at, ""),
        };
      })
    : [];

  const todayDate = asString(todayRaw.date, new Date().toISOString().slice(0, 10));
  const recentQuestionTodayCount = normalizedRecentQuestions.filter(
    (item) => item.created_at.slice(0, 10) === todayDate,
  ).length;

  const legacyUsage = {
    today_counted_requests: asNumber(
      legacyUsageRaw.today_counted_requests,
      asNumber(todayRaw.questions, 0),
    ),
    today_active_users: asNumber(
      legacyUsageRaw.today_active_users,
      asNumber(todayRaw.active_users, 0),
    ),
    by_users: asNumber(legacyUsageRaw.by_users, asNumber(legacyQuestionsSource.by_users, 0)),
    by_guests: asNumber(legacyUsageRaw.by_guests, asNumber(legacyQuestionsSource.by_guests, 0)),
    total: asNumber(legacyUsageRaw.total, asNumber(legacyQuestionsSource.total, 0)),
  };

  const answeredConversations = asNumber(
    todayRaw.answered_conversations,
    asNumber(conversationsRaw.today_completed, isLegacyResponse ? recentQuestionTodayCount : 0),
  );
  const countedRequests = asNumber(
    todayRaw.counted_requests,
    legacyUsage.today_counted_requests,
  );
  const events = asNumber(todayRaw.events, 0);

  return {
    schema_mode: isLegacyResponse ? "legacy" : "current",
    generated_at: asString(source.generated_at, new Date().toISOString()),
    event_tracking_started_at: asNullableString(source.event_tracking_started_at),
    runtime: {
      env: asString(runtimeRaw.env, "local"),
      api_base_url: asString(runtimeRaw.api_base_url, API_BASE_URL),
      app_base_url: asString(runtimeRaw.app_base_url, isLegacyResponse ? "미제공" : ""),
      timezone: asString(runtimeRaw.timezone, "Asia/Seoul"),
      daily_question_limit: asNumber(runtimeRaw.daily_question_limit, 0),
      feature_flags: isRecord(runtimeRaw.feature_flags)
        ? Object.fromEntries(
            Object.entries(runtimeRaw.feature_flags).map(([key, value]) => [key, Boolean(value)]),
          )
        : {},
    },
    today: {
      date: todayDate,
      events,
      answered_conversations: answeredConversations,
      counted_requests: countedRequests,
      count_gap: asNumber(todayRaw.count_gap, countedRequests - answeredConversations),
      active_users: asNumber(todayRaw.active_users, 0),
      guest_sessions: asNumber(todayRaw.guest_sessions, 0),
      reviews: asNumber(todayRaw.reviews, 0),
      drafts: asNumber(todayRaw.drafts, 0),
      feedback: asNumber(todayRaw.feedback, Number.NaN),
      logins: asNumber(todayRaw.logins, 0),
      new_users: asNumber(todayRaw.new_users, 0),
    },
    operations: Array.isArray(source.operations)
      ? source.operations.map((item) => {
          const row = isRecord(item) ? item : {};
          return {
            event_type: asString(row.event_type, "unknown"),
            label: asString(row.label, "알 수 없음"),
            today: asNumber(row.today, 0),
            last_7d: asNumber(row.last_7d, 0),
            last_seen_at: asNullableString(row.last_seen_at),
          };
        })
      : [],
    questions: {
      daily_trend: isRecord(source.questions) && Array.isArray(source.questions.daily_trend)
        ? source.questions.daily_trend.map((item) => {
            const row = isRecord(item) ? item : {};
            return {
              date: asString(row.date, ""),
              total: asNumber(row.total, asNumber(row.count, 0)),
              chat: asNumber(row.chat, 0),
              deep_dive: asNumber(row.deep_dive, 0),
              reviews: asNumber(row.reviews, 0),
              drafts: asNumber(row.drafts, 0),
              searches: asNumber(row.searches, 0),
              feedback: asNumber(row.feedback, 0),
              logins: asNumber(row.logins, 0),
            };
          })
        : [],
    },
    conversations: {
      today_completed: asNumber(conversationsRaw.today_completed, answeredConversations),
      last_7d_completed: asNumber(conversationsRaw.last_7d_completed, 0),
      daily_trend: Array.isArray(conversationsRaw.daily_trend)
        ? conversationsRaw.daily_trend.map((item) => {
            const row = isRecord(item) ? item : {};
            return {
              date: asString(row.date, ""),
              total: asNumber(row.total, 0),
            };
          })
        : [],
      last_completed_at: asNullableString(conversationsRaw.last_completed_at),
    },
    users: {
      total_registered: asNumber(usersRaw.total_registered, 0),
      total_all: asNumber(usersRaw.total_all, 0),
      total_active: asNumber(usersRaw.total_active, 0),
      guest_sessions: asNumber(usersRaw.guest_sessions, 0),
      recent_signups: Array.isArray(usersRaw.recent_signups)
        ? usersRaw.recent_signups.map((item) => {
            const row = isRecord(item) ? item : {};
            return {
              nickname: asString(row.nickname, "이름 없음"),
              created_at: asString(row.created_at, ""),
            };
          })
        : [],
    },
    legacy_usage: legacyUsage,
    cache: {
      response_cache_rows: asNumber(cacheRaw.response_cache_rows, asNumber(cacheRaw.response_cache, Number.NaN)),
      precedent_cache_rows: asNumber(cacheRaw.precedent_cache_rows, asNumber(cacheRaw.precedent_cache, Number.NaN)),
      chat_history_rows: asNumber(cacheRaw.chat_history_rows, asNumber(source.chat_history, Number.NaN)),
    },
    quality: {
      feedback_total: asNumber(qualityRaw.feedback_total, Number.NaN),
      saved_answers_total: asNumber(qualityRaw.saved_answers_total, Number.NaN),
    },
    data_quality: normalizeDataQuality(source.data_quality, {
      isLegacyResponse,
      todayEvents: events,
      answeredConversations,
      countedRequests,
      chatHistoryTotal: asNumber(source.chat_history, Number.NaN),
    }),
    storage: {
      law_db_mb: asNumber(storageRaw.law_db_mb, 0),
      vector_mb: asNumber(storageRaw.vector_mb, 0),
      state_db_mb: asNumber(storageRaw.state_db_mb, Number.NaN),
    },
    recent_activity: Array.isArray(source.recent_activity)
      ? source.recent_activity.map((item) => {
          const row = isRecord(item) ? item : {};
          return {
            event_type: asString(row.event_type, "unknown"),
            label: asString(row.label, "알 수 없음"),
            channel: asString(row.channel, "unknown"),
            actor_kind: asString(row.actor_kind, "unknown"),
            created_at: asString(row.created_at, ""),
            summary: asString(row.summary, ""),
          };
        })
      : [],
    recent_questions: normalizedRecentQuestions,
    notes: Array.isArray(source.notes)
      ? source.notes.filter((item): item is string => typeof item === "string")
      : isLegacyResponse
        ? [
            "현재 연결된 API는 구버전 dashboard 응답을 반환하고 있습니다.",
            "답변 완료/한도 카운트/운영 이벤트를 완전히 분리한 새 스냅샷은 아직 이 서버에서 내려오지 않습니다.",
          ]
      : [],
    policy_status: isRecord(source.policy_status)
      ? Object.fromEntries(
          Object.entries(source.policy_status).map(([key, value]) => {
            const row = isRecord(value) ? value : {};
            return [key, {
              label: asString(row.label, key),
              status: (row.status as PolicyStatus) || "partial",
              detail: asString(row.detail, ""),
            }];
          }),
        )
      : {},
  };
}

/** 긴 텍스트를 기본 접힘 상태로 보여주는 컴포넌트 */
function ExpandableText({ text, previewLength = 150 }: { text: string; previewLength?: number }) {
  const [expanded, setExpanded] = useState(false);
  if (!text) return null;
  if (text.length <= previewLength) return <>{text}</>;
  return (
    <>
      {expanded ? text : `${text.slice(0, previewLength)}...`}
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        style={{
          marginLeft: 4,
          padding: "0 4px",
          fontSize: 11,
          color: "var(--accent)",
          background: "none",
          border: "none",
          cursor: "pointer",
          textDecoration: "underline",
        }}
      >
        {expanded ? "접기" : "더 보기"}
      </button>
    </>
  );
}

type DashboardTab = "overview" | "conversations";

export default function DashboardPage() {
  const [apiKey, setApiKey] = useState("");
  const [authenticated, setAuthenticated] = useState(false);
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<DashboardTab>("overview");

  // 대화 내역 탭 state
  const [historyItems, setHistoryItems] = useState<any[]>([]);
  const [historyFilter, setHistoryFilter] = useState("");
  const [historyLoading, setHistoryLoading] = useState(false);
  const [expandedThread, setExpandedThread] = useState<string | null>(null);
  const [threadTurns, setThreadTurns] = useState<any[]>([]);


  async function fetchDashboard(key: string) {
    if (!key.trim()) {
      setError("Admin API Key를 입력해 주세요.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(ADMIN_DASHBOARD_PROXY_PATH, {
        headers: { "X-Admin-Api-Key": key },
        cache: "no-store",
      });
      if (!res.ok) {
        let nextError = res.status === 401 ? "인증 실패" : "요청 실패";
        try {
          const payload = await res.json();
          if (isRecord(payload) && typeof payload.detail === "string") {
            nextError = res.status === 401 ? "인증 실패" : payload.detail;
          }
        } catch {
          // ignore malformed error bodies
        }
        if (res.status === 502) {
          nextError = "프록시 서버가 운영 API에 연결하지 못했습니다.";
        }
        setError(nextError);
        return;
      }
      const nextData = normalizeDashboardData(await res.json());
      setData(nextData);
      setAuthenticated(true);
    } catch {
      setError("대시보드 프록시 연결 실패");
    } finally {
      setLoading(false);
    }
  }

  // 폴링 제거 — 초기 로드 + 수동 새로고침 버튼으로만 갱신

  async function fetchHistory(filterType?: string) {
    setHistoryLoading(true);
    try {
      const params = new URLSearchParams({ limit: "50" });
      if (filterType) params.set("request_type", filterType);
      const res = await fetch(`${API_BASE_URL}/v1/history?${params}`, {
        headers: { "X-Admin-Api-Key": apiKey },
        cache: "no-store",
      });
      if (res.ok) setHistoryItems((await res.json()).items || []);
    } catch {} finally { setHistoryLoading(false); }
  }

  async function fetchThread(threadId: string) {
    if (expandedThread === threadId) { setExpandedThread(null); setThreadTurns([]); return; }
    setExpandedThread(threadId);
    try {
      const res = await fetch(`${API_BASE_URL}/v1/history/thread/${threadId}`, {
        headers: { "X-Admin-Api-Key": apiKey },
        cache: "no-store",
      });
      if (res.ok) setThreadTurns((await res.json()).turns || []);
    } catch { setThreadTurns([]); }
  }

  useEffect(() => {
    if (!authenticated) return;
    if (activeTab === "conversations") fetchHistory(historyFilter || undefined);
  }, [activeTab, authenticated, historyFilter]);

  if (!authenticated) {
    return (
      <SiteShell
        eyebrow="Operations"
        title="운영 대시보드"
        description="지표가 어떤 저장소에서 왔는지와 실제 수집 상태를 함께 확인합니다."
      >
        <section className="panel ops-auth-panel">
          <div className="stack-sm">
            <p className="section-kicker">Admin Access</p>
            <h2 className="ops-panel-title">관리자 키로 접속</h2>
            <p className="muted-note">
              답변 완료 기록, 한도 카운터, 운영 이벤트 추적 상태를 같은 기준으로 비교해 봅니다.
            </p>
          </div>
          <div className="ops-auth-row">
            <input
              type="password"
              placeholder="Admin API Key"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && fetchDashboard(apiKey)}
            />
            <button onClick={() => fetchDashboard(apiKey)} disabled={loading}>
              {loading ? "확인 중..." : "접속"}
            </button>
          </div>
          {error && <p className="error-note">{error}</p>}
        </section>
      </SiteShell>
    );
  }

  if (!data) {
    return (
      <SiteShell
        eyebrow="Operations"
        title="운영 대시보드"
        description="지표가 어떤 저장소에서 왔는지와 실제 수집 상태를 함께 확인합니다."
      >
        <section className="panel stack-sm">
          <p className="section-kicker">Load Error</p>
          <h2 className="ops-panel-title">데이터를 불러오지 못했습니다.</h2>
          <p className="error-note">{error ?? "API 키와 서버 상태를 확인해 주세요."}</p>
          <div className="button-row">
            <button className="secondary-button" type="button" onClick={() => fetchDashboard(apiKey)}>
              다시 시도
            </button>
          </div>
        </section>
      </SiteShell>
    );
  }

  const requestEventsQuality = data.data_quality.request_events;
  const requestEventsMissing = requestEventsQuality?.status === "missing";
  const isLegacyApi = data.schema_mode === "legacy";
  const policyEntries = Object.values(data.policy_status);
  const featureFlagEntries = Object.entries(data.runtime.feature_flags);
  const notes = data.notes.length > 0 ? data.notes : ["운영 메모가 아직 없습니다."];

  const TAB_LABELS: Record<DashboardTab, string> = {
    overview: "개요",
    conversations: "대화 내역",
  };

  const REQUEST_TYPE_LABELS: Record<string, string> = {
    "": "전체",
    chat: "질문 답변",
    review: "문서 검토",
  };

  // "review"로 필터 시 review_file + review_text 모두 포함

  return (
    <SiteShell
      eyebrow="Operations"
      title="운영 대시보드"
      description="서비스 현황, 대화 내역, 저장된 답변을 한 곳에서 관리합니다."
    >
      {/* 탭 네비게이션 */}
      <nav className="dash-tab-nav">
        {(Object.keys(TAB_LABELS) as DashboardTab[]).map((tab) => (
          <button
            key={tab}
            type="button"
            className={`dash-tab-btn ${activeTab === tab ? "dash-tab-active" : ""}`}
            onClick={() => setActiveTab(tab)}
          >
            {TAB_LABELS[tab]}
          </button>
        ))}
      </nav>

      {/* ── 대화 내역 탭 ── */}
      {activeTab === "conversations" && (
        <div className="stack-lg">
          <div className="history-page-filters">
            <button type="button" className={`history-filter-chip ${!historyFilter ? "history-filter-active" : ""}`} onClick={() => setHistoryFilter("")}>전체</button>
            <button type="button" className={`history-filter-chip ${historyFilter === "chat" ? "history-filter-active" : ""}`} onClick={() => setHistoryFilter("chat")}>질문 답변</button>
            <button type="button" className={`history-filter-chip ${historyFilter === "review" ? "history-filter-active" : ""}`} onClick={() => setHistoryFilter("review")}>문서 검토</button>
          </div>
          {historyLoading ? (
            <div className="panel"><p className="muted-note">불러오는 중...</p></div>
          ) : historyItems.length === 0 ? (
            <div className="panel"><p className="muted-note">대화 기록이 없습니다.</p></div>
          ) : (
            <div className="history-page-list">
              {historyItems.map((item: any) => (
                <div key={item.id} className="history-item-card">
                  <div className="history-item-header">
                    <span className="history-type-badge" style={{ background: item.request_type?.includes("review") ? "var(--success)" : "var(--accent)" }}>
                      {item.request_type?.includes("review") ? "문서 검토" : "질문 답변"}
                    </span>
                    {item.has_attachments && <span>📎</span>}
                    {item.thread_id && (
                      <button type="button" className="history-filter-chip" style={{fontSize: 11, padding: "1px 6px"}} onClick={() => fetchThread(item.thread_id)}>
                        {expandedThread === item.thread_id ? "스레드 접기" : "스레드 보기"}
                      </button>
                    )}
                    <span style={{ fontSize: 11, color: "var(--text-tertiary)" }}>
                      {item.user_id && item.user_id !== "anonymous" ? item.user_id : (item.session_id ? item.session_id.slice(0, 8) + "…" : "게스트")}
                    </span>
                    <span className="history-date">{formatShortDateTime(item.created_at)}</span>
                  </div>
                  <p className="history-question-preview" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{item.question || ""}</p>
                  {item.response && (() => {
                    const resp = typeof item.response === "object" ? item.response : {};
                    const text = resp.summary || resp.answer || resp.message || "";
                    return text ? (
                      <p className="history-response-preview" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{text}</p>
                    ) : null;
                  })()}
                  {expandedThread === item.thread_id && threadTurns.length > 0 && (
                    <div className="history-thread-turns" style={{marginTop: 8}}>
                      {threadTurns.map((turn: any) => (
                        <div key={turn.id} className="history-turn-card">
                          <div className="history-turn-header">
                            <span className="history-type-badge history-type-badge-sm" style={{background: "var(--accent)"}}>턴 {turn.turn_index + 1}</span>
                            <span style={{fontSize: 11, color: "var(--text-tertiary)"}}>{REQUEST_TYPE_LABELS[turn.request_type] || turn.request_type}</span>
                          </div>
                          <p className="history-turn-question" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}><strong>Q:</strong> {turn.question || ""}</p>
                          <p className="history-turn-response" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}><strong>A:</strong> {typeof turn.response === "object" ? (turn.response.summary || turn.response.answer || turn.response.message || "") : ""}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── 개요 탭 ── */}
      {activeTab === "overview" && <div className="stack-lg">
        <div className="ops-panel-head">
          <p className="muted-note">갱신 {formatDateTime(data.generated_at)}</p>
          <button className="secondary-button" type="button" onClick={() => fetchDashboard(apiKey)} disabled={loading}>
            {loading ? "갱신 중..." : "새로고침"}
          </button>
        </div>

        {/* 핵심 지표 4개 */}
        <section className="panel stack-md">
          <div className="stat-grid ops-stat-grid">
            <article className="ops-stat-highlight">
              <span>전체 사용자</span>
              <strong>{formatCount(data.users.total_all)}</strong>
              <p className="ops-stat-note">로그인 {formatCount(data.users.total_registered)} · 게스트 {formatCount(data.users.guest_sessions)}</p>
            </article>
            <article className="ops-stat-highlight">
              <span>전체 질문</span>
              <strong>{formatMetric(data.cache.chat_history_rows)}</strong>
              <p className="ops-stat-note">오늘 +{formatCount(data.today.answered_conversations)}</p>
            </article>
            <article>
              <span>오늘 답변</span>
              <strong>{formatCount(data.today.answered_conversations)}</strong>
              <p className="ops-stat-note">채팅 + 심층 분석</p>
            </article>
            <article>
              <span>오늘 신규 가입</span>
              <strong>{formatCount(data.today.new_users)}</strong>
              <p className="ops-stat-note">누적 {formatCount(data.users.total_registered)}</p>
            </article>
          </div>
        </section>

        {/* 7일 추이 차트 */}
        <section className="panel stack-md">
          <p className="section-kicker">7-Day Trend</p>
          {data.conversations.daily_trend.length > 0 ? (
            <div style={{ width: "100%", height: 220 }}>
              <ResponsiveContainer>
                <BarChart data={data.conversations.daily_trend.map((item) => {
                  const ev = data.questions.daily_trend.find((e) => e.date === item.date);
                  return {
                    date: item.date.slice(5),
                    답변: item.total,
                    검토: ev?.reviews ?? 0,
                  };
                })}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="date" tick={{ fontSize: 12 }} stroke="var(--text-tertiary)" />
                  <YAxis tick={{ fontSize: 12 }} stroke="var(--text-tertiary)" allowDecimals={false} />
                  <Tooltip contentStyle={{ fontSize: 13, borderRadius: 8, border: "1px solid var(--border)" }} />
                  <Bar dataKey="답변" fill="var(--accent)" radius={[4, 4, 0, 0]} />
                  <Bar dataKey="검토" fill="var(--success)" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <p className="muted-note">아직 데이터가 없습니다.</p>
          )}
        </section>

        {/* 최근 질문 */}
        <section className="panel stack-md">
          <p className="section-kicker">Recent Questions</p>
          {data.recent_questions.length > 0 ? (
            <div className="dash-list">
              {data.recent_questions.map((question, index) => (
                <div key={`${question.created_at}-${index}`} className="dash-list-item" style={{ flexDirection: "column", alignItems: "stretch", gap: 6 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span style={{ fontSize: 12, fontWeight: 600, color: (question as any).actor_type === "user" ? "var(--accent)" : "var(--text-tertiary)" }}>
                      {(question as any).actor || "알 수 없음"}
                      {(question as any).session_id && (question as any).actor_type === "guest" && (
                        <span style={{ fontWeight: 400, marginLeft: 4 }}>({(question as any).session_id})</span>
                      )}
                    </span>
                    <span className="dash-list-date">{formatShortDateTime(question.created_at)}</span>
                  </div>
                  <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.6, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                    {question.question}
                  </p>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted-note">아직 질문 기록이 없습니다.</p>
          )}
        </section>
      </div>}
    </SiteShell>
  );
}
