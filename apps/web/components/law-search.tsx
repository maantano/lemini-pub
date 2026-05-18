"use client";

import Link from "next/link";
import { useSearchParams, useRouter } from "next/navigation";
import { FormEvent, startTransition, useEffect, useState } from "react";

import { apiRequest } from "../lib/api";
import type { SearchResult } from "../lib/types";

type SearchResponse = {
  items: SearchResult[];
  corrected?: string;
};

/** 법령 검색 컴포넌트. GET /v1/laws/search로 키워드 검색 → 결과 목록을 카드로 표시. */
export function LawSearch() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const urlQuery = searchParams.get("q") || "";

  const [query, setQuery] = useState(urlQuery || "");
  const [items, setItems] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [corrected, setCorrected] = useState<string | null>(null);

  // Auto-search if URL has query param
  useEffect(() => {
    if (urlQuery) {
      setQuery(urlQuery);
      doSearch(urlQuery);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlQuery]);

  async function doSearch(q: string) {
    if (!q.trim() || loading) return;
    setLoading(true);
    setError(null);
    try {
      const response = await apiRequest<SearchResponse>(
        `/v1/laws/search?q=${encodeURIComponent(q.trim())}`,
      );
      startTransition(() => {
        setItems(response.items);
        setCorrected(response.corrected || null);
        setSearched(true);
      });
    } catch (submitError) {
      setError(
        submitError instanceof Error ? submitError.message : "검색에 실패했습니다.",
      );
      setSearched(true);
    } finally {
      setLoading(false);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!query.trim()) return;
    // Update URL with search query (preserves state on back navigation)
    router.replace(`/law?q=${encodeURIComponent(query.trim())}`, { scroll: false });
    doSearch(query);
  }

  return (
    <section className="panel search-workbench">
      <div className="search-hero">
        <p className="section-kicker">법령 검색</p>
        <h2>법령명으로 검색</h2>
      </div>
      <form className="search-bar search-bar-wide" onSubmit={handleSubmit}>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="법령명을 입력하세요..."
        />
        <button disabled={loading} type="submit">
          {loading ? (
            <span className="button-loading">
              <span className="spinner" />
              검색 중
            </span>
          ) : (
            "검색"
          )}
        </button>
      </form>

      {error ? (
        <div className="error-banner">
          <strong>연결 실패</strong>
          <p>{error}</p>
        </div>
      ) : null}

      {!error && searched && items.length === 0 ? (
        <div className="empty-results">
          <p>검색 결과가 없습니다.</p>
        </div>
      ) : null}

      {corrected && (
        <p className="search-corrected">
          <strong>&quot;{corrected}&quot;</strong>(으)로 검색한 결과도 포함했습니다.
        </p>
      )}

      {items.length > 0 ? (
        <div className="search-results search-grid">
          {items.map((item) => (
            <Link
              key={item.id}
              className="search-result-card"
              href={{ pathname: "/law", query: { lawId: item.law_id } }}
            >
              <div className="stack-sm">
                <p className="section-kicker">{item.law_type ?? "미분류"}</p>
                <strong>{item.title}</strong>
                <p className="muted-note">
                  {item.status ?? "status 없음"} · 시행일 {item.effective_date ?? "미상"}
                </p>
              </div>
              <span className="search-result-score">{`${(item.score * 100).toFixed(0)}%`}</span>
            </Link>
          ))}
        </div>
      ) : null}

      {!searched ? (
        <p className="muted-note">검색 버튼을 눌러 법령을 찾아보세요.</p>
      ) : null}
    </section>
  );
}
