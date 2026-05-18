import { getAuthHeader } from "./auth";

/** 백엔드 API 서버 주소. 환경변수 또는 기본값 localhost:8000 */
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") || "http://localhost:8000";

/** 세션 ID를 localStorage에서 가져오거나 새로 생성한다. 비로그인 사용량 추적에 사용. */
function getSessionId(): string {
  const key = "kr-law-rag/session-id";
  let id = window.localStorage.getItem(key);
  if (!id) {
    id = typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    window.localStorage.setItem(key, id);
  }
  return id;
}

/**
 * 범용 API 요청 함수.
 * 세션 ID + 인증 헤더를 자동 주입하고, 에러 시 메시지를 파싱하여 throw한다.
 */
const DEFAULT_TIMEOUT_MS = 240_000;

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        "X-Session-Id": getSessionId(),
        ...getAuthHeader(),
        ...(init?.headers ?? {}),
      },
      cache: "no-store",
      signal: init?.signal ?? controller.signal,
    });

    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }

    return (await response.json()) as T;
  } catch (err) {
    if ((err as Error).name === "AbortError") {
      throw new Error("요청 시간이 초과되었습니다. 다시 시도해 주세요.");
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * FormData API 요청. 파일 업로드 등에 사용.
 * Content-Type은 브라우저가 자동 설정 (multipart/form-data).
 */
export async function apiRequestFormData<T>(
  path: string,
  formData: FormData,
  init?: Omit<RequestInit, "body" | "method">,
): Promise<T> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      method: "POST",
      headers: {
        "X-Session-Id": getSessionId(),
        ...getAuthHeader(),
        ...(init?.headers ?? {}),
      },
      body: formData,
      cache: "no-store",
      signal: init?.signal ?? controller.signal,
      ...init,
    });

    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }

    return (await response.json()) as T;
  } catch (err) {
    if ((err as Error).name === "AbortError") {
      throw new Error("요청 시간이 초과되었습니다. 다시 시도해 주세요.");
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

export { API_BASE_URL };

/**
 * SSE 스트리밍 채팅 요청.
 * POST /v1/chat → Server-Sent Events로 토큰 단위 수신.
 * 이벤트 순서: meta → token(반복) → citations → done
 */
export async function streamChat(
  question: string,
  onMeta: (meta: { summary: string; grounded: boolean }) => void,
  onToken: (text: string) => void,
  onCitations: (citations: unknown[]) => void,
  onDone: (answer: unknown) => void,
  onError: (error: string) => void,
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/v1/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Session-Id": getSessionId(),
      ...getAuthHeader(),
    },
    body: JSON.stringify({
      question,
      stream: true,
      save: false,
      channel: "web",
    }),
    cache: "no-store",
  });

  if (!response.ok) {
    const msg = await readErrorMessage(response);
    onError(msg);
    return;
  }

  const reader = response.body?.getReader();
  if (!reader) {
    onError("스트리밍을 지원하지 않는 브라우저입니다.");
    return;
  }

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    let eventType = "";
    for (const line of lines) {
      if (line.startsWith("event: ")) {
        eventType = line.slice(7).trim();
      } else if (line.startsWith("data: ")) {
        const data = line.slice(6);
        try {
          const parsed = JSON.parse(data);
          if (eventType === "meta") onMeta(parsed);
          else if (eventType === "token") onToken(parsed.text);
          else if (eventType === "citations") onCitations(parsed);
          else if (eventType === "done") onDone(parsed);
        } catch { /* ignore parse errors */ }
      }
    }
  }
}

/** API 에러 응답에서 사람이 읽을 수 있는 메시지를 추출한다. */
async function readErrorMessage(response: Response): Promise<string> {
  const raw = await response.text();
  if (!raw) {
    return "API request failed";
  }
  try {
    const parsed = JSON.parse(raw) as { detail?: string };
    return parsed.detail || raw;
  } catch {
    return raw;
  }
}
