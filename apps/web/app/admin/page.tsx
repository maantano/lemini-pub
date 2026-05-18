import Link from "next/link";

import { IngestForm } from "../../components/ingest-form";
import { SiteShell } from "../../components/site-shell";

export default function AdminPage() {
  return (
    <SiteShell
      eyebrow="Operations"
      title="데이터 적재와 저장량 관리"
      description="필요할 때만 ingest를 실행하고 저장량을 확인합니다."
    >
      <div className="button-row">
        <Link href="/ops/dashboard" className="secondary-button">
          운영 대시보드 열기
        </Link>
      </div>
      <IngestForm />
    </SiteShell>
  );
}
