# Security Policy

## Supported Versions

We actively support the latest release. Please update to the latest version to ensure you have the latest security patches.

| Version | Supported |
| ------- | --------- |
| 0.8.x   | :white_check_mark: Yes |
| < 0.8.0 | :x: No |

## Local Dashboard & Data Security Controls

- **Loopback Default & Non-Loopback Gate**: `gitlytics dashboard` binds to `127.0.0.1` by default. Any non-loopback `--host` (`0.0.0.0`, `::`, or a LAN IP) requires `--public-bind`, which automatically drops pre-configured server tokens (`--token` and `GITLYTICS_TOKEN`) so each caller must supply their own GitHub token.
- **No Ambient `GITHUB_TOKEN` Promotion**: `gitlytics dashboard` never silently promotes an ambient `GITHUB_TOKEN` from your shell into the HTTP server; pass `--token` or set `GITLYTICS_TOKEN` explicitly on loopback if desired.
- **Trusted `Host` & Same-Origin Checks**: Browser requests are validated against trusted loopback hosts (`localhost`, `127.0.0.1`, `[::1]`, or `GITLYTICS_ALLOWED_HOSTS` for reverse proxies) and require `application/json` on JSON `POST` routes.
- **Upload Access & Size Caps**: `POST /api/upload-csv` enforces `GITLYTICS_UPLOAD_KEY` (via `X-Upload-Key`) whenever set, caps request bodies at 5 MiB before multipart parsing, and limits parsed CSVs to 50,000 rows and 2,000 unique repositories.
- **Principal & Privacy Isolation**: `POST /api/traffic` scopes `GITLYTICS_DATA_DIR` CSV archives to repositories owned by or accessible to the authenticated token, public username lookups filter with `is:public`, and `export_public_only=True` excludes all rows of any repository marked private.
- **Security Headers**: The packaged `src/gitlytics/static/_headers` file carries the `Content-Security-Policy` (hash-pinned `script-src`, `frame-ancestors 'none'`, `object-src 'none'`), and the local FastAPI server applies those headers (excluding HSTS on local HTTP) to every response.

## Reporting a Vulnerability

If you discover a security vulnerability within Gitlytics, please report it privately to avoid exposing users to risk before a fix is available.

**Do not open a public GitHub issue for security vulnerabilities.**

### How to Report

Please report vulnerabilities privately using either:
1. GitHub's built-in **Private Vulnerability Reporting** feature (**Security** tab -> **Advisories** -> **Report a vulnerability**).
2. Email at **ameyasanjaychopade@gmail.com** with `[SECURITY]` in the subject line.

Please include the following details in your report:
- **Affected Component**: CLI/Python library, local dashboard API, or frontend bundle.
- **Detailed Description**: A clear description of the vulnerability and its potential impact.
- **Steps to Reproduce**: Step-by-step instructions or proof-of-concept (PoC) code to reproduce the issue.

### Our Response Process

1. **Acknowledgment**: We aim to acknowledge receipt of your report within 72 hours.
2. **Investigation**: We will investigate the issue and determine the severity and impact.
3. **Fix & Release**: If confirmed, we will work on a patch and release it in a new version as soon as possible.
