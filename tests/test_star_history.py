"""
tests/test_star_history.py
Unit tests for gitlytics/core.py::fetch_star_history.
All GitHub API calls are mocked so these run offline.
"""
import pytest
from unittest.mock import patch, MagicMock

from gitlytics.core import (
    fetch_star_history,
    GitHubRateLimitError,
    StarHistoryFetchError,
)


@pytest.fixture(autouse=True)
def _clear_restricted_cache():
    """Keep the process-local negative cache isolated between tests."""
    from gitlytics import core
    core._stargazer_restricted_cache.clear()
    yield
    core._stargazer_restricted_cache.clear()


def _meta_response(stargazers_count: int, status: int = 200, created_at: str = "2020-01-01T00:00:00Z"):
    """Build a mock GitHub repo metadata response."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {
        "stargazers_count": stargazers_count,
        "created_at": created_at,
    }
    return resp


def _stargazer_page(starred_at_list, status: int = 200):
    """Build a mock GitHub stargazers page response."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = [
        {"starred_at": ts, "user": {"login": f"user{i}"}}
        for i, ts in enumerate(starred_at_list)
    ]
    return resp


class TestFetchStarHistoryValidation:
    def test_empty_owner_raises_value_error(self):
        # Bad input must not hit the network — fail fast with ValueError.
        with pytest.raises(ValueError, match="requires owner"):
            fetch_star_history("", "repo")

    def test_empty_repo_raises_value_error(self):
        with pytest.raises(ValueError, match="requires owner"):
            fetch_star_history("owner", "")

    def test_slash_in_repo_raises_value_error(self):
        # A repo name like 'a/b/c' is malformed — refuse before the API call.
        with pytest.raises(ValueError, match="no '/'"):
            fetch_star_history("owner", "a/b/c")


class TestFetchStarHistoryErrors:
    @patch("gitlytics.core._http_get")
    def test_429_metadata_raises_github_rate_limit_error(self, mock_get):
        # The metadata call hits the rate limit — must raise the specific
        # GitHubRateLimitError class, not a generic Exception.
        mock_get.return_value = _meta_response(0, status=429)
        with pytest.raises(GitHubRateLimitError, match="rate limit"):
            fetch_star_history("owner", "repo")

    @patch("gitlytics.core._http_get")
    def test_403_metadata_raises_github_rate_limit_error(self, mock_get):
        # 403 with rate-limit semantics must also surface as the rate-limit class.
        mock_get.return_value = _meta_response(0, status=403)
        with pytest.raises(GitHubRateLimitError, match="rate limit"):
            fetch_star_history("owner", "repo")

    @patch("gitlytics.core._http_get")
    def test_500_metadata_raises_star_history_fetch_error(self, mock_get):
        # 500 is a server error, not a rate limit — must raise StarHistoryFetchError.
        mock_get.return_value = _meta_response(0, status=500)
        with pytest.raises(StarHistoryFetchError, match="metadata"):
            fetch_star_history("owner", "repo")

    @patch("gitlytics.core._http_get")
    def test_zero_stars_returns_single_today_point(self, mock_get):
        # A repo with zero stars returns a single point dated today with total=0.
        mock_get.return_value = _meta_response(0, status=200)
        points = fetch_star_history("owner", "repo")
        assert len(points) == 1
        assert points[0]["total"] == 0
        # Should NOT have made a second API call for the stargazers endpoint.
        assert mock_get.call_count == 1


class TestFetchStarHistorySmallRepo:
    """Repos with <= 200 stars use the fine-grained per-page walk."""

    @patch("gitlytics.core._http_get")
    def test_small_repo_walks_per_star(self, mock_get):
        # 5-star repo: 5 stargazers to walk. Use the small_per_page of 30 so
        # all 5 land in a single page-1 response. GitHub returns stargazers
        # in CHRONOLOGICAL order, so the page lists oldest first.
        meta = _meta_response(5, status=200)
        page = _stargazer_page(
            [
                "2024-01-01T00:00:00Z",  # items[0] = oldest = star #1
                "2024-01-02T00:00:00Z",  # star #2
                "2024-01-03T00:00:00Z",  # star #3
                "2024-01-04T00:00:00Z",  # star #4
                "2024-01-05T00:00:00Z",  # items[4] = newest = star #5
            ]
        )
        # Side effects: first call returns metadata, second returns the stargazer page.
        mock_get.side_effect = [meta, page]
        points = fetch_star_history("owner", "repo")
        # The function should return a monotonically-increasing cumulative timeline
        # with today's total equal to the live count (5).
        assert points, "Expected at least one point"
        assert points[-1]["total"] == 5
        # Cumulative totals must never decrease across dates.
        totals = [p["total"] for p in points]
        assert totals == sorted(totals)
        # All 5 star dates must appear in the timeline. (Today may also
        # appear as a trailing point, which is expected — the chart's
        # right edge is always today.)
        dates = {p["date"] for p in points}
        assert {
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
        }.issubset(dates), f"Expected all 5 star dates, got {dates}"
        # Network: metadata + 1 stargazer page.
        assert mock_get.call_count == 2

    @patch("gitlytics.core._http_get")
    def test_small_repo_position_maps_newest_to_highest(self, mock_get):
        # GitHub returns stargazers oldest first.
        # items[0] is the earliest star, items[-1] is the latest.
        # The timeline must end at the live count with
        # the newest date carrying the highest star number.
        meta = _meta_response(3, status=200)
        # Oldest first: items[0] is the earliest star, items[-1] is the latest.
        page = _stargazer_page(
            [
                "2024-04-01T00:00:00Z",  # oldest
                "2024-05-01T00:00:00Z",
                "2024-06-01T00:00:00Z",  # newest
            ]
        )
        mock_get.side_effect = [meta, page]
        points = fetch_star_history("owner", "repo")
        # The newest date (2024-06-01) should carry the highest cumulative
        # total for that date. (Today may also be appended at the end with
        # the live count of 3, but it must not be lower than 2024-06-01.)
        by_date = {p["date"]: p["total"] for p in points}
        assert by_date["2024-04-01"] < by_date["2024-05-01"] < by_date["2024-06-01"]
        # 2024-06-01 (the newest) carries position 3, the live total.
        assert by_date["2024-06-01"] == 3
        # 2024-04-01 (the oldest) carries position 1.
        assert by_date["2024-04-01"] == 1


class TestFetchStarHistoryLargeRepo:
    """Repos with > 200 stars use the 10-page sampling strategy."""

    @patch("gitlytics.core._http_get")
    def test_large_repo_samples_ten_pages(self, mock_get):
        # 5000-star repo: 50 pages of stargazers. The function should pick
        # 10 evenly-spaced pages across the 50-page range, including the
        # first and last. Each page returns 100 items with a distinct
        # `starred_at` (oldest item in the page = oldest star of that page).
        meta = _meta_response(5000, status=200)
        # Build 10 distinct page responses. Each page's LAST item carries
        # the date of the oldest star in that page.
        page_dates = [f"2020-{i+1:02d}-15T00:00:00Z" for i in range(10)]
        pages = [
            _stargazer_page([page_dates[i]] * 100) for i in range(10)
        ]
        # 1 metadata call + 10 page calls.
        mock_get.side_effect = [meta] + pages
        points = fetch_star_history("owner", "repo")
        # The total must monotonically increase to today's 5000.
        assert points[-1]["total"] == 5000
        # Timeline must span multiple distinct dates (not collapse to one).
        dates = {p["date"] for p in points}
        assert len(dates) >= 5, f"Expected >=5 distinct dates, got {len(dates)}: {dates}"
        # Network calls: metadata + 10 stargazer pages.
        assert mock_get.call_count == 11

    @patch("gitlytics.core._http_get")
    def test_large_repo_uses_last_item_per_page(self, mock_get):
        # Lock in the fix: the LAST item in each page is the oldest star
        # in that page, and that is what the chart should sample. If the
        # code reverts to using the first item (newest), this test fails.
        meta = _meta_response(5000, status=200)
        # Build 10 page responses. Each page has 100 items where the LAST
        # item is much older than the first. The sampled date should be
        # the LAST item's date (the oldest in the page).
        page_dates = [
            "2015-01-15T00:00:00Z",  # page 1, oldest = star #100
            "2016-01-15T00:00:00Z",  # page 6
            "2017-01-15T00:00:00Z",  # page 11
            "2018-01-15T00:00:00Z",  # page 16
            "2019-01-15T00:00:00Z",  # page 21
            "2020-01-15T00:00:00Z",  # page 26
            "2021-01-15T00:00:00Z",  # page 31 — distinctive date
            "2022-01-15T00:00:00Z",  # page 36
            "2023-01-15T00:00:00Z",  # page 41
            "2024-01-15T00:00:00Z",  # page 46, newest = star #4600
        ]
        pages = [
            _stargazer_page(["2000-01-01T00:00:00Z"] * 99 + [page_dates[i]])
            for i in range(10)
        ]
        # 1 metadata call + 10 page calls.
        mock_get.side_effect = [meta] + pages
        points = fetch_star_history("owner", "repo")
        # All 10 distinct oldest-page-item dates must appear in the timeline.
        dates = {p["date"] for p in points}
        for d in page_dates:
            assert d[:10] in dates, f"Expected {d[:10]} in timeline, got {dates}"

    @patch("gitlytics.core._http_get")
    def test_large_repo_pages_capped_at_422(self, mock_get):
        # 200,000-star repo: would need 2000 pages, but we cap at 422.
        # Only 10 page requests should be made, and none may reference
        # a page number greater than 422.
        meta = _meta_response(200_000, status=200)
        page = _stargazer_page(["2020-06-15T00:00:00Z"] * 100)
        mock_get.side_effect = [meta] + [page] * 10
        fetch_star_history("owner", "repo")
        # Inspect every stargazer page request and verify page <= 422.
        for call in mock_get.call_args_list:
            args, kwargs = call
            url = args[0] if args else kwargs.get("url", "")
            params = kwargs.get("params", {})
            if "/stargazers" in url:
                assert int(params.get("page", 1)) <= 422, (
                    f"Page number {params.get('page')} exceeds 422 cap"
                )
        # Metadata + 10 pages = 11 calls total.
        assert mock_get.call_count == 11

    @patch("gitlytics.core._http_get")
    def test_large_repo_429_on_stargazer_page_raises_rate_limit(self, mock_get):
        # If the metadata succeeds but a stargazer page hits 429,
        # the function must raise GitHubRateLimitError (not a generic Exception).
        meta = _meta_response(5000, status=200)
        rate_limited = _meta_response(0, status=429)
        mock_get.side_effect = [meta, rate_limited]
        with pytest.raises(GitHubRateLimitError, match="rate limit"):
            fetch_star_history("owner", "repo")


class TestFetchStarHistoryRestrictedFallback:
    """LIB-08: restricted stargazer lists return a documented approximation."""

    @patch("gitlytics.core._http_get")
    def test_large_repo_404_returns_two_point_approximation(self, mock_get):
        meta = _meta_response(5000, status=200, created_at="2019-03-04T12:00:00Z")
        restricted = _stargazer_page([], status=404)
        mock_get.side_effect = [meta, restricted]
        points = fetch_star_history("restricted-owner", "large-repo")
        assert points[0] == {"date": "2019-03-04", "total": 0}
        assert points[-1] == {"date": points[-1]["date"], "total": 5000}
        assert len(points) == 2
        # One metadata request and one failed stargazer page; no events call.
        assert mock_get.call_count == 2

    @patch("gitlytics.core._http_get")
    def test_small_repo_404_returns_approximation_without_events(self, mock_get):
        meta = _meta_response(3, status=200, created_at="2024-01-01T00:00:00Z")
        restricted = _stargazer_page([], status=404)
        mock_get.side_effect = [meta, restricted]
        points = fetch_star_history("restricted-owner", "small-repo")
        assert points[0] == {"date": "2024-01-01", "total": 0}
        assert points[-1]["total"] == 3
        assert mock_get.call_count == 2

    @patch("gitlytics.core._http_get")
    def test_restriction_is_negatively_cached(self, mock_get):
        meta = _meta_response(5000, status=200, created_at="2019-03-04T12:00:00Z")
        restricted = _stargazer_page([], status=404)
        mock_get.side_effect = [meta, restricted, _meta_response(5000, status=200, created_at="2019-03-04T12:00:00Z")]
        first = fetch_star_history("cached-owner", "cached-repo")
        second = fetch_star_history("cached-owner", "cached-repo")
        assert first == second
        # Second call fetched metadata only; no stargazer page was attempted.
        assert mock_get.call_count == 3

    @patch("gitlytics.core._http_get")
    def test_events_fallback_anchors_to_live_total(self, mock_get):
        meta = _meta_response(500, status=200, created_at="2020-01-01T00:00:00Z")
        empty_page = _stargazer_page([], status=200)
        events = MagicMock()
        events.status_code = 200
        events.json.return_value = [
            {"type": "WatchEvent", "created_at": "2026-11-01T00:00:00Z"},
            {"type": "WatchEvent", "created_at": "2026-11-02T00:00:00Z"},
            {"type": "PushEvent", "created_at": "2026-11-03T00:00:00Z"},
        ]
        mock_get.side_effect = [meta] + [empty_page] * 5 + [events]
        points = fetch_star_history("events-owner", "events-repo")
        by_date = {p["date"]: p["total"] for p in points}
        # Two observed watchers occupy the final two positions of 500.
        assert by_date["2026-11-01"] == 499
        assert by_date["2026-11-02"] == 500


class TestFetchStarHistoryTokenOptional:
    @patch("gitlytics.core._http_get")
    def test_no_token_still_works_for_public_repos(self, mock_get):
        # Public reads do not need auth. The function should pass through
        # without raising even when token=None.
        meta = _meta_response(0, status=200)
        mock_get.return_value = meta
        points = fetch_star_history("owner", "repo", token=None)
        assert points == [{"date": points[0]["date"], "total": 0}]