# Third-Party Code Notices — sync 모듈

이 디렉토리의 코드 대부분은 다음 오픈소스 프로젝트에서 가져와 우리 환경에 맞춰 수정한 것이다.

## legalize-pipeline

- **원본 저장소:** https://github.com/legalize-kr/legalize-pipeline
- **저작권자:** Copyright (c) 2026 Junghwan Park, Jihyeon Kim, and other contributors
- **라이선스:** MIT License **OR** Apache License 2.0 (듀얼 라이선스 — 둘 중 택일 적용 가능)
- **라이선스 전문:** 이 디렉토리의 [`LICENSES/LICENSE-MIT`](./LICENSES/LICENSE-MIT), [`LICENSES/LICENSE-APACHE`](./LICENSES/LICENSE-APACHE) 참조

### 이식 범위

| 원본 경로 | 우리 경로 | 변경 내역 |
|---|---|---|
| `core/config.py` | `core/config.py` | `BOT_AUTHOR`/`PROJECT_ROOT` 제거, `WORKSPACE_ROOT` 기본값을 우리 레포 경로로, `GCS_DATA_BUCKET`/`GCS_CACHE_BUCKET` 추가 |
| `core/http.py` | `core/http.py` | 그대로 |
| `core/throttle.py` | `core/throttle.py` | 그대로 |
| `core/atomic_io.py` | `core/atomic_io.py` | 그대로 |
| `core/counter.py` | `core/counter.py` | 그대로 |
| `laws/api_client.py` | `laws/api_client.py` | import 경로 조정. **응답 파싱을 XML → JSON 으로 전환** (우리 OC 키 특성상 JSON 만 안정 동작). lsHistory 는 JSON 미지원이라 HTML 정규식 파싱 유지. 항·호·목은 API 가 None/dict/list 세 형태를 오가므로 `_as_list` 로 정규화 |
| `laws/cache.py` | `laws/cache.py` | import 경로 조정 |
| `laws/checkpoint.py` | `laws/checkpoint.py` | `_write`에 `parents=True` 추가 |
| `laws/config.py` | `laws/config.py` | `BOT_AUTHOR` re-export 제거 |
| `laws/converter.py` | `laws/converter.py` | 그대로 |
| `laws/empty_body_allowlist.py` + `data/known_empty_body.yaml` | 동일 | 그대로 |
| `laws/failures.py` | `laws/failures.py` | `_write`에 `parents=True` 추가 |
| `laws/reverse_index.py` | `laws/reverse_index.py` | 그대로 |
| `laws/update.py` | `laws/update.py` | `git_engine` · `import_laws` · `generate_metadata` 의존 제거. 파일시스템/GCS 쓰기 모델로 전환 |
| `laws/validate.py` | `laws/validate.py` | `metadata.json` 교차검증 제거 (우리 파이프라인은 metadata.json 미생성) |

### 아직 이식하지 않은 원본 모듈

아래 모듈은 현재 우리 파이프라인 설계와 맞지 않아 이식 대상에서 제외했다. 향후 필요 시 동일한 고지 규칙으로 추가한다.

- `laws/git_engine.py`, `laws/import_laws.py`, `laws/generate_metadata.py` — git 저장소 commit 기반 저장 방식. 우리는 GCS 직접 쓰기.
- `laws/fetch_cache.py`, `laws/rebuild.py`, `laws/migrate_ministry_paths.py` — 전수 초기 수집·마이그레이션 도구. 시드 유지 방침(docs/legalize-pipeline-port.md §9)이라 불필요.
- `precedents/*` — 판례 파이프라인. M4에서 이식 예정.
- `images/*` — 법령 별표·별지 이미지 추출. 현재 우리 스코프 외.

### 라이선스 의무 준수

MIT / Apache 2.0 듀얼 라이선스의 요구사항:

1. **저작권 고지 유지** ✅ — 원본 저작권자 표기는 본 NOTICES.md에 명시
2. **라이선스 전문 동봉** ✅ — [`LICENSES/LICENSE-MIT`](./LICENSES/LICENSE-MIT), [`LICENSES/LICENSE-APACHE`](./LICENSES/LICENSE-APACHE) 포함
3. **수정 사실 표시 (Apache §4.b)** ✅ — 변경 내역은 위 표에 명시, 각 이식 파일 docstring에 "Adapted from legalize-pipeline" 고지

상업 배포 시에는 프로덕트 푸터(또는 `/credits` 페이지)에도 본 프로젝트와의 관계를 표기할 것. 예시:

> 법령·판례 수집 파이프라인은 [legalize-pipeline](https://github.com/legalize-kr/legalize-pipeline) (MIT / Apache-2.0) 기반으로 이식·수정한 코드를 포함합니다.

## 데이터 출처 고지 (런타임)

수집된 법령 데이터의 궁극 출처는 **국가법령정보센터 (https://www.law.go.kr)** 이며, DRF OpenAPI(OC 키 인증)를 통해 직접 수집한다. 응답 UI에 법령을 인용할 때는 다음 형태로 출처를 명시한다:

> 출처: 국가법령정보센터 (law.go.kr) | DB 기준일: YYYY-MM-DD

시드 데이터(2026-04-03 스냅샷, 5,568건)는 공공누리 조건에 따라 제공된 법령 데이터를 사용했으며, 이후 증분 수집은 우리가 직접 DRF API로 수집한다.
