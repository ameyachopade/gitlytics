"""
tests/test_cli.py
Unit tests for gitlytics/cli.py — argument parsing and command dispatch.
All underlying functions are mocked so these tests run offline without a token.
"""
import sys
import pytest
from unittest.mock import patch, MagicMock
from gitlytics.cli import main, parse_repo_names
from gitlytics import TokenExpiredError


# ── parse_repo_names ──────────────────────────────────────────────────────────

class TestParseRepoNames:
    def test_none_input_returns_none(self):
        # No repo filter means "fetch all repos" — None is the correct signal
        assert parse_repo_names(None) is None

    def test_empty_string_returns_none(self):
        # An empty string from the CLI should behave the same as no argument
        assert parse_repo_names("") is None

    def test_single_repo(self):
        # A single repo name should come back as a one-item list
        assert parse_repo_names("owner/repo") == ["owner/repo"]

    def test_comma_separated(self):
        # Comma-separated input should be split into individual repo names
        result = parse_repo_names("owner/repo1, owner/repo2")
        assert result == ["owner/repo1", "owner/repo2"]

    def test_strips_whitespace(self):
        # Extra spaces around the repo name should be cleaned up automatically
        result = parse_repo_names("  owner/repo  ")
        assert result == ["owner/repo"]


# ── CLI dispatch ──────────────────────────────────────────────────────────────

class TestCliNoArgs:
    def test_no_args_prints_help_and_exits_0(self, capsys):
        # Bug fix: no-arg invocation should exit 0 (standard CLI behavior), not 1
        with patch.object(sys, "argv", ["gitlytics"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 0


class TestCliFetchCommand:
    @patch("gitlytics.cli.fetch_traffic")
    def test_fetch_calls_fetch_traffic(self, mock_fetch):
        # The fetch subcommand must actually call the fetch_traffic function
        mock_fetch.return_value = MagicMock()
        with patch.object(sys, "argv", ["gitlytics", "fetch", "--token", "fake_token", "--print-table"]):
            main()
        mock_fetch.assert_called_once()
        call_kwargs = mock_fetch.call_args
        assert call_kwargs.kwargs.get("token") == "fake_token" or call_kwargs.args[0] == "fake_token"

    @patch("gitlytics.cli.fetch_traffic")
    def test_fetch_passes_repo_name(self, mock_fetch):
        # --repo-name must be parsed into a list and forwarded to fetch_traffic
        mock_fetch.return_value = MagicMock()
        with patch.object(sys, "argv", [
            "gitlytics", "fetch", "--token", "tok", "--repo-name", "user/repo"
        ]):
            main()
        mock_fetch.assert_called_once()

    @patch("gitlytics.cli.fetch_traffic")
    def test_fetch_missing_token_exits_1(self, mock_fetch):
        # Running fetch without any token must exit immediately with code 1
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(sys, "argv", ["gitlytics", "fetch"]):
                with pytest.raises(SystemExit) as exc:
                    main()
        assert exc.value.code == 1
        # fetch_traffic should never be called if the token is missing
        mock_fetch.assert_not_called()


class TestCliSyncCommand:
    @patch("gitlytics.cli.sync")
    def test_sync_calls_sync(self, mock_sync):
        # The sync subcommand must forward the call to the sync() function
        with patch.object(sys, "argv", [
            "gitlytics", "sync", "--token", "fake_token", "--data-dir", "./my_data"
        ]):
            main()
        mock_sync.assert_called_once()

    @patch("gitlytics.cli.sync")
    def test_export_public_only_default_is_true(self, mock_sync):
        # Without --no-export-public-only, private repos should be stripped by default
        with patch.object(sys, "argv", [
            "gitlytics", "sync", "--token", "tok"
        ]):
            main()
        _, kwargs = mock_sync.call_args
        assert kwargs.get("export_public_only", True) is True

    @patch("gitlytics.cli.sync")
    def test_no_export_public_only_flag_sets_false(self, mock_sync):
        # The --no-export-public-only flag must explicitly set export_public_only=False
        with patch.object(sys, "argv", [
            "gitlytics", "sync", "--token", "tok", "--no-export-public-only"
        ]):
            main()
        _, kwargs = mock_sync.call_args
        assert kwargs.get("export_public_only") is False


class TestCliDashboardCommand:
    @patch("gitlytics.cli.serve_dashboard")
    def test_dashboard_calls_serve_dashboard(self, mock_serve):
        # The dashboard subcommand must call serve_dashboard
        with patch.object(sys, "argv", ["gitlytics", "dashboard"]):
            main()
        mock_serve.assert_called_once()

    @patch("gitlytics.cli.serve_dashboard")
    def test_dashboard_passes_host_and_port(self, mock_serve):
        with patch.object(sys, "argv", [
            "gitlytics", "dashboard", "--host", "0.0.0.0", "--port", "9000", "--public-bind"
        ]):
            main()
        call_kwargs = mock_serve.call_args.kwargs
        assert call_kwargs.get("host") == "0.0.0.0"
        assert call_kwargs.get("port") == 9000


# ── Token whitespace + invalid repo name (v0.2.1 fixes) ──────────────────────

class TestCliTokenStripping:
    @patch("gitlytics.cli.fetch_traffic")
    def test_token_is_stripped(self, mock_fetch):
        # Bug fix: trailing whitespace from copy-paste should not cause a 401
        mock_fetch.return_value = MagicMock()
        with patch.object(sys, "argv", [
            "gitlytics", "fetch", "--token", "  spaced_token  ", "--print-table"
        ]):
            main()
        kwargs = mock_fetch.call_args.kwargs
        assert kwargs.get("token") == "spaced_token"


class TestCliRepoNameValidation:
    @patch("gitlytics.cli.fetch_traffic")
    def test_repo_name_without_slash_exits_2(self, mock_fetch, capsys):
        # Bug fix: a malformed --repo-name must exit 2 with a clear error, not crash
        with patch.object(sys, "argv", [
            "gitlytics", "fetch", "--token", "tok", "--repo-name", "no-slash-here"
        ]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 2
        mock_fetch.assert_not_called()


class TestCliTokenExpired:
    @patch("gitlytics.cli.sync")
    def test_sync_token_expiry_exits_1(self, mock_sync):
        # LIB-10: the library raises TokenExpiredError; only the CLI decides
        # to exit, and it must map the failure to exit code 1.
        mock_sync.side_effect = TokenExpiredError("token expired, revoked")
        with patch.object(sys, "argv", ["gitlytics", "sync", "--token", "tok"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1
