"use client";

import { useSearchParams, useRouter } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

import { SiteShell } from "../../components/site-shell";
import { LawSearch } from "../../components/law-search";
import { apiRequest } from "../../lib/api";
import type { LawDetailResponse } from "../../lib/types";

export default function LawPage() {
  return (
    <Suspense fallback={<LawPageShell />}>
      <LawPageContent />
    </Suspense>
  );
}

function LawPageContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const lawId = useMemo(() => searchParams.get("lawId")?.trim() ?? "", [searchParams]);
  const [detail, setDetail] = useState<LawDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeChapter, setActiveChapter] = useState<string | null>(null);
  const [visibleCount, setVisibleCount] = useState(10);

  useEffect(() => {
    if (!lawId) {
      setDetail(null);
      setError(null);
      return;
    }
    setError(null);
    apiRequest<LawDetailResponse>(`/v1/laws/${encodeURIComponent(lawId)}`)
      .then((response) => setDetail(response))
      .catch((fetchError) =>
        setError(fetchError instanceof Error ? fetchError.message : "법령 조회에 실패했습니다."),
      );
  }, [lawId]);

  // No lawId — show search
  if (!lawId) {
    return (
      <SiteShell
        title="법령 탐색"
        description="법령명이나 키워드로 조문 상세를 확인합니다."
      >
        <Suspense><LawSearch /></Suspense>
      </SiteShell>
    );
  }

  // lawId present — show detail
  return (
    <SiteShell
      title={detail?.document.title ?? "불러오는 중..."}
      description="조문 흐름을 한눈에 확인합니다."
    >
      <button type="button" className="back-link" onClick={() => router.back()}>
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path d="M10 12L6 8l4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
        돌아가기
      </button>
      {error && <p className="error-note">{error}</p>}
      {detail ? (
        <section className="stack-lg">
          <div className="law-detail-overview">
            <article className="panel law-overview-card">
              <p className="section-kicker">Overview</p>
              <div className="law-meta-grid">
                <div>
                  <span>법령 구분</span>
                  <strong>{detail.document.law_type ?? "법률"}</strong>
                </div>
                <div>
                  <span>시행일</span>
                  <strong>{detail.document.effective_date ?? "미상"}</strong>
                </div>
                <div>
                  <span>상태</span>
                  <strong>{detail.document.status ?? "상태 미상"}</strong>
                </div>
              </div>
            </article>
          </div>
          {/* Chapter tabs */}
          {(() => {
            const chapters = Array.from(new Set(detail.chunks.map(c => c.chapter_title).filter(Boolean)));
            const filtered = activeChapter
              ? detail.chunks.filter(c => c.chapter_title === activeChapter)
              : detail.chunks;
            const visible = filtered.slice(0, visibleCount);
            const hasMore = filtered.length > visibleCount;

            return (
              <>
                {chapters.length > 1 && (
                  <div className="law-toc">
                    <p className="section-kicker">{detail.chunks.length}개 조문 · {chapters.length}개 장</p>
                    <div className="law-toc-chips">
                      <button
                        type="button"
                        className={`law-toc-chip ${!activeChapter ? "law-toc-chip-active" : ""}`}
                        onClick={() => { setActiveChapter(null); setVisibleCount(10); }}
                      >
                        전체
                      </button>
                      {chapters.map(ch => (
                        <button
                          key={ch}
                          type="button"
                          className={`law-toc-chip ${activeChapter === ch ? "law-toc-chip-active" : ""}`}
                          onClick={() => { setActiveChapter(ch!); setVisibleCount(10); }}
                        >
                          {ch}
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                <div className="article-stream">
                  {visible.map((chunk) => (
                    <article key={chunk.id} className="article-card">
                      <div className="article-badge">
                        <span>{chunk.chapter_title ?? "본문"}</span>
                        <strong>{chunk.article_no ?? chunk.chunk_type}</strong>
                      </div>
                      <h2>{chunk.article_title ?? "본문"}</h2>
                      <p>{chunk.text}</p>
                    </article>
                  ))}
                </div>

                {hasMore && (
                  <button
                    className="show-more-btn"
                    type="button"
                    onClick={() => setVisibleCount(prev => prev + 10)}
                  >
                    다음 10개 더보기 ({visibleCount}/{filtered.length})
                  </button>
                )}
              </>
            );
          })()}
        </section>
      ) : (
        !error && (
          <section className="panel">
            <p className="muted-note">{`\`${lawId}\` 데이터를 불러오는 중입니다.`}</p>
          </section>
        )
      )}
    </SiteShell>
  );
}

function LawPageShell() {
  return (
    <SiteShell
      title="법령 탐색"
      description="법령명이나 키워드로 조문 상세를 확인합니다."
    >
      <section className="panel">
        <p className="muted-note">로딩 중...</p>
      </section>
    </SiteShell>
  );
}
