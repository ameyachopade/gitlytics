"""Offline security and protocol tests for the local stdio MCP server (`gitlytics mcp`)."""
from __future__ import annotations

import io
import json

from gitlytics.cli.parser import build_parser
from gitlytics.mcp.server import LOCAL_TOOL_DEFINITIONS, LocalMcpServer, run_stdio_server


def test_no_local_mcp_tool_exposes_token_parameter():
    assert len(LOCAL_TOOL_DEFINITIONS) == 5
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
    assert len(list_resp["result"]["tools"]) == 5


def test_cli_parser_accepts_mcp_subcommand():
    parser = build_parser()
    args = parser.parse_args(["mcp", "--data-dir", "./vault"])
    assert args.command == "mcp"
    assert args.data_dir == "./vault"
    assert args.api_base == "https://api.gitlytics.dev"
