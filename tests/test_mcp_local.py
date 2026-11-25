"""Offline security and protocol tests for the local stdio MCP server (`gitlytics mcp`)."""
from __future__ import annotations

import io
import json

from gitlytics.cli.parser import build_parser
from gitlytics.mcp.server import LOCAL_TOOL_DEFINITIONS, LocalMcpServer, run_stdio_server


def test_no_local_mcp_tool_exposes_token_parameter():
    assert len(LOCAL_TOOL_DEFINITIONS) == 7
    for tool in LOCAL_TOOL_DEFINITIONS:
        props = tool["inputSchema"].get("properties", {})
        assert "token" not in props
        assert "github_token" not in props
        assert "api_key" not in props


def test_local_query_csv_vault_reads_archived_csv_offline(tmp_path, monkeypatch):
    monkeypatch.delenv("GITLYTICS_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    csv_file = tmp_path / "traffic_2026-11.csv"
    csv_file.write_text(
        "date,repo,views,unique_visitors,clones,unique_cloners\n"
        "2026-11-20,ameyachopade/gitlytics,100,60,20,15\n"
        "2026-11-21,ameyachopade/gitlytics,150,90,30,25\n",
        encoding="utf-8",
    )
    # Non-matching file should be ignored
    (tmp_path / "secrets.csv").write_text("secret\nshould_not_read\n", encoding="utf-8")

    server = LocalMcpServer(data_dir=str(tmp_path))
    result = server.call_tool("local_query_csv_vault", {"repo_name": "ameyachopade/gitlytics", "days": 30})
    assert result["isError"] is False
    sc = result["structuredContent"]
    assert sc["files_read"] == 1
    assert sc["row_count"] == 2
    assert sc["summary"]["total_views"] == 250
    assert sc["summary"]["total_clones"] == 50


def test_local_fetch_traffic_reads_env_token_and_fails_cleanly_when_missing(monkeypatch):
    monkeypatch.delenv("GITLYTICS_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    server = LocalMcpServer()
    res_missing = server.call_tool("local_fetch_traffic", {})
    assert res_missing["isError"] is True
    assert "GITLYTICS_TOKEN" in res_missing["structuredContent"]["error"]

    monkeypatch.setenv("GITLYTICS_TOKEN", "ghp_mock_env_token_123456")
    monkeypatch.setattr(
        "gitlytics.mcp.server.fetch_traffic",
        lambda **kwargs: {"repositories": [{"name": "ameyachopade/gitlytics", "views": 42}]},
    )
    res_ok = server.call_tool("local_fetch_traffic", {"repo_name": "ameyachopade/gitlytics"})
    assert res_ok["isError"] is False
    assert res_ok["structuredContent"]["repositories"][0]["views"] == 42


def test_stdio_transport_emits_pure_jsonrpc_and_handles_initialize_and_list(tmp_path):
    in_buf = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    )
    out_buf = io.StringIO()
    run_stdio_server(data_dir=str(tmp_path), stdin=in_buf, stdout=out_buf)

    lines = [ln for ln in out_buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2
    init_resp = json.loads(lines[0])
    list_resp = json.loads(lines[1])
    assert init_resp["result"]["serverInfo"]["name"] == "gitlytics"
    assert len(list_resp["result"]["tools"]) == 7


def test_cli_parser_accepts_mcp_subcommand():
    parser = build_parser()
    args = parser.parse_args(["mcp", "--data-dir", "./vault"])
    assert args.command == "mcp"
    assert args.data_dir == "./vault"
    assert args.api_base == "https://api.gitlytics.dev"


def test_local_build_widget_batch_and_new_badge_kinds():
    server = LocalMcpServer()
    res = server.call_tool(
        "local_build_widget_batch",
        {
            "defaults": {
                "theme": "gitlytics-dark",
                "variant": "clay",
                "owner": "ameyachopade",
                "repo": "gitlytics",
            },
            "layout": "inline_markdown",
            "items": [
                {"id": "py", "kind": "tech_badge", "slug": "python", "href": "https://python.org"},
                {"id": "npm", "kind": "npm_badge", "package_name": "@gitlytics/cli", "color": "E25A1C"},
                {"id": "sk", "kind": "skills_badge", "repo": "gitlytics-plugin"},
                {"id": "lg", "kind": "logo", "slug": "fastapi", "color": "009688"},
                {"id": "sh", "kind": "star_history"},
                {"id": "bad", "kind": "pypi_badge", "package_name": "../bad"},
            ],
        },
    )
    assert res["isError"] is False
    sc = res["structuredContent"]
    assert sc["requested"] == 6
    assert sc["succeeded"] == 5
    assert sc["failed"] == 1
    assert sc["errors"][0]["id"] == "bad"
    snippet = sc["combined_snippet"]
    assert "/api/badge/npm/@gitlytics/cli.svg?" in snippet
    assert "/api/badge/skills/ameyachopade/gitlytics-plugin.svg?" in snippet
    assert "/api/logos/fastapi.svg?" in snippet
    assert "icon_color=009688" in snippet
    assert "](https://python.org)" in snippet
    assert "/api/star-history-image?" in snippet
    assert "owner=ameyachopade" in snippet and "repo=gitlytics" in snippet


def test_local_vault_granularity_pruning_summary_resources_prompts_and_batch(tmp_path):
    # Older archive outside the 30-day window should be pruned before reading
    (tmp_path / "traffic_2022-01.csv").write_text(
        "date,repo,views,unique_visitors,clones,unique_cloners\n"
        "2022-01-10,ameyachopade/old,999,500,100,50\n",
        encoding="utf-8",
    )
    (tmp_path / "traffic_2026-11.csv").write_text(
        "date,repo,views,unique_visitors,clones,unique_cloners\n"
        "2026-11-16,ameyachopade/gitlytics,60,30,10,6\n"
        "2026-11-17,ameyachopade/gitlytics,40,20,5,4\n"
        "2026-11-24,ameyachopade/gitlytics,100,50,15,10\n",
        encoding="utf-8",
    )

    server = LocalMcpServer(data_dir=str(tmp_path))

    # Weekly granularity skips old archive and omits raw rows
    w_res = server.call_tool("local_query_csv_vault", {"days": 30, "granularity": "weekly"})
    assert w_res["isError"] is False
    w_sc = w_res["structuredContent"]
    assert w_sc["files_read"] == 1
    assert w_sc["files_skipped"] == 1
    assert "rows" not in w_sc
    assert len(w_sc["weekly"]) == 2
    assert w_sc["summary"]["clone_to_visitor_ratio"] == 0.2

    # local_vault_summary returns file inventory and per-repo breakdown
    s_res = server.call_tool("local_vault_summary", {"days": 30})
    assert s_res["isError"] is False
    s_sc = s_res["structuredContent"]
    assert len(s_sc["files"]) == 2
    assert s_sc["by_repo"][0]["repo"] == "ameyachopade/gitlytics"

    # Unknown tool argument is rejected cleanly
    bad_arg = server.call_tool("local_vault_summary", {"dayss": 30})
    assert bad_arg["isError"] is True

    # Stdio JSON-RPC batch + resources + prompts
    in_buf = io.StringIO(
        json.dumps([
            {"jsonrpc": "2.0", "id": 10, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 11, "method": "resources/read", "params": {"uri": "gitlytics://local/files"}},
            {"jsonrpc": "2.0", "id": 12, "method": "prompts/list"},
            {"jsonrpc": "2.0", "id": 13, "method": "prompts/get", "params": {"name": "create_badge_row", "arguments": {"owner": "ameyachopade", "repo": "gitlytics"}}},
        ]) + "\n"
    )
    out_buf = io.StringIO()
    run_stdio_server(data_dir=str(tmp_path), stdin=in_buf, stdout=out_buf)
    batch_resp = json.loads(out_buf.getvalue().strip())
    assert [r["id"] for r in batch_resp] == [10, 11, 12, 13]
    assert len(batch_resp[0]["result"]["resources"]) == 2
    assert len(batch_resp[2]["result"]["prompts"]) == 2
    assert "local_build_widget_batch" in batch_resp[3]["result"]["messages"][0]["content"]["text"]
