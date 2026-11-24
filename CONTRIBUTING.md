# Contributing to Gitlytics (Core Library & CLI)

Thank you for contributing to the **Gitlytics Ecosystem**. This repository contains the `gitlytics` Python package, CLI (`fetch`, `sync`, `stars`, `dashboard`, `mcp`), Python SDK, local stdio MCP server, and self-hosted dashboard server.

## Getting Started

1. Fork and clone the repository:
   ```bash
   git clone https://github.com/ameyachopade/gitlytics.git
   cd gitlytics
   ```
2. Install in editable mode with development dependencies (Python 3.11+):
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -e ".[dev]"
   ```
3. Create a feature branch:
   ```bash
   git checkout -b feat/your-change
   ```

## Local Verification

Run the test suite before opening a pull request:

```bash
python -m pytest -q
```

Key suites to run when touching specific subsystems:
- CLI & local MCP server: `python -m pytest -q tests/test_cli.py tests/test_init.py tests/test_mcp_local.py`
- CSV archive & sync logic: `python -m pytest -q tests/test_csv.py tests/test_sync.py`

## Standards & Guardrails

- **Version Synchronization**: Keep `version` in `pyproject.toml`, `src/gitlytics/_version.py`, and `src/gitlytics/static/index.html` in sync.
- **Zero Secrets**: Never commit `.env` files, GitHub PATs (`ghp_...` / `github_pat_...`), or local CSV archives containing private repository data.
- **Local MCP Safety**: Never add raw token parameters to `gitlytics mcp` tool schemas; read tokens strictly from environment variables (`GITLYTICS_TOKEN` / `GITHUB_TOKEN`) and enforce `--data-dir` path containment.
- **Security Issues**: Follow [SECURITY.md](SECURITY.md) and report vulnerabilities privately rather than opening a public issue.

## Pull Request Checklist

- [ ] Tests pass locally (`python -m pytest -q`)
- [ ] New CLI flags, SDK functions, or MCP tools include unit tests and README updates
- [ ] No secrets, tokens, or temporary artifacts are staged
- [ ] Commit messages and PR description clearly explain the change

## The Gitlytics Ecosystem

| Project | Role | Link |
| :--- | :--- | :--- |
| [gitlytics](https://github.com/ameyachopade/gitlytics) *(this repo)* | Core Python library, CLI, local stdio MCP & local dashboard | [PyPI](https://pypi.org/project/gitlytics) |
| [gitlytics-plugin](https://github.com/ameyachopade/gitlytics-plugin) | Official Antigravity plugin, MCP config & 4 `npx skills` | [skills.sh](https://skills.sh/ameyachopade/gitlytics-plugin) |
| [gitlytics-github-traffic-automation](https://github.com/ameyachopade/gitlytics-github-traffic-automation) | Composite GitHub Action for automated CSV backups | [GitHub Action](https://github.com/ameyachopade/gitlytics-github-traffic-automation) |
| **Gitlytics Web & Docs** | Homepage, Docs Hub & Engineering Blog | [gitlytics.dev](https://gitlytics.dev) / [docs.gitlytics.dev](https://docs.gitlytics.dev) / [blog.gitlytics.dev](https://blog.gitlytics.dev) |
| Gitlytics Cloud Dashboard & API | Hosted Analytics Dashboard, SVG Engine & MCP Server | [dashboard.gitlytics.dev](https://dashboard.gitlytics.dev) / [api.gitlytics.dev](https://api.gitlytics.dev) |
