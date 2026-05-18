"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { setAuth } from "../../../lib/auth";
import { apiRequest } from "../../../lib/api";
import type { AuthUser } from "../../../lib/auth";
import { SiteShell } from "../../../components/site-shell";

type KakaoAuthResponse = {
  token: string;
  user_id: string;
  nickname: string;
  profile_image: string | null;
};

function AuthCallbackContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("카카오 인증 처리 중...");

  useEffect(() => {
    const code = searchParams.get("code");
    if (!code) {
      setError("카카오 인증 코드가 없습니다. 다시 로그인해 주세요.");
      return;
    }

    let cancelled = false;
    setStatus("서버에 인증 요청 중...");

    async function authenticate(authCode: string) {
      try {
        const data = await apiRequest<KakaoAuthResponse>("/v1/auth/kakao", {
          method: "POST",
          body: JSON.stringify({ code: authCode }),
        });

        if (cancelled) return;

        setStatus("로그인 완료! 이동 중...");

        const user: AuthUser = {
          token: data.token,
          user_id: data.user_id,
          nickname: data.nickname,
          profile_image: data.profile_image,
        };
        setAuth(user);
        try {
          const keys = Object.keys(localStorage);
          for (const key of keys) {
            if (key.startsWith("kr-law-rag/last-session/")) {
              localStorage.removeItem(key);
            }
          }
        } catch {}
        router.replace("/?new=1");
      } catch (err) {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : "로그인에 실패했습니다.";
        setError(msg);
      }
    }

    authenticate(code);

    return () => {
      cancelled = true;
    };
  }, [searchParams, router]);

  if (error) {
    return (
      <SiteShell>
        <div className="auth-callback-card">
          <div className="auth-callback-icon auth-callback-error-icon">!</div>
          <h2>로그인 실패</h2>
          <p className="auth-callback-msg">{error}</p>
          <a href="/" className="auth-callback-btn">홈으로 돌아가기</a>
        </div>
      </SiteShell>
    );
  }

  return (
    <SiteShell>
      <div className="auth-callback-card">
        <div className="spinner" style={{ width: 32, height: 32, margin: "0 auto", borderColor: "var(--border)", borderTopColor: "var(--accent)" }} />
        <h2>{status}</h2>
        <p className="auth-callback-msg">잠시만 기다려 주세요.</p>
      </div>
    </SiteShell>
  );
}

export default function AuthCallbackPage() {
  return (
    <Suspense>
      <AuthCallbackContent />
    </Suspense>
  );
}
