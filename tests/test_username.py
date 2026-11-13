"""
tests/test_username.py
Unit tests for the username-mode feature:
  - core.get_public_user()
  - core.get_public_repos()  — Option 3: merges recent + starred, top 50
  - api.get_username_data()  (/api/username endpoint)

All GitHub API calls are mocked — no token or internet required.

Run with:
    python -m pytest tests/test_username.py -v
"""
import pytest
from unittest.mock import patch, MagicMock

from gitlytics.core import get_public_user, get_public_repos


# ── get_public_user ───────────────────────────────────────────────────────────

class TestGetPublicUser:
    @patch("gitlytics.core._http_get")
    def test_valid_user_returns_profile(self, mock_get):
        # A 200 response should give back the parsed profile fields
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "login": "ameyachopade",
            "name": "Ameya Chopade",
            "avatar_url": "https://avatars.githubusercontent.com/u/1",
            "bio": "Developer",
            "location": "India",
            "blog": "https://example.com",
            "twitter_username": "ameyachopade",
            "html_url": "https://github.com/ameyachopade",
            "followers": 100,
            "following": 50,
            "public_repos": 20,
            "created_at": "2020-01-01T00:00:00Z",
        }
        mock_get.return_value = mock_resp
        profile = get_public_user("ameyachopade")
        assert profile["login"] == "ameyachopade"
        assert profile["name"] == "Ameya Chopade"
        assert profile["followers"] == 100
        assert profile["public_repos"] == 20

    @patch("gitlytics.core._http_get")
    def test_null_name_falls_back_to_login(self, mock_get):
        # GitHub allows empty display names — must fall back to login
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "login": "ghost",
            "name": None,
            "avatar_url": "",
            "html_url": "https://github.com/ghost",
            "followers": 0,
            "following": 0,
            "public_repos": 0,
            "created_at": "",
        }
        mock_get.return_value = mock_resp
        profile = get_public_user("ghost")
        assert profile["name"] == "ghost"

    @patch("gitlytics.core._http_get")
    def test_unknown_user_raises_value_error(self, mock_get):
        # A 404 from the GitHub API should raise a ValueError with the username
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp
        with pytest.raises(ValueError, match="ameyachopade"):
            get_public_user("ameyachopade")

    @patch("gitlytics.core._http_get")
    def test_network_error_raises_runtime_error(self, mock_get):
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError("offline")
        with pytest.raises(RuntimeError, match="unreachable"):
            get_public_user("someuser")

    @patch("gitlytics.core._http_get")
    def test_returns_all_expected_keys(self, mock_get):
        # Every field the dashboard depends on must be present
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "login": "user",
            "name": "User Name",
            "avatar_url": "https://x",
            "bio": None,
            "location": None,
            "blog": "",
            "twitter_username": None,
            "html_url": "https://github.com/user",
            "followers": 5,
            "following": 2,
            "public_repos": 3,
            "created_at": "2021-01-01T00:00:00Z",
        }
        mock_get.return_value = mock_resp
        profile = get_public_user("user")
        for key in ("login", "name", "avatar_url", "followers", "following",
                    "public_repos", "created_at", "html_url"):
            assert key in profile, f"Missing key: {key}"

    @patch("gitlytics.core._http_get")
    def test_non_404_server_error_raises_runtime_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp
        with pytest.raises(RuntimeError):
            get_public_user("someone")

    @patch("gitlytics.core._http_get")
    def test_html_url_present(self, mock_get):
        # html_url is required by the dashboard to link to the GitHub profile page
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "login": "user",
            "name": "User",
            "avatar_url": "",
            "html_url": "https://github.com/user",
            "followers": 0,
            "following": 0,
            "public_repos": 0,
            "created_at": "",
        }
        mock_get.return_value = mock_resp
        profile = get_public_user("user")
        assert profile["html_url"] == "https://github.com/user"


# ── get_public_repos (Option 3: two-call merge) ───────────────────────────────

def _make_repo(name="repo", stars=10, fork=False, language="Python"):
    """Build a minimal normalised repo dict matching _normalise_repo output."""
    return {
        "name": name,
        "full_name": f"user/{name}",
        "description": "Test repo",
        "html_url": f"https://github.com/user/{name}",
        "fork": fork,
        "stargazers_count": stars,
        "forks_count": 2,
        "watchers_count": 2,
        "language": language,
        "open_issues_count": 0,
        "topics": ["python"],
        "pushed_at": "2025-01-01T00:00:00Z",
        "created_at": "2020-01-01T00:00:00Z",
        "default_branch": "main",
    }


class TestGetPublicRepos:
    @patch("gitlytics.core._fetch_public_repos_by_stars", return_value=[])
    @patch("gitlytics.core._fetch_public_repos_by_updated", return_value=[_make_repo()])
    def test_returns_list(self, mock_recent, mock_starred):
        # Should always return a list
        result = get_public_repos("user")
        assert isinstance(result, list)
        assert len(result) == 1

    @patch("gitlytics.core._fetch_public_repos_by_stars", return_value=[])
    @patch("gitlytics.core._fetch_public_repos_by_updated", return_value=[_make_repo("myrepo", stars=42)])
    def test_each_repo_has_required_fields(self, mock_recent, mock_starred):
        # Each repo object needs the fields the dashboard uses
        repos = get_public_repos("user")
        assert len(repos) == 1
        repo = repos[0]
        for field in ("name", "full_name", "html_url", "stargazers_count",
                      "forks_count", "language", "topics", "fork"):
            assert field in repo, f"Missing field: {field}"

    @patch("gitlytics.core._fetch_public_repos_by_stars", return_value=[])
    @patch("gitlytics.core._fetch_public_repos_by_updated", return_value=[])
    def test_both_empty_returns_empty_list(self, mock_recent, mock_starred):
        # If both calls return nothing, result should be an empty list
        result = get_public_repos("user")
        assert result == []

    @patch("gitlytics.core._fetch_public_repos_by_stars", return_value=[_make_repo("star_repo", stars=500)])
    @patch("gitlytics.core._fetch_public_repos_by_updated", return_value=[_make_repo("new_repo", stars=1)])
    def test_deduplicates_repos_from_both_calls(self, mock_recent, mock_starred):
        # A repo appearing in both recent and starred should appear only once
        starred_and_recent = [_make_repo("shared", stars=100)]
        mock_recent.return_value = starred_and_recent
        mock_starred.return_value = starred_and_recent
        result = get_public_repos("user")
        names = [r["full_name"] for r in result]
        assert names.count("user/shared") == 1

    @patch("gitlytics.core._fetch_public_repos_by_stars", return_value=[_make_repo("star_repo", stars=500)])
    @patch("gitlytics.core._fetch_public_repos_by_updated", return_value=[_make_repo("new_repo", stars=1)])
    def test_sorted_by_stars_descending(self, mock_recent, mock_starred):
        # Result must be sorted highest stars first so the dashboard shows best repos first
        result = get_public_repos("user")
        stars = [r["stargazers_count"] for r in result]
        assert stars == sorted(stars, reverse=True)

    @patch("gitlytics.core._fetch_public_repos_by_stars", return_value=[])
    @patch("gitlytics.core._fetch_public_repos_by_updated")
    def test_capped_at_max_repos(self, mock_recent, mock_starred):
        # Should never return more than max_repos items
        mock_recent.return_value = [_make_repo(f"repo{i}", stars=i) for i in range(80)]
        result = get_public_repos("user", max_repos=50)
        assert len(result) <= 50

    @patch("gitlytics.core._fetch_public_repos_by_stars", return_value=[_make_repo("fork_repo", fork=True)])
    @patch("gitlytics.core._fetch_public_repos_by_updated", return_value=[])
    def test_fork_field_preserved(self, mock_recent, mock_starred):
        # fork=True must be preserved so the dashboard can filter forked repos
        repos = get_public_repos("user")
        assert repos[0]["fork"] is True

    @patch("gitlytics.core._fetch_public_repos_by_stars")
    @patch("gitlytics.core._fetch_public_repos_by_updated")
    def test_merges_distinct_repos_from_both_calls(self, mock_recent, mock_starred):
        # Repos unique to each call should all appear in the merged result
        mock_recent.return_value = [_make_repo("recent_only", stars=5)]
        mock_starred.return_value = [_make_repo("starred_only", stars=100)]
        result = get_public_repos("user")
        names = {r["name"] for r in result}
        assert "recent_only" in names
        assert "starred_only" in names


# ── /api/username endpoint ────────────────────────────────────────────────────

try:
    from fastapi.testclient import TestClient
    from gitlytics.api import app
    HAS_TESTCLIENT = True
except Exception:
    HAS_TESTCLIENT = False

_client_mark = pytest.mark.skipif(
    not HAS_TESTCLIENT,
    reason="httpx not installed — run: pip install httpx",
)

if HAS_TESTCLIENT:
    _client = TestClient(app, raise_server_exceptions=True)


@_client_mark
class TestUsernameEndpoint:
    def _post(self, username: str):
        return _client.post("/api/username", json={"username": username})

    @patch("gitlytics.api.get_public_user", return_value={
        "login": "ameyachopade", "name": "Ameya", "avatar_url": "https://x",
        "html_url": "https://github.com/ameyachopade",
        "followers": 10, "following": 5, "public_repos": 3, "created_at": "",
    })
    @patch("gitlytics.api.get_public_repos", return_value=[])
    def test_valid_username_returns_200(self, mock_repos, mock_user):
        # A valid username must return HTTP 200 with profile and repos
        response = self._post("ameyachopade")
        assert response.status_code == 200

    @patch("gitlytics.api.get_public_user", return_value={
        "login": "ameyachopade", "name": "Ameya", "avatar_url": "https://x",
        "html_url": "https://github.com/ameyachopade",
        "followers": 10, "following": 5, "public_repos": 3, "created_at": "",
    })
    @patch("gitlytics.api.get_public_repos", return_value=[])
    def test_response_has_profile_and_repos_keys(self, mock_repos, mock_user):
        # The response body must have 'profile' and 'repos' at the top level
        data = self._post("ameyachopade").json()
        assert "profile" in data
        assert "repos" in data

    @patch("gitlytics.api.get_public_user", return_value={
        "login": "ameyachopade", "name": "Ameya", "avatar_url": "https://x",
        "html_url": "https://github.com/ameyachopade",
        "followers": 10, "following": 5, "public_repos": 3, "created_at": "",
    })
    @patch("gitlytics.api.get_public_repos", return_value=[{"name": "repo1", "full_name": "ameyachopade/repo1"}])
    def test_repos_is_a_list(self, mock_repos, mock_user):
        # The repos field must always be a list (even if empty)
        data = self._post("ameyachopade").json()
        assert isinstance(data["repos"], list)

    def test_empty_username_returns_400(self):
        # Sending an empty string must return HTTP 400
        response = self._post("")
        assert response.status_code == 400

    def test_whitespace_username_returns_400(self):
        # A username made of only whitespace is invalid and must return 400
        response = self._post("   ")
        assert response.status_code == 400

    @patch("gitlytics.api.get_public_user", side_effect=ValueError("User 'nobody' not found."))
    def test_unknown_user_returns_404(self, mock_user):
        # A non-existent GitHub username must return HTTP 404
        response = self._post("nobody")
        assert response.status_code == 404
        assert "detail" in response.json()

    @patch("gitlytics.api.get_public_user", side_effect=Exception("unexpected"))
    def test_unexpected_error_returns_500(self, mock_user):
        # An unexpected backend error must return 500, not crash the server
        response = self._post("someuser")
        assert response.status_code == 500

    @patch("gitlytics.api.get_public_user", return_value={
        "login": "ameyachopade", "name": "Ameya", "avatar_url": "https://x",
        "html_url": "https://github.com/ameyachopade",
        "followers": 10, "following": 5, "public_repos": 3, "created_at": "",
    })
    @patch("gitlytics.api.get_public_repos", return_value=[])
    def test_profile_has_login_field(self, mock_repos, mock_user):
        # The profile dict inside the response must contain login at minimum
        data = self._post("ameyachopade").json()
        assert data["profile"]["login"] == "ameyachopade"

    @patch("gitlytics.api.get_public_user", return_value={
        "login": "ameyachopade", "name": "Ameya", "avatar_url": "https://x",
        "html_url": "https://github.com/ameyachopade",
        "followers": 50, "following": 5, "public_repos": 12, "created_at": "",
    })
    @patch("gitlytics.api.get_public_repos", return_value=[])
    def test_username_stripped_before_lookup(self, mock_repos, mock_user):
        # Leading/trailing whitespace around username must be stripped before the API call
        response = self._post("  ameyachopade  ")
        assert response.status_code == 200
        mock_user.assert_called_once_with("ameyachopade")

    def _get(self, username: str):
        return _client.get(f"/api/username/{username}")

    @patch("gitlytics.api.get_public_user", return_value={
        "login": "ameyachopade", "name": "Ameya", "avatar_url": "https://x",
        "html_url": "https://github.com/ameyachopade",
        "followers": 10, "following": 5, "public_repos": 3, "created_at": "",
    })
    @patch("gitlytics.api.get_public_repos", return_value=[])
    def test_get_valid_username_returns_200(self, mock_repos, mock_user):
        response = self._get("ameyachopade")
        assert response.status_code == 200
        data = response.json()
        assert "profile" in data
        assert "repos" in data
        assert data["profile"]["login"] == "ameyachopade"

    @patch("gitlytics.api.get_public_user", side_effect=ValueError("User 'nobody' not found."))
    def test_get_unknown_user_returns_404(self, mock_user):
        response = self._get("nobody")
        assert response.status_code == 404
        assert "detail" in response.json()
