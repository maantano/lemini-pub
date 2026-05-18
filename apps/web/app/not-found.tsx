import Link from "next/link";

export default function NotFound() {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "100vh",
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        color: "#1a1a2e",
        padding: "2rem",
        textAlign: "center",
      }}
    >
      <h1
        style={{
          fontSize: "6rem",
          fontWeight: 700,
          margin: 0,
          lineHeight: 1,
          color: "#d1d5db",
        }}
      >
        404
      </h1>
      <p
        style={{
          fontSize: "1.25rem",
          margin: "1rem 0 2rem",
          color: "#6b7280",
        }}
      >
        요청하신 페이지를 찾을 수 없습니다.
      </p>
      <Link
        href="/"
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "0.5rem",
          padding: "0.75rem 1.5rem",
          backgroundColor: "#1a1a2e",
          color: "#fff",
          borderRadius: "8px",
          textDecoration: "none",
          fontSize: "0.95rem",
          fontWeight: 500,
          transition: "opacity 0.15s",
        }}
      >
        홈으로 돌아가기
      </Link>
    </div>
  );
}
