# Lemini — Korean Law RAG Chatbot

> 한국 법령·판례·자율규약을 자연어로 물어보는 RAG 기반 챗봇.
> Ouroboros 패턴(사실관계 수렴 후 분석) + 6체인 문서 검토 + 인용 검증 루프.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Portfolio](https://img.shields.io/badge/Portfolio-View-blue)](https://maantano.github.io/lemini-pub/)

📊 **[프로젝트 포트폴리오 보기](https://maantano.github.io/lemini-pub/)** ← 디자인·아키텍처 시각화

---

## What's inside

| Layer | Stack |
|---|---|
| Web | Next.js 15 (App Router) |
| API | FastAPI on Cloud Run |
| LLM | Google Gemini (via `google-genai`) |
| Search | SQLite + 자체 임베딩 매트릭스 (벡터 + lexical + exact) |
| Data | 법령(법제처 DRF API) + 판례(국가법령정보 공동활용) + 자율규약 |

## 핵심 설계

- **Ouroboros 모드 (대화형)** — 질문이 들어오면 모델이 사실관계 충분성부터 판단. 부족하면 객관식 후속질문, 충분하면 RAG → 구조화 분석.
- **전문 분석 모드 (문서 검토)** — 계약서·약관 입력 시 6단계 체인: 전체 스캔 → 외부 제도 프레임 매핑 → 축별 RAG → 조항 검토 → 병렬 분석(목적-수단·제도·리스크) → verdict.
- **인용 검증 루프** — LLM이 단 인용을 검색 hit과 대조해, 존재하지 않는 인용은 응답에서 제거.
- **3종 데이터 동일 풀** — 법령 + 판례 + 자율규약을 같은 벡터 공간에. 도메인 분기 코드 0건.

## Monorepo layout

```text
apps/
  api/       FastAPI entrypoint (apps/api/main.py)
  web/       Next.js web app
  worker/    Data ingest wrapper
packages/
  python/    parser, ingest, retrieval, chat orchestration
data/
  sample/    Sample markdown laws for quick start
  migrations/ SQL schema migrations
docs/        Portfolio (GitHub Pages)
scripts/     Ingest, collect, deploy helpers
```

---

# 🚀 Quick start (로컬에서 띄우기)

본 저장소에는 **시크릿·운영 데이터가 포함되지 않습니다.** 직접 발급받은 키를 `.env`에 채워야 동작합니다.

## 1. Prerequisites

- Node.js 20+, pnpm 9+
- Python 3.11+
- (필수) Google AI Studio API key
- (선택, 판례 검색 시) 국가법령정보 공동활용 OC

## 2. 키 발급 가이드

**최소 동작에 필요한 키는 4개**입니다:

| 변수 | 용도 | 위치 |
|---|---|---|
| `GEMINI_API_KEY` | LLM 호출 | BE `.env` |
| `ADMIN_API_KEY` | 관리자 엔드포인트 보호 | BE `.env` |
| `JWT_SECRET` | 토큰 서명 (현재 로그인 비활성화라도 코드 의존) | BE `.env` |
| `NEXT_PUBLIC_API_BASE_URL` | FE가 호출할 BE 주소 | FE `.env` (또는 같은 `.env`에) |

판례 검색까지 쓰려면 `LAW_API_KEY` 1개 추가. 로그인은 비활성화 상태라 카카오 키는 비워둬도 됩니다.

### 2-1. Google Gemini API Key (필수)

1. [Google AI Studio](https://aistudio.google.com/app/apikey) 접속 (Google 계정 필요)
2. "Create API key" 클릭
3. 생성된 키 복사 (`AIza...` 39자)
4. **무료 한도**: 분당 15회, 일별 1,500회 (`gemini-2.5-flash-lite` 기준). 데모용으로 충분.
5. `.env`의 `GEMINI_API_KEY=`에 값 채우기

### 2-2. 국가법령정보 공동활용 OC (선택 — 판례 검색 사용 시)

1. [국가법령정보 공동활용](https://open.law.go.kr) 접속, 회원가입
2. "OPEN API 활용신청" → 사용 신청 작성
3. **중요**: 호출할 서버의 **IP 또는 도메인을 등록**해야 합니다. 로컬 개발이면 본인 공인 IP, Cloud Run이면 고정 IP 등록 필요.
4. 발급받은 OC 값(보통 본인 ID와 동일)을 `.env`의 `LAW_API_KEY=`에 채우기
5. OC 미설정 시 판례 검색만 비활성화되고 법령 검색은 정상 동작

### 2-3. ADMIN_API_KEY / JWT_SECRET (필수)

랜덤 32바이트로 직접 생성:

```bash
openssl rand -hex 32
```

생성된 64자 hex를 `.env`의 `ADMIN_API_KEY`, `JWT_SECRET`에 각각 넣기 (두 값은 달라야 함).

## 3. Clone & install

```bash
git clone https://github.com/maantano/lemini-pub.git
cd lemini-pub

# .env 만들기
cp .env.example .env
# 위 2번 가이드에 따라 .env의 <your-...> 부분을 본인 값으로 채우기

# JS 의존성
pnpm install

# Python 의존성
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 4. Build local artifacts (sample data)

```bash
pnpm run ingest:sample
```

`data/sample/`의 노동법·산업안전보건법 마크다운으로 `data/artifacts/laws.sqlite` 와 임베딩 파일 생성.

## 5. Run

```bash
# Terminal 1 — API
pnpm run api:dev          # → http://localhost:8000

# Terminal 2 — Web
pnpm --filter @kr-law-rag/web dev   # → http://localhost:3000
```

`http://localhost:3000` 접속해서 "근로기준법 위반시 어떻게 해요?" 같은 질문 던져보세요.

---

# 📦 데이터 수집 (선택)

## 법령·판례 (법제처 DRF API)

```bash
# 법령 동기화 (전체 한국 법령)
python -m worker.sync.run --type laws

# 판례 동기화 (등록 IP에서만 호출 가능)
python -m worker.sync.run --type precedents
```

## 자율규약·가이드라인

자세한 방법은 [`data/voluntary-raw/README.md`](data/voluntary-raw/README.md) 참고.

```bash
# 공정거래위원회 표준약관 자동 수집
python scripts/collect_ftc_standard_terms.py

# 협회 자율규약 Playwright 수집
python scripts/voluntary_playwright_collector.py
```

---

# 🌐 GCP 배포 (선택)

본 저장소는 Cloud Run 배포 스크립트(`scripts/`, `cloudbuild.*.yaml`)와 GitHub Actions 워크플로(`.github/workflows/`)를 포함합니다.

본인의 GCP 프로젝트·버킷·서비스 계정을 **GitHub Repository Variables / Secrets**에 등록하면 자동 배포됩니다.

기본 환경:
- 백엔드: Cloud Run (`asia-northeast3` 권장)
- 프론트: Vercel 또는 Cloudflare Pages
- 데이터: GCS 버킷 (SQLite + 임베딩)
- 동기화: Cloud Run Job + Cloud Scheduler

> 비용은 본인 GCP 결제 페이지에서 직접 확인하세요. Cloud Run `min-instances=0` 권장.

---

# 🔒 Privacy & Cost guardrails

- `ENABLE_SERVER_CHAT_HISTORY=false` 기본값 — 서버 DB에 대화 저장 안 함
- **현재 로그인 UI 비활성화** — 회원가입·로그인 없이 데모 가능. 카카오 로그인 코드는 남아있으나 UI에서 차단됨
- Stateless 요청 — 클라이언트가 history를 매 요청에 동봉
- IP는 rate limit용 in-memory 버킷에만 사용
- 원본 markdown은 artifact DB에 복제되지 않음 (FTS index에만)

---

# 📊 포트폴리오

시스템 아키텍처와 디자인을 한 페이지로 정리한 시각 자료:

**👉 [https://maantano.github.io/lemini-pub/](https://maantano.github.io/lemini-pub/)**

---

# License

[MIT](./LICENSE) — 자유롭게 fork·수정·재배포 가능. 저작권 표시만 유지.

# Disclaimer

본 프로젝트는 **법률 자문 도구가 아닙니다.** 출력은 일반적인 법령·판례 정보 검색·분석 결과이며, 중요한 사안은 반드시 원문 법령과 변호사·노무사 등 전문가 검토가 필요합니다.

수집한 법령·판례·자율규약 원본의 사용 책임은 수집자(사용자) 본인에게 있습니다.
