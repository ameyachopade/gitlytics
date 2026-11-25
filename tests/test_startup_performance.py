"""Tests verifying lazy imports and zero eager pandas/numpy loading on startup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def _run_python(code: str) -> dict:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    out = subprocess.check_output(
        [sys.executable, "-c", code],
        text=True,
        env=env,
        timeout=60,
    )
    return json.loads(out)


def test_import_gitlytics_defers_heavy_dependencies():
    code = (
        "import json, sys, gitlytics; "
        "v = gitlytics.__version__; "
        "print(json.dumps({"
        "'version': bool(v), "
        "'pandas': 'pandas' in sys.modules, "
        "'numpy': 'numpy' in sys.modules, "
        "'requests': 'requests' in sys.modules, "
        "'croniter': 'croniter' in sys.modules"
        "}))"
    )
    loaded = _run_python(code)
    assert loaded == {
        "version": True,
        "pandas": False,
        "numpy": False,
        "requests": False,
        "croniter": False,
    }


def test_cli_parser_and_commands_do_not_import_pandas_or_numpy():
    code = (
        "import json, sys; "
        "from gitlytics.cli.commands import main; "
        "from gitlytics.cli.parser import build_parser; "
        "p = build_parser(); "
        "assert p.prog == 'gitlytics'; "
        "print(json.dumps({"
        "'pandas': 'pandas' in sys.modules, "
        "'numpy': 'numpy' in sys.modules, "
        "'requests': 'requests' in sys.modules, "
        "'croniter': 'croniter' in sys.modules"
        "}))"
    )
    loaded = _run_python(code)
    assert loaded == {
        "pandas": False,
        "numpy": False,
        "requests": False,
        "croniter": False,
    }


def test_cli_help_and_version_defer_pandas_and_numpy():
    code = (
        "import io, contextlib, json, sys\n"
        "from gitlytics.cli import main\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    try:\n"
        "        main(['--version'])\n"
        "    except SystemExit as exc:\n"
        "        assert exc.code == 0\n"
        "print(json.dumps({"
        "'version_out': 'gitlytics' in buf.getvalue(), "
        "'pandas': 'pandas' in sys.modules, "
        "'numpy': 'numpy' in sys.modules"
        "}))"
    )
    loaded = _run_python(code)
    assert loaded == {
        "version_out": True,
        "pandas": False,
        "numpy": False,
    }


def test_mcp_server_import_defers_pandas_and_numpy():
    code = (
        "import json, sys\n"
        "from gitlytics.mcp.server import LocalMcpServer\n"
        "srv = LocalMcpServer()\n"
        "res = srv.handle_message({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})\n"
        "assert len(res['result']['tools']) == 5\n"
        "print(json.dumps({"
        "'pandas': 'pandas' in sys.modules, "
        "'numpy': 'numpy' in sys.modules, "
        "'requests': 'requests' in sys.modules, "
        "'croniter': 'croniter' in sys.modules"
        "}))"
    )
    loaded = _run_python(code)
    assert loaded == {
        "pandas": False,
        "numpy": False,
        "requests": False,
        "croniter": False,
    }


def test_lazy_exports_still_resolve_and_match_submodules():
    import gitlytics
    from gitlytics.core.stars import fetch_star_history
    from gitlytics.io.atomic import _write_csv_safely, _write_json_safely
    from gitlytics.sdk import _coerce_metrics, fetch_traffic, serve_dashboard, sync

    assert gitlytics.fetch_traffic is fetch_traffic
    assert gitlytics.sync is sync
    assert gitlytics.serve_dashboard is serve_dashboard
    assert gitlytics._coerce_metrics is _coerce_metrics
    assert gitlytics.fetch_star_history is fetch_star_history
    assert gitlytics._write_csv_safely is _write_csv_safely
    assert gitlytics._write_json_safely is _write_json_safely
    assert "fetch_traffic" in dir(gitlytics)
    assert "fetch_star_history" in dir(gitlytics)
