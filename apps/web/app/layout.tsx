import type { Metadata } from "next";
import Script from "next/script";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "lemini — 법률 질문에서 행동까지",
  description: "상황을 분석하고, 유리한 점을 정리하고, 다음 행동을 알려드립니다.",
};

const filePreviewNoticeScript = `
(() => {
  if (window.location.protocol !== "file:") {
    return;
  }

  const mountNotice = () => {
    if (!document.body || document.getElementById("file-preview-warning")) {
      return;
    }

    const banner = document.createElement("div");
    banner.id = "file-preview-warning";
    banner.setAttribute(
      "style",
      "position:sticky;top:0;z-index:9999;padding:12px 16px;background:#fff1cc;color:#4a3212;font:600 14px/1.5 sans-serif;border-bottom:1px solid #d9b36a;text-align:center;"
    );
    banner.textContent =
      "export된 HTML을 브라우저에서 직접 열면 스타일과 스크립트 경로가 깨집니다. apps/web/out 을 서버로 띄워서 확인하세요. 예: pnpm run preview:web";
    document.body.prepend(banner);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountNotice, { once: true });
    return;
  }

  mountNotice();
})();
`;

const plausibleDomain = process.env.NEXT_PUBLIC_PLAUSIBLE_DOMAIN;

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="ko" suppressHydrationWarning>
      <body>
        <Script id="file-preview-notice" strategy="beforeInteractive">
          {filePreviewNoticeScript}
        </Script>
        {plausibleDomain && (
          <Script
            defer
            data-domain={plausibleDomain}
            src="https://plausible.io/js/script.tagged-events.js"
            strategy="afterInteractive"
          />
        )}
        {children}
      </body>
    </html>
  );
}
