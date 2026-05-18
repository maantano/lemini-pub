from __future__ import annotations

import argparse
import json

from law_rag_core.ingest import IngestService
from law_rag_core.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build SQLite and embedding artifacts from Korean law markdown."
    )
    parser.add_argument("--input", required=True, help="Path to a .zip, .md, or extracted directory.")
    parser.add_argument("--mode", choices=["minimal", "full"], default="minimal")
    parser.add_argument("--apply-schema", action="store_true")
    parser.add_argument("--reindex", action="store_true")
    return parser


def main() -> None:
    configure_logging()
    args = build_parser().parse_args()
    service = IngestService()
    job = service.ingest_path(
        args.input,
        mode=args.mode,
        apply_schema=args.apply_schema,
        reindex=args.reindex,
    )
    print(json.dumps(job.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
