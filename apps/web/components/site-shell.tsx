"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { PropsWithChildren, useState, useEffect } from "react";
import { getAuth, type AuthUser } from "../lib/auth";

type SiteShellProps = PropsWithChildren<{
  eyebrow?: string;
  title?: string;
  description?: string;
}>;

const navItems = [
  { href: "/?new=1", label: "질문하기" },
  { href: "/tools", label: "도구" },
];

const authNavItems = [
  { href: "/ops/dashboard", label: "대시보드" },
];

/**
 * 전체 페이지 공통 레이아웃 래퍼.
 * Masthead(로고+네비+사용자메뉴) + Hero(타이틀) + 콘텐츠 영역을 제공한다.
 * 로그인 상태에 따라 네비게이션 항목이 달라진다.
 */
export function SiteShell({ eyebrow, title, description, children }: SiteShellProps) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const pathname = usePathname();

  useEffect(() => {
    setUser(getAuth());
  }, []);

  const allNav = user ? [...navItems, ...authNavItems] : navItems;

  const showHero = !!(title || eyebrow || description);

  return (
    <div className="page-shell">
      <header className="masthead">
        <div className="masthead-top">
          <Link className="brand" href="/">
            Lemini
          </Link>
          <nav className="top-nav">
            {allNav.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={pathname === item.href.split("?")[0] ? "nav-active" : ""}
              >
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="user-nav" />
        </div>
      </header>

      {showHero && (
        <section className="hero-section">
          <div className="hero-block">
            {eyebrow && <p className="eyebrow">{eyebrow}</p>}
            {title && <h1>{title}</h1>}
            {description && <p className="hero-copy">{description}</p>}
          </div>
        </section>
      )}

      <main className="page-content">{children}</main>
    </div>
  );
}
