# Security Policy

## Supported Versions

We actively support the latest release. Please update to the latest version to ensure you have the latest security patches.

| Version | Supported |
| ------- | --------- |
| 0.7.x   | :white_check_mark: Yes |
| < 0.7.0 | :x: No |

## Reporting a Vulnerability

If you discover a security vulnerability within Gitlytics, please report it privately to avoid exposing users to risk before a fix is available.

**Do not open a public GitHub issue for security vulnerabilities.** Use the private **Private Vulnerability Reporting** feature (see below).

### Security Headers

The local dashboard (`gitlytics dashboard`) and the hosted dashboard serve the same
bundle, so they ship the same protections. The packaged
`src/gitlytics/static/_headers` file carries the `Content-Security-Policy`
(hash-pinned `script-src`, `frame-ancestors 'none'`, `object-src 'none'`) and the
local FastAPI server applies it to every response it serves.

If you self-host the bundle on a CDN (Cloudflare Pages / Netlify), that host
reads `_headers` directly — the same policy applies either way.

### How to Report

Please report vulnerabilities privately using GitHub's built-in **Private Vulnerability Reporting** feature. You can find this by navigating to the "Security" tab of this repository on GitHub and selecting "Advisories" -> "Report a vulnerability".

Please include the following details in your report:
- **Affected Component**: CLI/Python library, frontend dashboard, or backend.
- **Detailed Description**: A clear description of the vulnerability and its potential impact.
- **Steps to Reproduce**: Step-by-step instructions or proof-of-concept (PoC) code to reproduce the issue.

### Our Response Process

1. **Acknowledgment**: We aim to acknowledge receipt of your report within 72 hours.
2. **Investigation**: We will investigate the issue and determine the severity and impact.
3. **Fix & Release**: If confirmed, we will work on a patch and release it in a new version as soon as possible.

