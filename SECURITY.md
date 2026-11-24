# Security Policy - Gitlytics (Core Library & CLI)

This repository is part of the **Gitlytics Ecosystem**. Security issues in any Gitlytics repository follow the same private disclosure process.

## Supported Versions

| Version | Supported |
| :--- | :--- |
| `0.9.x` (latest) | Yes |
| `< 0.9.0` | No |

## Security Controls in `gitlytics`

- **Loopback Default & Non-Loopback Gate**: `gitlytics dashboard` binds to `127.0.0.1` by default. Any non-loopback `--host` (`0.0.0.0`, `::`, or a LAN IP) requires `--public-bind`, which automatically drops pre-configured server tokens (`--token` and `GITLYTICS_TOKEN`) so each caller must supply their own GitHub token.
- **No Ambient `GITHUB_TOKEN` Promotion**: `gitlytics dashboard` never silently promotes an ambient `GITHUB_TOKEN` from your shell into the HTTP server; pass `--token` or set `GITLYTICS_TOKEN` explicitly on loopback if desired.
- **Local Stdio MCP Isolation (`gitlytics mcp`)**: Tool schemas never accept raw GitHub tokens as arguments (preventing secrets in AI chat logs); live GitHub tools read `GITLYTICS_TOKEN` / `GITHUB_TOKEN` strictly from process environment variables. CSV vault queries enforce strict path containment under `--data-dir` and reject path traversal (`..`).
- **Trusted `Host` & Same-Origin Checks**: Browser requests are validated against trusted loopback hosts (`localhost`, `127.0.0.1`, `[::1]`, or `GITLYTICS_ALLOWED_HOSTS` for reverse proxies) and require `application/json` on JSON `POST` routes.
- **Upload Access & Size Caps**: `POST /api/upload-csv` enforces `GITLYTICS_UPLOAD_KEY` (via `X-Upload-Key`) whenever set, caps request bodies at 5 MiB before multipart parsing, and limits parsed CSVs to 50,000 rows and 2,000 unique repositories.
- **Principal & Privacy Isolation**: `POST /api/traffic` scopes `GITLYTICS_DATA_DIR` CSV archives to repositories owned by or accessible to the authenticated token, public username lookups filter with `is:public`, and `export_public_only=True` excludes all rows of any repository marked private.
- **Security Headers**: The packaged `src/gitlytics/static/_headers` file carries a hash-pinned `Content-Security-Policy` (`frame-ancestors 'none'`, `object-src 'none'`), applied by the local FastAPI server to every response.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report vulnerabilities privately using either:
1. GitHub's **Private Vulnerability Reporting** (**Security** tab -> **Advisories** -> **Report a vulnerability**).
2. Email **contact@gitlytics.dev** (or **ameyasanjaychopade@gmail.com**) with `[SECURITY]` in the subject line.

Please include:
- **Affected Repository & Component** (e.g. `gitlytics` CLI, `gitlytics mcp`, Python SDK, or local dashboard)
- **Impact & Detailed Description**
- **Steps to Reproduce** or minimal Proof of Concept

## Response SLA

1. **Acknowledgment**: Within 72 hours.
2. **Triage & Investigation**: Severity assessment and root-cause analysis.
3. **Fix & Advisory**: Coordinated patch release and credit (if desired).
