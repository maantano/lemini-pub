"use client";

import { Suspense } from "react";

import { PrecedentSearch } from "../../components/precedent-search";
import { SiteShell } from "../../components/site-shell";

export default function PrecedentPage() {
  return (
    <Suspense fallback={<PrecedentPageShell />}>
      <SiteShell
        title="판례 탐색"
        description="판례, 헌재결정례, 행정심판례, 위원회 결정문까지 공식 무료 소스를 묶어 확인합니다."
      >
        <PrecedentSearch />
      </SiteShell>
    </Suspense>
  );
}

function PrecedentPageShell() {
  return (
    <SiteShell
      title="판례 탐색"
      description="판례, 헌재결정례, 행정심판례, 위원회 결정문까지 공식 무료 소스를 묶어 확인합니다."
    >
      <section className="panel">
        <p className="muted-note">판례 탐색 화면을 준비하고 있습니다.</p>
      </section>
    </SiteShell>
  );
}
