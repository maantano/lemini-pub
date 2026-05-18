export type SearchResult = {
  id: string;
  law_id: string;
  title: string;
  law_type: string | null;
  status: string | null;
  effective_date: string | null;
  score: number;
};

export type Citation = {
  chunk_id: string;
  law_id: string;
  law_title: string;
  article_no: string | null;
  article_title: string | null;
  chunk_type: string;
  quote: string;
  score: number;
};

export type PrecedentResult = {
  case_id: string;
  case_name: string;
  case_number: string;
  court_name: string;
  source_id: string;
  source_label: string;
  provider: string | null;
  judgment_date: string | null;
  judgment_type: string | null;
  summary: string | null;
  url: string | null;
  external_url: string | null;
};

export type PrecedentSource = {
  source_id: string;
  label: string;
  provider: string;
  request_format: string;
  default_enabled: boolean;
  description: string | null;
};

export type PrecedentSearchResponse = {
  query: string;
  results: PrecedentResult[];
  total_count: number;
  win_rate: number | null;
  source_counts: Record<string, number>;
  errors: Record<string, string>;
  note: string;
};

export type PrecedentDetailSection = {
  label: string;
  content: string;
};

export type PrecedentDetailResponse = {
  result: PrecedentResult;
  sections: PrecedentDetailSection[];
  note: string;
};

export type JudgmentBlock = {
  is_judgment_question: boolean;
  verdict: "likely_yes" | "likely_no" | "depends" | "needs_more_info" | "not_applicable";
  short_answer: string;
  reasoning: string;
  missing_facts: string[];
  typical_path: string;
  key_authorities?: Array<{ type?: string; ref: string; point?: string }>;
  contradicting_views?: string[];
  rejected_citations?: Array<{ ref: string; reason: string }>;
};

/* ── Document Review types ────────────────────────────── */

export type DocumentOverview = {
  nature: string;
  principals: string[];
  standpoint: string;
};

// v3: basis 가 문자열(레거시) 또는 구조화 객체 둘 다 지원
export type BasisStatute = {
  name: string;
  article?: string;
  clause?: string;
  raw?: string;
};
export type BasisCase = {
  court?: string;
  date?: string;
  case_no: string;
  holding_excerpt?: string;
};
export type BasisOrdinance = {
  issuer?: string;
  title?: string;
  number?: string;
  article?: string;
};
export type BasisVoluntaryCode = {
  issuer?: string;
  title?: string;
  article?: string;
};
export type BasisObject = {
  statutes?: BasisStatute[];
  cases?: BasisCase[];
  ordinances?: BasisOrdinance[];
  voluntary_codes?: BasisVoluntaryCode[];
};
export type Basis = string | BasisObject | null;

export type ReviewObservationIssue = {
  severity: string;
  concern: string;
  suggestion: string;
  basis: Basis;
};

export type ReviewObservation = {
  locator: string;
  locator_title: string;
  original_text: string;
  severity: string;
  issues: ReviewObservationIssue[];
};

export type ReviewGap = {
  topic: string;
  reason: string;
  suggestion: string;
};

export type ExternalConsideration = {
  section_title: string;
  topic: string;
  detail: string;
  suggestion: string | null;
};

export type FileReviewResponse = {
  files?: Array<{
    filename: string;
    extractedText: string;
    extractedLength: number;
  }>;
  fileCount?: number;
  totalExtractedLength?: number;
  documentOverview: DocumentOverview;
  documentType: string;
  summary: string;
  institutionalFrame: string;
  institutionalAxes: Array<Record<string, unknown>>;
  reviewAxes: string[];
  observations: ReviewObservation[];
  clauseReviews: ReviewObservation[];
  gaps: ReviewGap[];
  missingClauses: Array<Record<string, unknown>>;
  externalConsiderations: ExternalConsideration[];
  riskScenarios: Array<Record<string, unknown>>;
  overallRisk: string;
  keyPoints: string[];
  actionItems: string[];
  rejectedCitations: Array<{ ref: string; reason: string }>;
  judgment: JudgmentBlock | null;
  disclaimer: string;
};

export type ChatAnswer = {
  summary: string;
  answer: string;
  grounded: boolean;
  citations: Citation[];
  decision_factors: string[];
  action_items: string[];
  retrieved_chunk_ids: string[];
  precedents: PrecedentResult[];
  saved: boolean;
  warning: string | null;
  judgment?: JudgmentBlock | null;
};

export type LawDetailResponse = {
  document: {
    id: string;
    law_id: string;
    law_mst: string | null;
    title: string;
    law_type: string | null;
    ministry: Record<string, unknown> | null;
    promulgation_date: string | null;
    effective_date: string | null;
    status: string | null;
    source_url: string | null;
    created_at: string;
  };
  chunks: Array<{
    id: string;
    chunk_type: string;
    chapter_title: string | null;
    section_title: string | null;
    article_no: string | null;
    article_title: string | null;
    text: string;
    order_index: number;
    token_count: number;
    created_at: string;
  }>;
};

export type SavedAnswer = {
  id: string;
  user_id: string | null;
  question: string;
  summary: string;
  answer: string;
  citations: Citation[];
  created_at: string;
};

/* ── Resolution / Intake types ─────────────────────────── */

export type IntakeQuestion = {
  id: string;
  question: string;
  type: "yes_no" | "yes_no_unknown" | "number" | "date" | "choice" | "text";
  options?: string[];
  unit?: string;
  legalRelevance?: string;
};

export type IntakeDomainConfig = {
  id: string;
  title: string;
  questions: IntakeQuestion[];
};

export type FactAnalysisItem = {
  fact: string;
  legalBasis: string;
  impact: string;
};

export type ActionStep = {
  order: number;
  action: string;
  deadline: string;
  cost: string;
  documents: string[];
  agency: string | null;
  detail: string;
};

export type IntakeAnalysis = {
  summary: string;
  answer: string;
  favorableFacts: FactAnalysisItem[];
  cautionaryFacts: FactAnalysisItem[];
  recommendedEvidence: { item: string; reason: string; howToGet: string }[];
  actionPlan: ActionStep[];
  keyDeadlines: { item: string; deadline: string; consequence: string }[];
  estimatedCost: string;
  estimatedTimeline: string;
  winRate: string | null;
  citations: Citation[];
  precedents: PrecedentResult[];
};

/* ── History types ────────────────────────────────────── */

export type HistoryItem = {
  id: string;
  thread_id: string | null;
  turn_index: number;
  request_type: string;
  question: string;
  response: Record<string, unknown>;
  has_attachments: boolean;
  attachment_names: string[];
  created_at: string;
};

export type AdminStats = {
  documents: number;
  chunks: number;
  saved_answers: number;
  feedback: number;
  estimated_text_bytes: number;
  estimated_vector_bytes: number;
  relation_bytes: Record<string, number>;
};
