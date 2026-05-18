import { NextRequest, NextResponse } from "next/server";

const API_BASE_URL =
  process.env.API_BASE_URL?.replace(/\/$/, "")
  || process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "")
  || "http://localhost:8000";

export const dynamic = "force-dynamic";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readUpstreamBody(response: Response): Promise<unknown> {
  const raw = await response.text();
  if (!raw) return null;

  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return { detail: raw };
  }
}

export async function GET(request: NextRequest) {
  const adminApiKey = request.headers.get("x-admin-api-key")?.trim();

  if (!adminApiKey) {
    return NextResponse.json({ detail: "Missing admin API key." }, { status: 400 });
  }

  let upstreamResponse: Response;
  try {
    upstreamResponse = await fetch(`${API_BASE_URL}/v1/admin/dashboard`, {
      method: "GET",
      headers: {
        "X-Admin-Api-Key": adminApiKey,
      },
      cache: "no-store",
      next: { revalidate: 0 },
    });
  } catch {
    return NextResponse.json(
      {
        detail: "Upstream admin dashboard request failed.",
        api_base_url: API_BASE_URL,
      },
      { status: 502 },
    );
  }

  const payload = await readUpstreamBody(upstreamResponse);

  if (!upstreamResponse.ok) {
    return NextResponse.json(
      isRecord(payload) ? payload : { detail: "Failed to load admin dashboard." },
      { status: upstreamResponse.status },
    );
  }

  return NextResponse.json(payload, { status: upstreamResponse.status });
}
