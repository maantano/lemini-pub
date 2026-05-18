"use client";

import { FormEvent, useState } from "react";

import { apiRequest } from "../lib/api";
import type { AdminStats } from "../lib/types";

type IngestJob = {
  id: string;
  status: string;
  stats: Record<string, number | string>;
  error_log: string | null;
};

export function IngestForm() {
  const [adminKey, setAdminKey] = useState("");
  const [inputPath, setInputPath] = useState("./data/sample");
  const [mode, setMode] = useState<"minimal" | "full">("minimal");
  const [job, setJob] = useState<IngestJob | null>(null);
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleIngest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const response = await apiRequest<IngestJob>("/v1/admin/ingest/run", {
        method: "POST",
        headers: {
          "X-Admin-Api-Key": adminKey,
        },
        body: JSON.stringify({
          input_path: inputPath,
          mode,
          apply_schema: true,
          reindex: false,
        }),
      });
      setJob(response);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "ingest 실행에 실패했습니다.");
    } finally {
      setLoading(false);
    }
  }

  async function handleLoadStats() {
    setLoading(true);
    setError(null);
    try {
      const response = await apiRequest<AdminStats>("/v1/admin/stats", {
        headers: {
          "X-Admin-Api-Key": adminKey,
        },
      });
      setStats(response);
    } catch (statsError) {
      setError(statsError instanceof Error ? statsError.message : "통계 조회에 실패했습니다.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="panel stack-lg">
      <div className="section-heading">
        <p className="section-kicker">관리자 ingest</p>
        <h2>상시 worker 없이 필요할 때만 적재합니다.</h2>
      </div>
      <form className="stack-md" onSubmit={handleIngest}>
        <input
          value={adminKey}
          onChange={(event) => setAdminKey(event.target.value)}
          placeholder="X-Admin-Api-Key"
          type="password"
        />
        <input
          value={inputPath}
          onChange={(event) => setInputPath(event.target.value)}
          placeholder="./data/sample 또는 /path/to/zip"
        />
        <div className="button-row">
          <select value={mode} onChange={(event) => setMode(event.target.value as "minimal" | "full")}>
            <option value="minimal">minimal</option>
            <option value="full">full</option>
          </select>
          <button disabled={loading} type="submit">
            {loading ? "실행 중..." : "Ingest 실행"}
          </button>
          <button className="secondary-button" disabled={loading} onClick={handleLoadStats} type="button">
            저장량 보기
          </button>
        </div>
      </form>
      {error ? <p className="error-note">{error}</p> : null}
      {job ? (
        <div className="code-block">
          <strong>최근 ingest 결과</strong>
          <pre>{JSON.stringify(job, null, 2)}</pre>
        </div>
      ) : null}
      {stats ? (
        <div className="stat-grid">
          <article>
            <span>documents</span>
            <strong>{stats.documents}</strong>
          </article>
          <article>
            <span>chunks</span>
            <strong>{stats.chunks}</strong>
          </article>
          <article>
            <span>text bytes</span>
            <strong>{stats.estimated_text_bytes.toLocaleString()}</strong>
          </article>
          <article>
            <span>vector bytes</span>
            <strong>{stats.estimated_vector_bytes.toLocaleString()}</strong>
          </article>
        </div>
      ) : null}
    </section>
  );
}

