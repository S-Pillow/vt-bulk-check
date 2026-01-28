# vt-bulk-check

Monorepo for the VirusTotal Bulk Check tool.

## Structure

- `dns-tool/backend`: FastAPI backend hosting `/api/vt-bulk-check/*`
- `vt-bulk-check/frontend`: Standalone UI for `/tools/vt-bulk-check/`

## Security

- Do **not** commit secrets. Configure `VT_API_KEY` via environment variables.

