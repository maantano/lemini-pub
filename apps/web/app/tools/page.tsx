"use client";

import Link from "next/link";
import { SiteShell } from "../../components/site-shell";

const tools = [
  {
    href: "/law",
    title: "법령 검색",
    desc: "법령명이나 키워드로 조문을 직접 찾아봅니다.",
    icon: "📜",
  },
  {
    href: "/precedent",
    title: "판례 검색",
    desc: "판례, 헌재결정례, 행정심판례를 검색합니다.",
    icon: "⚖️",
  },
  {
    href: "/tools/severance",
    title: "퇴직금 계산기",
    desc: "입사일·퇴사일·평균임금으로 퇴직금을 계산합니다.",
    icon: "🧮",
  },
];

export default function ToolsPage() {
  return (
    <SiteShell title="도구" description="법령과 판례를 직접 검색하고 확인합니다.">
      <div className="tools-grid">
        {tools.map((tool) => (
          <Link key={tool.href} href={tool.href} className="tool-card">
            <span className="tool-icon">{tool.icon}</span>
            <h3 className="tool-title">{tool.title}</h3>
            <p className="tool-desc">{tool.desc}</p>
          </Link>
        ))}
      </div>
    </SiteShell>
  );
}
