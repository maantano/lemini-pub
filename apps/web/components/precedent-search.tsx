"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { FormEvent, startTransition, useEffect, useMemo, useState } from "react";

const CONSUMER_LABELS: Record<string, { label: string; defaultOpen: boolean }> = {
  "판시사항": { label: "법원이 다룬 쟁점", defaultOpen: true },
  "판결요지": { label: "판결 핵심 결론", defaultOpen: true },
  "참조조문": { label: "관련 법 조항", defaultOpen: false },
  "참조판례": { label: "관련 판례", defaultOpen: false },
  "판례내용": { label: "판결문 전문", defaultOpen: false },
  "결정요지": { label: "결정 핵심 결론", defaultOpen: true },
  "심판대상조문": { label: "심판 대상 법 조항", defaultOpen: false },
  "전문": { label: "결정문 전문", defaultOpen: false },
  "주문": { label: "최종 결정", defaultOpen: true },
  "청구취지": { label: "청구인이 원한 것", defaultOpen: true },
  "재결요지": { label: "재결 핵심 결론", defaultOpen: true },
  "이유": { label: "판단 이유", defaultOpen: false },
  "질의요지": { label: "질문 요지", defaultOpen: true },
  "회답": { label: "공식 답변", defaultOpen: true },
  "조치내용": { label: "조치 내용", defaultOpen: true },
  "조치이유": { label: "조치 이유", defaultOpen: false },
  "주요내용": { label: "주요 내용", defaultOpen: true },
};

import { apiRequest } from "../lib/api";
import type {
  PrecedentDetailResponse,
  PrecedentSearchResponse,
  PrecedentSource,
} from "../lib/types";

function buildSearchParams(
  query: string,
  sourceIds: string[],
  bodySearch: boolean,
  selectedCase?: { sourceId: string; caseId: string } | null,
) {
  const params = new URLSearchParams();
  if (query.trim()) params.set("q", query.trim());
  if (bodySearch) params.set("body", "1");
  sourceIds.forEach((sourceId) => params.append("source", sourceId));
  if (selectedCase?.sourceId) params.set("sourceCase", selectedCase.sourceId);
  if (selectedCase?.caseId) params.set("caseId", selectedCase.caseId);
  return params;
}

/**
 * 판례 검색 컴포넌트.
 * 초기화: GET /v1/precedents/sources → 소스 목록 + 기본 필터.
 * 검색: GET /v1/precedents/search → 소스별 결과 그룹 표시.
 * 상세: GET /v1/precedents/{sourceId}/{caseId} → 판례 전문 표시.
 */
export function PrecedentSearch() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const urlQuery = searchParams.get("q") ?? "";
  const urlCaseId = searchParams.get("caseId") ?? "";
  const urlCaseSource = searchParams.get("sourceCase") ?? "";
  const urlSourceIds = searchParams.getAll("source");
  const urlBodySearch = searchParams.get("body") === "1";

  const [availableSources, setAvailableSources] = useState<PrecedentSource[]>([]);
  const [query, setQuery] = useState(urlQuery);
  const [selectedSources, setSelectedSources] = useState<string[]>([]);
  const [bodySearch, setBodySearch] = useState(urlBodySearch);
  const [searchResponse, setSearchResponse] = useState<PrecedentSearchResponse | null>(null);
  const [courtFilter, setCourtFilter] = useState<string | null>(null);
  const [detail, setDetail] = useState<PrecedentDetailResponse | null>(null);
  const [loadingSources, setLoadingSources] = useState(true);
  const [loadingSearch, setLoadingSearch] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function initialize() {
      setLoadingSources(true);
      try {
        const sources = await apiRequest<PrecedentSource[]>("/v1/precedents/sources");
        if (cancelled) return;

        const defaults = sources.filter((source) => source.default_enabled).map((source) => source.source_id);
        const initialSources = urlSourceIds.length > 0 ? urlSourceIds : defaults;

        setAvailableSources(sources);
        setSelectedSources(initialSources);

        if (urlQuery.trim()) {
          await performSearch(urlQuery, initialSources, urlBodySearch, {
            sourceId: urlCaseSource,
            caseId: urlCaseId,
          });
        } else if (urlCaseId && urlCaseSource) {
          await loadDetail(urlCaseSource, urlCaseId);
        }
      } catch (error) {
        if (cancelled) return;
        setSearchError(error instanceof Error ? error.message : "판례 소스를 불러오지 못했습니다.");
      } finally {
        if (!cancelled) {
          setLoadingSources(false);
        }
      }
    }

    initialize();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const groupedResults = useMemo(() => {
    const items = searchResponse?.results ?? [];
    return availableSources
      .filter((source) => selectedSources.includes(source.source_id))
      .map((source) => ({
        source,
        items: items.filter((item) => item.source_id === source.source_id),
        error: searchResponse?.errors?.[source.source_id] ?? null,
        count: searchResponse?.source_counts?.[source.source_id] ?? 0,
      }));
  }, [availableSources, searchResponse, selectedSources]);

  /** 판례 검색 실행 — API 호출 → URL 업데이트 → 첫 번째 결과 자동 상세 조회 */
  async function performSearch(
    nextQuery: string,
    sourceIds: string[],
    nextBodySearch: boolean,
    selectedCase?: { sourceId: string; caseId: string } | null,
  ) {
    if (!nextQuery.trim() || sourceIds.length === 0) return;

    setLoadingSearch(true);
    setSearchError(null);
    try {
      const params = new URLSearchParams();
      params.set("q", nextQuery.trim());
      params.set("display", "4");
      if (nextBodySearch) params.set("bodySearch", "true");
      sourceIds.forEach((sourceId) => params.append("source", sourceId));

      const response = await apiRequest<PrecedentSearchResponse>(
        `/v1/precedents/search?${params.toString()}`,
      );

      startTransition(() => {
        setSearchResponse(response);
      });

      const firstResult = response.results[0];
      const nextSelectedCase = selectedCase?.caseId
        ? selectedCase
        : firstResult
          ? { sourceId: firstResult.source_id, caseId: firstResult.case_id }
          : null;

      const nextUrl = buildSearchParams(nextQuery, sourceIds, nextBodySearch, nextSelectedCase);
      router.replace(nextUrl.toString() ? `/precedent?${nextUrl.toString()}` : "/precedent", {
        scroll: false,
      });

      if (nextSelectedCase?.sourceId && nextSelectedCase.caseId) {
        await loadDetail(nextSelectedCase.sourceId, nextSelectedCase.caseId);
      } else {
        setDetail(null);
        setDetailError(null);
      }
    } catch (error) {
      setSearchError(error instanceof Error ? error.message : "판례 검색에 실패했습니다.");
    } finally {
      setLoadingSearch(false);
    }
  }

  /** 판례 상세 조회 — GET /v1/precedents/{sourceId}/{caseId} */
  async function loadDetail(sourceId: string, caseId: string) {
    if (!sourceId || !caseId) return;

    setLoadingDetail(true);
    setDetailError(null);
    try {
      const response = await apiRequest<PrecedentDetailResponse>(
        `/v1/precedents/${encodeURIComponent(sourceId)}/${encodeURIComponent(caseId)}`,
      );
      setDetail(response);
    } catch (error) {
      setDetailError(error instanceof Error ? error.message : "상세 판례를 불러오지 못했습니다.");
    } finally {
      setLoadingDetail(false);
    }
  }

  function toggleSource(sourceId: string) {
    setSelectedSources((prev) => {
      if (prev.includes(sourceId)) {
        return prev.filter((item) => item !== sourceId);
      }
      return [...prev, sourceId];
    });
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    performSearch(query, selectedSources, bodySearch);
  }

  function handleSelectResult(sourceId: string, caseId: string) {
    const params = buildSearchParams(query, selectedSources, bodySearch, { sourceId, caseId });
    router.replace(`/precedent?${params.toString()}`, { scroll: false });
    loadDetail(sourceId, caseId);
  }

  return (
    <section className="precedent-page-stack">
      <section className="panel precedent-search-panel">
        <div className="precedent-search-header">
          <div className="stack-sm">
            <p className="section-kicker">공식 무료 데이터</p>
            <h2 className="precedent-search-title">판례, 재결례, 결정문을 한 번에 찾기</h2>
            <p className="muted-note">
              국가법령정보 공동활용 소스를 묶어 검색합니다. 현재 서버 IP/도메인이 등록되지 않은
              환경에서는 일부 소스가 공식 정책상 차단될 수 있습니다.
            </p>
          </div>
          {searchResponse ? (
            <div className="precedent-summary-badges">
              <span className="status-pill status-neutral">{`총 ${searchResponse.total_count}건`}</span>
              {searchResponse.win_rate !== null ? (
                <span className="status-pill status-affirmed">
                  {`승소·인용 비율 ${(searchResponse.win_rate * 100).toFixed(0)}%`}
                </span>
              ) : null}
            </div>
          ) : null}
        </div>

        <form className="precedent-form" onSubmit={handleSubmit}>
          <div className="precedent-query-row">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="판례 검색어를 입력하세요..."
            />
            <button disabled={loadingSearch || selectedSources.length === 0} type="submit">
              {loadingSearch ? "검색 중" : "판례 검색"}
            </button>
          </div>

          <label className="precedent-body-toggle">
            <input
              type="checkbox"
              checked={bodySearch}
              onChange={(event) => setBodySearch(event.target.checked)}
            />
            <span>본문까지 함께 검색</span>
          </label>

          {loadingSources ? (
            <p className="muted-note">소스 목록을 불러오는 중입니다.</p>
          ) : (
            <div className="precedent-filter-groups">
              {availableSources.map((source) => (
                <label key={source.source_id} className="precedent-filter-chip">
                  <input
                    type="checkbox"
                    checked={selectedSources.includes(source.source_id)}
                    onChange={() => toggleSource(source.source_id)}
                  />
                  <span>
                    <strong>{source.label}</strong>
                    <small>{source.provider}</small>
                  </span>
                </label>
              ))}
            </div>
          )}
        </form>

        {searchError ? (
          <div className="error-banner">
            <strong>판례 검색 실패</strong>
            <p>{searchError}</p>
          </div>
        ) : null}
      </section>

      {/* Detail view — shown when a case is selected */}
      {detail && !loadingDetail && (
        <section className="panel precedent-detail-card">
          <button type="button" className="back-link" onClick={() => { setDetail(null); setDetailError(null); }}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M10 12L6 8l4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
            검색 결과로 돌아가기
          </button>

          <div className="precedent-detail-header">
            <div className="stack-sm">
              <div className="precedent-detail-badges">
                <span className="status-pill status-affirmed">{detail.result.source_label}</span>
                {detail.result.judgment_type && <span className="status-pill status-neutral">{detail.result.judgment_type}</span>}
              </div>
              <h2 className="precedent-detail-title">{detail.result.case_name}</h2>
              <p className="muted-note">
                {detail.result.court_name}
                {detail.result.case_number ? ` · ${detail.result.case_number}` : ""}
                {detail.result.judgment_date ? ` · ${detail.result.judgment_date}` : ""}
              </p>
            </div>
            {detail.result.external_url && (
              <a href={detail.result.external_url} className="secondary-button precedent-link-button" rel="noopener noreferrer" target="_blank">
                공식 원문
              </a>
            )}
          </div>

          {/* Section navigation */}
          {detail.sections.length > 1 && (
            <nav className="precedent-section-nav">
              {detail.sections.map((sec) => (
                <a key={sec.label} href={`#sec-${sec.label}`} className="precedent-section-nav-item">{(CONSUMER_LABELS[sec.label] ?? { label: sec.label }).label}</a>
              ))}
            </nav>
          )}

          {detail.sections.length > 0 ? (
            <div className="precedent-section-list">
              {detail.sections.map((section) => {
                const config = CONSUMER_LABELS[section.label] ?? { label: section.label, defaultOpen: true };
                return (
                  <details key={section.label} id={`sec-${section.label}`} className="precedent-section-item" open={config.defaultOpen || undefined}>
                    <summary className="precedent-section-summary">
                      <h3>{config.label}</h3>
                      {config.label !== section.label && <span className="precedent-original-term">{section.label}</span>}
                    </summary>
                    <div className="precedent-section-content" dangerouslySetInnerHTML={{ __html: section.content }} />
                  </details>
                );
              })}
            </div>
          ) : (
            <p className="muted-note">표시할 상세 요약이 없습니다.</p>
          )}

          <p className="precedent-disclaimer">{detail.note}</p>
        </section>
      )}

      {loadingDetail && (
        <section className="panel">
          <p className="muted-note">상세 판례를 불러오는 중...</p>
        </section>
      )}

      {detailError && !detail && (
        <div className="error-banner">
          <strong>상세 조회 실패</strong>
          <p>{detailError}</p>
        </div>
      )}

      {/* Search results — hidden when detail is shown */}
      {!detail && !loadingDetail && (
        <div className="precedent-results-single">
          {!searchResponse ? (
            <section className="panel">
              <p className="muted-note">검색 소스를 선택하면 결과가 여기에 표시됩니다.</p>
            </section>
          ) : (
            <>
              {/* Court filter chips */}
              {(() => {
                const allItems = searchResponse?.results ?? [];
                const courts = Array.from(new Set(allItems.map((i) => i.court_name).filter(Boolean)));
                if (courts.length <= 1) return null;
                return (
                  <div className="court-filter-row">
                    <button type="button" className={`law-toc-chip ${!courtFilter ? "law-toc-chip-active" : ""}`} onClick={() => setCourtFilter(null)}>전체</button>
                    {courts.map((court) => (
                      <button key={court} type="button" className={`law-toc-chip ${courtFilter === court ? "law-toc-chip-active" : ""}`} onClick={() => setCourtFilter(court)}>
                        {court} ({allItems.filter((i) => i.court_name === court).length})
                      </button>
                    ))}
                  </div>
                );
              })()}
              {groupedResults.map(({ source, items: rawItems, error, count }) => {
                const items = courtFilter ? rawItems.filter((i) => i.court_name === courtFilter) : rawItems;
                return (
              <section key={source.source_id} className="panel precedent-group-card">
                <div className="precedent-group-header">
                  <div className="stack-sm">
                    <p className="section-kicker">{source.provider}</p>
                    <h3>{source.label}</h3>
                    <p className="muted-note">{source.description}</p>
                  </div>
                  <span className="status-pill status-neutral">{`${courtFilter ? items.length : count}건`}</span>
                </div>

                {error ? (
                  <div className="error-banner">
                    <strong>{source.label} 호출 실패</strong>
                    <p>{error}</p>
                  </div>
                ) : items.length === 0 ? (
                  <p className="muted-note">{courtFilter ? `${courtFilter} 판례가 없습니다.` : "검색 결과가 없습니다."}</p>
                ) : (
                  <div className="precedent-search-results">
                    {items.map((item) => (
                      <button
                        key={`${item.source_id}-${item.case_id}`}
                        type="button"
                        className="precedent-search-card"
                        onClick={() => handleSelectResult(item.source_id, item.case_id)}
                      >
                        <div className="precedent-card-header">
                          <strong>{item.case_name}</strong>
                          {item.judgment_type && <span className="precedent-meta">{item.judgment_type}</span>}
                        </div>
                        <span className="precedent-court">
                          {item.court_name}
                          {item.judgment_date ? ` · ${item.judgment_date}` : ""}
                        </span>
                        {item.case_number && <span className="precedent-case-number">{item.case_number}</span>}
                        {item.summary && <p className="precedent-summary">{item.summary}</p>}
                      </button>
                    ))}
                  </div>
                )}
              </section>
                );
              })}
            </>
          )}
        </div>
      )}
    </section>
  );
}
