import type { NextConfig } from "next";

function normalizeBasePath(rawBasePath?: string): string | undefined {
  if (!rawBasePath) {
    return undefined;
  }

  const trimmedBasePath = rawBasePath.trim();

  if (!trimmedBasePath || trimmedBasePath === "/") {
    return undefined;
  }

  const withLeadingSlash = trimmedBasePath.startsWith("/")
    ? trimmedBasePath
    : `/${trimmedBasePath}`;

  return withLeadingSlash.endsWith("/") ? withLeadingSlash.slice(0, -1) : withLeadingSlash;
}

const basePath = normalizeBasePath(process.env.WEB_BASE_PATH);
const isDev = process.env.NODE_ENV === "development";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // SSR mode — no static export. Required for SEO (crawlable pages, dynamic meta tags).
  // Previously: ...(isDev ? {} : { output: "export" })
  trailingSlash: true,
  images: {
    unoptimized: true,
  },
  ...(basePath ? { basePath } : {}),
};

export default nextConfig;
