# Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest   # more than 1,200 tests against a mock API, plus a guarded live smoke test
```

The live smoke test skips itself unless `ORCA_API_URL` and `ORCA_API_TOKEN` point at a running OrcaSlicer MCP build. Protocol notes live in [`docs/`](.).
