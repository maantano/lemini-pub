PRAGMA journal_mode = DELETE;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS law_documents (
  id TEXT PRIMARY KEY,
  law_id TEXT NOT NULL UNIQUE,
  law_mst TEXT,
  title TEXT NOT NULL,
  title_normalized TEXT NOT NULL,
  law_type TEXT,
  ministry TEXT,
  promulgation_date TEXT,
  effective_date TEXT,
  status TEXT DEFAULT 'active',
  source_url TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  document_type TEXT DEFAULT 'statute',
  license_policy TEXT DEFAULT 'statute_public',
  citation_mode TEXT DEFAULT 'full',
  issuer TEXT,
  authority_endorsement TEXT,
  source TEXT,
  source_fetched_at TEXT,
  source_collector TEXT,
  content_hash TEXT,
  domain_tags TEXT,
  repealed_at TEXT
);

CREATE INDEX IF NOT EXISTS law_documents_title_normalized_idx
  ON law_documents (title_normalized);

CREATE INDEX IF NOT EXISTS law_documents_document_type_idx
  ON law_documents (document_type);

CREATE INDEX IF NOT EXISTS law_documents_status_idx
  ON law_documents (status);

CREATE INDEX IF NOT EXISTS law_documents_content_hash_idx
  ON law_documents (content_hash);

CREATE TABLE IF NOT EXISTS law_documents_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  law_id TEXT NOT NULL,
  snapshot_at TEXT NOT NULL,
  content_hash TEXT,
  title TEXT,
  promulgation_date TEXT,
  effective_date TEXT,
  status TEXT,
  raw_markdown TEXT,
  source TEXT,
  source_fetched_at TEXT
);

CREATE INDEX IF NOT EXISTS law_documents_history_law_id_idx
  ON law_documents_history (law_id, snapshot_at DESC);

CREATE TABLE IF NOT EXISTS law_chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES law_documents(id) ON DELETE CASCADE,
  law_id TEXT NOT NULL,
  chunk_type TEXT NOT NULL,
  chapter_title TEXT,
  section_title TEXT,
  article_no TEXT,
  article_title TEXT,
  text TEXT NOT NULL,
  order_index INTEGER NOT NULL,
  token_count INTEGER NOT NULL DEFAULT 0,
  has_embedding INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS law_chunks_document_order_idx
  ON law_chunks (document_id, order_index);

CREATE INDEX IF NOT EXISTS law_chunks_law_article_idx
  ON law_chunks (law_id, article_no);

CREATE INDEX IF NOT EXISTS law_chunks_chunk_type_idx
  ON law_chunks (chunk_type);

CREATE TABLE IF NOT EXISTS law_aliases (
  id TEXT PRIMARY KEY,
  law_id TEXT NOT NULL REFERENCES law_documents(law_id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  alias_normalized TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (law_id, alias_normalized)
);

CREATE INDEX IF NOT EXISTS law_aliases_alias_normalized_idx
  ON law_aliases (alias_normalized);

CREATE VIRTUAL TABLE IF NOT EXISTS law_search_fts USING fts5(
  chunk_id UNINDEXED,
  law_id UNINDEXED,
  law_title,
  search_text,
  article_no,
  article_title,
  tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS collector_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT,
  fetched_count INTEGER DEFAULT 0,
  changed_count INTEGER DEFAULT 0,
  failed_count INTEGER DEFAULT 0,
  error_message TEXT,
  details TEXT
);

CREATE INDEX IF NOT EXISTS collector_runs_source_started_idx
  ON collector_runs (source_id, started_at DESC);

CREATE TABLE IF NOT EXISTS association_notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  association TEXT NOT NULL,
  contact_email TEXT,
  notice_sent_at TEXT,
  activation_date TEXT,
  status TEXT DEFAULT 'pending',
  objection_at TEXT,
  objection_reason TEXT,
  notes TEXT
);

CREATE INDEX IF NOT EXISTS association_notifications_status_idx
  ON association_notifications (status);

CREATE TABLE IF NOT EXISTS precedent_search_cache (
  key TEXT PRIMARY KEY,
  keywords TEXT,
  precedent_ids TEXT,
  cached_at TEXT NOT NULL,
  expires_at TEXT
);

CREATE INDEX IF NOT EXISTS precedent_search_cache_expires_idx
  ON precedent_search_cache (expires_at);

CREATE TABLE IF NOT EXISTS precedent_doc_cache (
  precedent_id TEXT PRIMARY KEY,
  title TEXT,
  court TEXT,
  case_no TEXT,
  judgment_date TEXT,
  case_type TEXT,
  body TEXT,
  fetched_at TEXT NOT NULL,
  source_url TEXT,
  -- M4 확장 (DRF JSON 응답 세분화):
  holding TEXT,              -- 판시사항
  summary TEXT,              -- 판결요지
  referenced_statutes TEXT,  -- 참조조문
  referenced_cases TEXT,     -- 참조판례
  judgment_type TEXT,        -- 판결유형 (판결/결정/명령)
  court_type_code TEXT,      -- 법원종류코드
  case_type_code TEXT,       -- 사건종류코드
  source TEXT,               -- 'drf-api' | 'on-demand' | ...
  content_hash TEXT
);

CREATE INDEX IF NOT EXISTS precedent_doc_cache_judgment_date_idx
  ON precedent_doc_cache (judgment_date DESC);
CREATE INDEX IF NOT EXISTS precedent_doc_cache_court_idx
  ON precedent_doc_cache (court);
CREATE INDEX IF NOT EXISTS precedent_doc_cache_case_type_idx
  ON precedent_doc_cache (case_type);

CREATE TABLE IF NOT EXISTS cache_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  layer TEXT NOT NULL,
  event TEXT NOT NULL,
  key_sample TEXT,
  timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS cache_metrics_layer_ts_idx
  ON cache_metrics (layer, timestamp DESC);
