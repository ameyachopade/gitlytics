"""
live_username.py
Online integration test for the username-mode feature.
Tests both the core functions and the /api/username FastAPI endpoint.

No GitHub token is required — username mode is public.

Usage:
    python tests/live_username.py [username]

    If no username argument is given, defaults to 'torvalds' as a well-known
    public account guaranteed to have repos and followers.

Output is printed to stdout with colour-coded PASS / FAIL / WARN lines.
"""
import os
import sys
import json

if __name__ == "__main__":
    # Force UTF-8 output so Unicode symbols render on Windows terminals
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # ── Colour helpers ─────────────────────────────────────────────────────────
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

    def ok(msg):   print(f"  {GREEN}✓{RESET}  {msg}")
    def fail(msg): print(f"  {RED}✗{RESET}  {msg}")
    def info(msg): print(f"  {CYAN}ℹ{RESET}  {msg}")
    def warn(msg): print(f"  {YELLOW}⚠{RESET}  {msg}")
    def section(title):
        print(f"\n{BOLD}{'─'*55}{RESET}")
        print(f"{BOLD}  {title}{RESET}")
        print(f"{BOLD}{'─'*55}{RESET}")

    # ── Target username ────────────────────────────────────────────────────────
    USERNAME = sys.argv[1] if len(sys.argv) > 1 else "torvalds"
    MISSING  = "nonexistent-user-xyzzy-91827"

    # ── Import check ───────────────────────────────────────────────────────────
    section("0 / 4  Import check")
    try:
        from gitlytics.core import get_public_user, get_public_repos
        ok("gitlytics.core imported successfully")
    except ImportError as exc:
        fail(f"Could not import gitlytics.core: {exc}")
        print("\nMake sure you installed it with:\n  pip install -e .")
        sys.exit(1)

    try:
        from gitlytics.api import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        HAS_CLIENT = True
        ok("FastAPI TestClient available — /api/username endpoint tests enabled")
    except Exception as exc:
        HAS_CLIENT = False
        warn(f"FastAPI TestClient unavailable ({exc}) — skipping endpoint tests")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 1 — get_public_user()
    # ─────────────────────────────────────────────────────────────────────────
    section(f"1 / 4  get_public_user('{USERNAME}')")

    print(f"\n  [1a] Fetch profile for '{USERNAME}'")
    profile = None
    try:
        profile = get_public_user(USERNAME)
        ok(f"Profile returned for '{profile['login']}'")
        info(f"Name: {profile['name']}")
        info(f"Followers: {profile['followers']}, Following: {profile['following']}, Repos: {profile['public_repos']}")
        info(f"Profile URL: {profile['html_url']}")
    except Exception as exc:
        fail(f"get_public_user('{USERNAME}') raised: {exc}")

    print("\n  [1b] Validate required fields are present")
    REQUIRED_PROFILE_FIELDS = (
        "login", "name", "avatar_url", "html_url",
        "followers", "following", "public_repos", "created_at",
    )
    if profile:
        missing = [f for f in REQUIRED_PROFILE_FIELDS if f not in profile]
        if missing:
            fail(f"Missing profile fields: {missing}")
        else:
            ok(f"All {len(REQUIRED_PROFILE_FIELDS)} required profile fields present")
    else:
        warn("Skipped — no profile returned in [1a]")

    print(f"\n  [1c] Fetch profile for non-existent user '{MISSING}'")
    try:
        get_public_user(MISSING)
        fail("Expected ValueError — no exception was raised")
    except ValueError as exc:
        ok(f"Correctly raised ValueError: {exc}")
    except Exception as exc:
        fail(f"Wrong exception type: {type(exc).__name__}: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 2 — get_public_repos()
    # ─────────────────────────────────────────────────────────────────────────
    section(f"2 / 4  get_public_repos('{USERNAME}')")

    print(f"\n  [2a] Fetch public repos for '{USERNAME}'")
    repos = []
    try:
        repos = get_public_repos(USERNAME)
        assert isinstance(repos, list), "Expected a list"
        ok(f"Returned {len(repos)} repos")
        if repos:
            top = sorted(repos, key=lambda r: r.get("stargazers_count", 0), reverse=True)[0]
            info(f"Most starred: {top['full_name']} ★{top['stargazers_count']}")
            info(f"Languages: {sorted({r.get('language') for r in repos if r.get('language')})}")
        else:
            warn("Repo list is empty — user may have no public repos")
    except Exception as exc:
        fail(f"get_public_repos('{USERNAME}') raised: {exc}")

    print("\n  [2b] Validate required repo fields")
    REQUIRED_REPO_FIELDS = (
        "name", "full_name", "html_url", "stargazers_count",
        "forks_count", "language", "topics", "fork",
    )
    if repos:
        sample = repos[0]
        missing = [f for f in REQUIRED_REPO_FIELDS if f not in sample]
        if missing:
            fail(f"Missing repo fields: {missing}")
        else:
            ok(f"All {len(REQUIRED_REPO_FIELDS)} required repo fields present")
    else:
        warn("Skipped — no repos to validate")

    print(f"\n  [2c] Fetch repos for non-existent user '{MISSING}' — must return []")
    try:
        result = get_public_repos(MISSING)
        if result == []:
            ok("Returned [] for unknown user (no crash)")
        else:
            warn(f"Returned non-empty list for unknown user: {result[:1]}")
    except Exception as exc:
        fail(f"get_public_repos raised instead of returning []: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 3 — /api/username endpoint (requires FastAPI TestClient)
    # ─────────────────────────────────────────────────────────────────────────
    section("3 / 4  /api/username endpoint")

    if not HAS_CLIENT:
        warn("Skipped — FastAPI TestClient not available (pip install httpx)")
    else:
        print(f"\n  [3a] POST /api/username  username='{USERNAME}'")
        try:
            resp = client.post("/api/username", json={"username": USERNAME})
            if resp.status_code == 200:
                ok(f"HTTP 200 OK")
                data = resp.json()
                assert "profile" in data, "Missing 'profile' key in response"
                assert "repos" in data,   "Missing 'repos' key in response"
                ok(f"Response has 'profile' and 'repos' keys")
                info(f"Profile login: {data['profile'].get('login')}")
                info(f"Repos count: {len(data['repos'])}")
            else:
                fail(f"Unexpected HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            fail(f"/api/username raised: {exc}")

        print("\n  [3b] POST /api/username  username='' (empty)")
        try:
            resp = client.post("/api/username", json={"username": ""})
            if resp.status_code == 400:
                ok("Correctly returned 400 for empty username")
            else:
                fail(f"Expected 400 but got HTTP {resp.status_code}")
        except Exception as exc:
            fail(f"Empty username test raised: {exc}")

        print(f"\n  [3c] POST /api/username  username='{MISSING}' (non-existent)")
        try:
            resp = client.post("/api/username", json={"username": MISSING})
            if resp.status_code == 404:
                ok("Correctly returned 404 for non-existent user")
                info(f"Detail: {resp.json().get('detail', '')}")
            else:
                fail(f"Expected 404 but got HTTP {resp.status_code}")
        except Exception as exc:
            fail(f"Non-existent user test raised: {exc}")

        print("\n  [3d] POST /api/username  username='   ' (whitespace-only)")
        try:
            resp = client.post("/api/username", json={"username": "   "})
            if resp.status_code == 400:
                ok("Correctly returned 400 for whitespace-only username")
            else:
                fail(f"Expected 400 but got HTTP {resp.status_code}")
        except Exception as exc:
            fail(f"Whitespace username test raised: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 4 — Edge cases and data integrity
    # ─────────────────────────────────────────────────────────────────────────
    section("4 / 4  Data integrity checks")

    print(f"\n  [4a] Stars are non-negative integers")
    if repos:
        bad = [r for r in repos if not isinstance(r.get("stargazers_count", 0), int)
               or r.get("stargazers_count", 0) < 0]
        if bad:
            fail(f"Found repos with invalid star counts: {[r['name'] for r in bad]}")
        else:
            ok(f"All {len(repos)} repos have valid non-negative star counts")
    else:
        warn("Skipped — no repos fetched")

    print("\n  [4b] all full_name values follow 'user/repo' format")
    if repos:
        bad = [r["full_name"] for r in repos if "/" not in r.get("full_name", "")]
        if bad:
            fail(f"Invalid full_name format: {bad}")
        else:
            ok("All full_name values are in 'user/repo' format")
    else:
        warn("Skipped — no repos fetched")

    print("\n  [4c] topics field is always a list (not None)")
    if repos:
        bad = [r["full_name"] for r in repos if not isinstance(r.get("topics"), list)]
        if bad:
            fail(f"Repos with non-list topics: {bad}")
        else:
            ok("All repos have a list for 'topics'")
    else:
        warn("Skipped — no repos fetched")

    print(f"\n  [4d] profile created_at is not empty for '{USERNAME}'")
    if profile:
        if profile.get("created_at"):
            ok(f"created_at present: {profile['created_at']}")
        else:
            warn("created_at is empty — may be a private field for this user")
    else:
        warn("Skipped — no profile fetched")

    # ── Summary ────────────────────────────────────────────────────────────────
    section("Done")
    print(f"\n  All username-mode online tests completed.")
    print(f"  Check {RED}✗{RESET} lines above for any failures.")
    print(f"  Tested username: {USERNAME}\n")
