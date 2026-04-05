"""
End-to-end tests for Google Authentication.

Tests are split into two parts:
  1. Backend auth tests (no browser needed) — verify token enforcement
  2. Browser E2E tests (Playwright) — test the full Google OAuth flow
     NOTE: Google OAuth popup requires real Google credentials.
     Set TEST_GOOGLE_EMAIL and TEST_GOOGLE_PASSWORD env vars to run.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

import pytest
from playwright.async_api import async_playwright, expect

BASE_URL = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8765")

# ── Helpers ───────────────────────────────────────────────────────────────────

def get(path, token=None):
    url = BASE_URL + path
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def post(path, body=None, token=None):
    url = BASE_URL + path
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

# ── Part 1: Backend auth enforcement tests ────────────────────────────────────

class TestBackendAuth:

    def test_health_is_public(self):
        """Health endpoint should work without auth."""
        status, body = get("/health")
        assert status == 200
        assert body["status"] == "ok"
        assert body["firebase_enabled"] is True
        assert body["firebase_ready"] is True
        assert body["firebase_error"] is None
        print(f"  ✓ /health OK — firebase_ready={body['firebase_ready']}, ssh_available={body['ssh_available']}")

    def test_firebase_config_endpoint(self):
        """Firebase config should return the project ID (apiKey comes from static JS, not env)."""
        status, body = get("/api/firebase-config")
        assert status == 200
        assert body.get("projectId") == "sql-agent-5b660", f"Unexpected body: {body}"
        # apiKey and authDomain are served via static/firebase-config.js,
        # not via environment vars on the backend, so they may be empty here.
        print(f"  ✓ /api/firebase-config returned projectId={body['projectId']}")

    def test_connections_requires_auth(self):
        """GET /api/connections without token → 401."""
        status, body = get("/api/connections")
        assert status == 401, f"Expected 401, got {status}: {body}"
        assert "Missing Authorization" in body.get("detail", "")
        print("  ✓ /api/connections without token → 401 (Missing Authorization)")

    def test_bootstrap_requires_auth(self):
        """POST /api/bootstrap without token → 401."""
        status, body = post("/api/bootstrap")
        assert status == 401
        print("  ✓ /api/bootstrap without token → 401")

    def test_invalid_token_rejected(self):
        """A fake Bearer token → 401 with Invalid token."""
        status, body = get("/api/connections", token="fake-token-abc123")
        assert status == 401
        assert "Invalid token" in body.get("detail", "")
        print("  ✓ Fake Bearer token → 401 (Invalid token)")

    def test_schema_requires_auth(self):
        """GET /api/connections/{id}/schema without token → 401."""
        status, body = get("/api/connections/nonexistent/schema")
        assert status == 401
        print("  ✓ /api/connections/{id}/schema without token → 401")

    def test_index_html_served(self):
        """Root serves index.html with Firebase SDK scripts."""
        req = urllib.request.Request(BASE_URL + "/")
        with urllib.request.urlopen(req) as r:
            html = r.read().decode()
        assert "firebase-app-compat.js" in html
        assert "firebase-auth-compat.js" in html
        assert "signInGoogle" in html
        assert "auth-screen" in html
        print("  ✓ / serves index.html with Firebase SDK and auth UI")

    def test_firebase_config_js_served(self):
        """firebase-config.js is served and contains the project config."""
        req = urllib.request.Request(BASE_URL + "/firebase-config.js")
        with urllib.request.urlopen(req) as r:
            js = r.read().decode()
        assert "sql-agent-5b660" in js
        assert "APP_CONFIG" in js
        assert "apiKey" in js
        print("  ✓ /firebase-config.js served with correct projectId")

# ── Part 2: Browser E2E tests (Playwright) ────────────────────────────────────

GOOGLE_EMAIL    = os.environ.get("TEST_GOOGLE_EMAIL", "")
GOOGLE_PASSWORD = os.environ.get("TEST_GOOGLE_PASSWORD", "")
SKIP_BROWSER    = not (GOOGLE_EMAIL and GOOGLE_PASSWORD)
SKIP_REASON     = "Set TEST_GOOGLE_EMAIL and TEST_GOOGLE_PASSWORD to run browser E2E tests"

class TestFrontendAuthFlow:

    @pytest.mark.asyncio
    async def test_auth_screen_visible_on_load(self):
        """The auth screen should be visible when the page loads (Firebase configured)."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(BASE_URL)
            auth_screen = page.locator("#auth-screen")
            await expect(auth_screen).to_be_visible(timeout=8000)
            print("  ✓ Auth screen is visible on page load")
            await browser.close()

    @pytest.mark.asyncio
    async def test_google_button_present(self):
        """The 'Continue with Google' button should be visible."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(BASE_URL)
            btn = page.locator("button.btn-google")
            await expect(btn).to_be_visible(timeout=8000)
            text = await btn.inner_text()
            assert "Google" in text
            print(f"  ✓ Google sign-in button visible: '{text.strip()}'")
            await browser.close()

    @pytest.mark.asyncio
    async def test_email_fields_present(self):
        """Email and password fields should be present as fallback auth."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(BASE_URL)
            await expect(page.locator("#auth-email")).to_be_visible(timeout=8000)
            await expect(page.locator("#auth-password")).to_be_visible(timeout=8000)
            await expect(page.locator("#btn-email-signin")).to_be_visible(timeout=8000)
            print("  ✓ Email/password fields and sign-in button visible")
            await browser.close()

    @pytest.mark.asyncio
    async def test_empty_email_shows_error(self):
        """Submitting empty email/password shows an error."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(BASE_URL)
            # Wait for Firebase SDK to finish initializing (_fbAuth becomes non-null)
            await page.wait_for_function(
                "() => typeof firebase !== 'undefined' && firebase.apps && firebase.apps.length > 0",
                timeout=12000
            )
            await page.locator("#btn-email-signin").click()
            error = page.locator("#auth-error")
            await expect(error).to_be_visible(timeout=5000)
            text = await error.inner_text()
            assert text, "Error message should not be empty"
            print(f"  ✓ Empty submit shows error: '{text}'")
            await browser.close()

    @pytest.mark.asyncio
    async def test_wrong_password_shows_error(self):
        """Wrong credentials show a Firebase error in the auth error div."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(BASE_URL)
            # Wait for Firebase to initialize before interacting
            await page.wait_for_function(
                "() => typeof firebase !== 'undefined' && firebase.apps && firebase.apps.length > 0",
                timeout=12000
            )
            await page.fill("#auth-email", "notareal@example.com")
            await page.fill("#auth-password", "wrongpassword123")
            await page.click("#btn-email-signin")
            error = page.locator("#auth-error")
            await expect(error).to_be_visible(timeout=15000)
            text = await error.inner_text()
            assert text, "Should show error for wrong credentials"
            print(f"  ✓ Wrong credentials shows error: '{text[:80]}'")
            await browser.close()

    @pytest.mark.asyncio
    async def test_toggle_to_signup_mode(self):
        """Toggling auth mode switches button text to 'Create Account'."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(BASE_URL)
            await page.wait_for_function(
                "() => typeof firebase !== 'undefined' && firebase.apps && firebase.apps.length > 0",
                timeout=12000
            )
            btn = page.locator("#btn-email-signin")
            initial_text = await btn.inner_text()
            # Click the toggle link to switch to sign-up mode
            await page.locator("#auth-toggle span").click()
            # Wait for text to change
            await page.wait_for_function(
                "() => document.getElementById('btn-email-signin').textContent.includes('Create')",
                timeout=3000
            )
            new_text = await btn.inner_text()
            assert "Create" in new_text, f"Expected 'Create Account', got '{new_text}'"
            print(f"  ✓ Auth mode toggle: '{initial_text.strip()}' → '{new_text.strip()}'")
            await browser.close()

    @pytest.mark.asyncio
    @pytest.mark.skipif(SKIP_BROWSER, reason=SKIP_REASON)
    async def test_google_oauth_full_flow(self):
        """
        Full Google OAuth flow: click Google → OAuth popup → sign in → main UI loads.
        Requires TEST_GOOGLE_EMAIL and TEST_GOOGLE_PASSWORD env vars.
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,   # Must be headed for Google OAuth
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(BASE_URL)

            print(f"  → Clicking 'Continue with Google' for {GOOGLE_EMAIL}")
            async with context.expect_page() as popup_info:
                await page.click("button.btn-google")
            popup = await popup_info.value
            await popup.wait_for_load_state("domcontentloaded")

            # Fill in Google credentials
            print("  → Filling Google email...")
            await popup.fill('input[type="email"]', GOOGLE_EMAIL)
            await popup.click('#identifierNext')
            await popup.wait_for_selector('input[type="password"]', timeout=15000)

            print("  → Filling Google password...")
            await popup.fill('input[type="password"]', GOOGLE_PASSWORD)
            await popup.click('#passwordNext')

            # Wait for popup to close (OAuth complete) and main page to load
            await popup.wait_for_event("close", timeout=30000)
            print("  → OAuth popup closed, waiting for main app UI...")

            # Auth screen should disappear
            auth_screen = page.locator("#auth-screen")
            await expect(auth_screen).not_to_be_visible(timeout=15000)

            # User info should appear
            user_info = page.locator("#user-info")
            await expect(user_info).to_be_visible(timeout=10000)

            # Verify user's email is shown in UI
            user_name = await page.locator("#user-name").inner_text()
            print(f"  → Logged in as: {user_name}")

            # Wait for connections to load (sidebar)
            sidebar = page.locator(".conn-list")
            await expect(sidebar).to_be_visible(timeout=10000)

            # Verify API call was made with valid token — check connections loaded
            await page.wait_for_selector(".conn-item, .no-conn", timeout=10000)

            # Sign out
            await page.click("button.btn-signout")
            await expect(auth_screen).to_be_visible(timeout=8000)
            print("  ✓ Sign out successful — auth screen visible again")

            await browser.close()


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n{'='*60}")
    print("SQL Agent — Google Auth E2E Test Suite")
    print(f"Target: {BASE_URL}")
    print(f"{'='*60}\n")

    # Part 1: Backend tests (no credentials needed)
    print("── Part 1: Backend Auth Enforcement ──────────────────────\n")
    backend = TestBackendAuth()
    tests = [
        ("Health is public",              backend.test_health_is_public),
        ("Firebase config endpoint",      backend.test_firebase_config_endpoint),
        ("Connections require auth",       backend.test_connections_requires_auth),
        ("Bootstrap requires auth",       backend.test_bootstrap_requires_auth),
        ("Fake token rejected",           backend.test_invalid_token_rejected),
        ("Schema requires auth",          backend.test_schema_requires_auth),
        ("index.html served",             backend.test_index_html_served),
        ("firebase-config.js served",     backend.test_firebase_config_js_served),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failed += 1

    print(f"\n  Backend: {passed} passed, {failed} failed\n")

    # Part 2: Browser tests
    print("── Part 2: Browser / Frontend Auth Tests ─────────────────\n")
    browser_tests = [
        ("Auth screen visible on load",   TestFrontendAuthFlow().test_auth_screen_visible_on_load),
        ("Google button present",         TestFrontendAuthFlow().test_google_button_present),
        ("Email fields present",          TestFrontendAuthFlow().test_email_fields_present),
        ("Empty submit shows error",      TestFrontendAuthFlow().test_empty_email_shows_error),
        ("Wrong password shows error",    TestFrontendAuthFlow().test_wrong_password_shows_error),
        ("Toggle to signup mode",         TestFrontendAuthFlow().test_toggle_to_signup_mode),
    ]
    b_passed = b_failed = 0
    for name, fn in browser_tests:
        try:
            asyncio.run(fn())
            b_passed += 1
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            b_failed += 1

    # Google OAuth (requires credentials)
    if GOOGLE_EMAIL and GOOGLE_PASSWORD:
        print("\n── Part 3: Full Google OAuth Flow ────────────────────────\n")
        try:
            asyncio.run(TestFrontendAuthFlow().test_google_oauth_full_flow())
            print("  ✓ Full Google OAuth flow passed!")
            b_passed += 1
        except Exception as e:
            print(f"  ✗ Full Google OAuth flow: {e}")
            b_failed += 1
    else:
        print(f"\n  ⚠  Full Google OAuth test SKIPPED ({SKIP_REASON})")
        print("     Run with: TEST_GOOGLE_EMAIL=you@gmail.com TEST_GOOGLE_PASSWORD=xxx python3 test_google_auth_e2e.py")

    total_p = passed + b_passed
    total_f = failed + b_failed
    print(f"\n{'='*60}")
    print(f"Total: {total_p} passed, {total_f} failed")
    print(f"{'='*60}\n")
    sys.exit(0 if total_f == 0 else 1)
