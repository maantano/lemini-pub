/** localStorage에 저장되는 인증 정보 키 */
const AUTH_KEY = "kr-law-rag/auth";

/** 카카오 로그인 후 저장되는 사용자 인증 정보 */
export type AuthUser = {
  token: string;
  user_id: string;
  nickname: string;
  profile_image: string | null;
};

/** localStorage에서 인증 정보를 읽는다. 로그인 안 됐으면 null 반환. */
export function getAuth(): AuthUser | null {
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem(AUTH_KEY);
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
}

/** 카카오 로그인 성공 후 인증 정보를 localStorage에 저장한다. */
export function setAuth(user: AuthUser): void {
  localStorage.setItem(AUTH_KEY, JSON.stringify(user));
}

/** 로그아웃 — localStorage에서 인증 정보를 삭제한다. */
export function clearAuth(): void {
  localStorage.removeItem(AUTH_KEY);
}

/** API 요청에 사용할 Authorization Bearer 헤더를 반환한다. 비로그인이면 빈 객체. */
export function getAuthHeader(): Record<string, string> {
  const auth = getAuth();
  if (!auth) return {};
  return { Authorization: `Bearer ${auth.token}` };
}

/** 카카오 OAuth 로그인 URL을 생성한다. 환경변수에서 client_id와 redirect_uri를 읽는다. */
export function getKakaoLoginUrl(): string {
  const clientId = process.env.NEXT_PUBLIC_KAKAO_CLIENT_ID || "";
  const redirectUri = encodeURIComponent(
    process.env.NEXT_PUBLIC_KAKAO_REDIRECT_URI || "http://localhost:3000/auth/callback/"
  );
  return `https://kauth.kakao.com/oauth/authorize?client_id=${clientId}&redirect_uri=${redirectUri}&response_type=code`;
}
