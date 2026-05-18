import type { Citation } from "../lib/types";

type CitationsListProps = {
  citations: Citation[];
};

/** 답변에 인용된 법률 조문 목록을 렌더링한다. 각 조문은 접기/펼치기로 원문 확인 가능. */
export function CitationsList({ citations }: CitationsListProps) {
  if (citations.length === 0) {
    return (
      <p className="muted-note">
        직접 인용할 조문은 아직 없습니다. 위 판단 포인트를 먼저 확인한 뒤 질문을 조금 더 나눠 물으면
        정확도가 올라갑니다.
      </p>
    );
  }

  return (
    <div className="citation-list">
      {citations.map((citation) => (
        <article key={citation.chunk_id} className="citation-card">
          <div className="citation-meta">
            <span>{citation.law_title}</span>
            <span>{citation.article_no ?? citation.chunk_type}</span>
          </div>
          <strong>{citation.article_title ?? "조문 근거"}</strong>
          <details className="citation-details">
            <summary className="citation-toggle">법률 원문 보기</summary>
            <p className="citation-quote">{citation.quote}</p>
          </details>
        </article>
      ))}
    </div>
  );
}
