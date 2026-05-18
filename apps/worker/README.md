# Worker Entrypoint

This directory is the app-level worker surface for deployment or task wiring.

Use the shared Python module for the actual ingest command:

```bash
.venv/bin/python -m worker.ingest.run --input ./data/sample --mode minimal --apply-schema
```
