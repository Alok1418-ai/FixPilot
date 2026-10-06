"""Authentication: the account store and the server's login gate.

These cover the hole that used to exist — ``GET /`` handed a working mutation
token to anyone who could reach the port, with no identity check at all.
"""

from __future__ import annotations

import json
import unittest

from fixpilot.api.server import ApiError, FixPilotServer, Request, SESSION_COOKIE
from fixpilot.config import AuthSettings
from fixpilot.core.agent import FixPilotAgent
from fixpilot.security.auth import AuthError, AuthStore, hash_password, validate_password

from .support import TempRepo


class PasswordPolicyTests(unittest.TestCase):
    def test_short_passwords_are_rejected(self) -> None:
        for password in ("", "a", "abcdefg"):
            with self.subTest(password=password):
                with self.assertRaises(AuthError):
                    validate_password(password)

    def test_repetitive_passwords_are_rejected(self) -> None:
        with self.assertRaises(AuthError):
            validate_password("aaaaaaaa")

    def test_reasonable_password_is_accepted(self) -> None:
        self.assertEqual(validate_password("  hunter2x  "), "  hunter2x  ")

    def test_hashing_is_salted_and_stable(self) -> None:
        salt = b"0123456789abcdef"
        first = hash_password("correct horse", salt)
        self.assertEqual(first, hash_password("correct horse", salt))
        self.assertNotEqual(first, hash_password("correct horse", b"fedcba9876543210"))
        self.assertNotEqual(first, hash_password("wrong horse", salt))


class AuthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.directory = self.repo.settings.auth_dir

    def tearDown(self) -> None:
        self.repo.cleanup()

    def store(self, **kwargs) -> AuthStore:
        return AuthStore(self.directory, **kwargs)

    def test_first_run_needs_setup_then_does_not(self) -> None:
        store = self.store()
        self.assertTrue(store.needs_setup())
        store.create_user("alok", "correct-horse-9")
        self.assertFalse(store.needs_setup())

    def test_first_account_becomes_owner(self) -> None:
        store = self.store()
        owner = store.create_user("alok", "correct-horse-9")
        member = store.create_user("teammate", "another-secret-1")
        self.assertEqual(owner.role, "owner")
        self.assertEqual(member.role, "member")

    def test_wrong_password_is_rejected(self) -> None:
        store = self.store()
        store.create_user("alok", "correct-horse-9")
        self.assertIsNotNone(store.authenticate("alok", "correct-horse-9"))
        self.assertIsNone(store.authenticate("alok", "correct-horse-8"))
        self.assertIsNone(store.authenticate("nobody", "correct-horse-9"))

    def test_duplicate_username_is_rejected(self) -> None:
        store = self.store()
        store.create_user("alok", "correct-horse-9")
        with self.assertRaises(AuthError):
            store.create_user("ALOK", "different-secret-1")

    def test_password_never_lands_on_disk_in_plaintext(self) -> None:
        store = self.store()
        store.create_user("alok", "correct-horse-9")
        raw = (self.directory / "users.json").read_text(encoding="utf-8")
        self.assertNotIn("correct-horse-9", raw)
        self.assertIn("password_hash", raw)

    def test_sessions_survive_a_new_store_instance(self) -> None:
        store = self.store()
        user = store.create_user("alok", "correct-horse-9")
        token = store.create_session(user)
        reopened = self.store()
        self.assertIsNotNone(reopened.session_user(token))
        self.assertEqual(reopened.session_user(token).username, "alok")

    def test_revoked_and_unknown_tokens_are_rejected(self) -> None:
        store = self.store()
        user = store.create_user("alok", "correct-horse-9")
        token = store.create_session(user)
        self.assertTrue(store.revoke(token))
        self.assertIsNone(store.session_user(token))
        self.assertIsNone(store.session_user("not-a-real-token"))
        self.assertIsNone(store.session_user(""))

    def test_expired_sessions_are_rejected(self) -> None:
        store = self.store(session_hours=1)
        user = store.create_user("alok", "correct-horse-9")
        token = store.create_session(user)
        store._sessions[token]["expires_at"] = 1.0  # long past
        self.assertIsNone(store.session_user(token))

    def test_changing_a_password_kills_live_sessions(self) -> None:
        store = self.store()
        user = store.create_user("alok", "correct-horse-9")
        token = store.create_session(user)
        self.assertTrue(store.set_password("alok", "rotated-secret-2"))
        self.assertIsNone(store.session_user(token))
        self.assertIsNotNone(store.authenticate("alok", "rotated-secret-2"))

    def test_public_payload_leaks_no_secret_material(self) -> None:
        store = self.store()
        user = store.create_user("alok", "correct-horse-9")
        public = user.public()
        self.assertNotIn("password_hash", public)
        self.assertNotIn("salt", public)
        self.assertEqual(public["username"], "alok")


class ServerAuthGateTests(unittest.TestCase):
    """Drives ``FixPilotServer.dispatch`` directly — no socket needed."""

    def setUp(self) -> None:
        self.repo = TempRepo()
        self.settings = self.repo.settings
        self.app = FixPilotServer(FixPilotAgent(self.settings), self.settings)
        self.assertTrue(self.app.auth_enabled)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def call(self, method: str, path: str, *, body=None, token: str = "", cookie=None):
        request = Request(method=method, path=path, body=body or {}, token=token, cookie=cookie or {})
        return self.app.dispatch(request)

    # -- the regression that matters ------------------------------------
    def test_api_is_closed_before_login(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.call("GET", "/api/overview")
        self.assertEqual(caught.exception.status, 401)

    def test_mutations_are_closed_before_login(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.call("POST", "/api/sessions", body={"text": "boom"}, token=self.app.token)
        self.assertEqual(caught.exception.status, 401)

    def test_public_endpoints_stay_reachable(self) -> None:
        self.assertEqual(self.call("GET", "/api/health").status, 200)
        status = json.loads(_json(self.call("GET", "/api/auth/status")))
        self.assertTrue(status["needs_setup"])
        self.assertFalse(status["signed_in"])

    # -- static gating ---------------------------------------------------
    def test_root_serves_the_login_shell_when_signed_out(self) -> None:
        response = self.call("GET", "/")
        body = response.raw.decode("utf-8")
        self.assertIn("Set up FixPilot", body)

    def test_login_shell_never_contains_the_mutation_token(self) -> None:
        body = self.call("GET", "/").raw.decode("utf-8")
        self.assertNotIn(self.app.token, body)
        self.assertNotIn("__FIXPILOT_TOKEN__", body)

    def test_unknown_paths_cannot_smuggle_the_app_when_signed_out(self) -> None:
        body = self.call("GET", "/nope").raw.decode("utf-8")
        self.assertIn("Set up FixPilot", body)
        self.assertNotIn(self.app.token, body)

    # -- setup, login, session ------------------------------------------
    def setup_owner(self) -> str:
        response = self.call(
            "POST", "/api/auth/setup",
            body={"username": "alok", "password": "correct-horse-9", "display_name": "Alok"},
        )
        self.assertEqual(response.status, 201)
        return response.set_cookie.split(f"{SESSION_COOKIE}=", 1)[1].split(";", 1)[0]

    def test_setup_creates_the_owner_and_returns_a_session_cookie(self) -> None:
        response = self.call(
            "POST", "/api/auth/setup",
            body={"username": "alok", "password": "correct-horse-9", "display_name": "Alok"},
        )
        self.assertEqual(response.status, 201)
        cookie = response.set_cookie
        self.assertTrue(cookie.startswith(f"{SESSION_COOKIE}="))
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie, cookie)
        self.assertEqual(json.loads(_json(response))["user"]["role"], "owner")
        self.assertFalse(self.app.auth.needs_setup())

    def test_setup_can_only_run_once(self) -> None:
        self.setup_owner()
        with self.assertRaises(ApiError) as caught:
            self.call("POST", "/api/auth/setup", body={"username": "intruder", "password": "another-secret-1"})
        self.assertEqual(caught.exception.status, 409)

    def test_wrong_password_does_not_sign_in(self) -> None:
        self.setup_owner()
        with self.assertRaises(ApiError) as caught:
            self.call("POST", "/api/auth/login", body={"username": "alok", "password": "wrong-horse-9"})
        self.assertEqual(caught.exception.status, 401)

    def test_login_opens_the_api_and_serves_the_real_app(self) -> None:
        token = self.setup_owner()
        cookie = {SESSION_COOKIE: token}
        overview = self.call("GET", "/api/overview", cookie=cookie)
        self.assertEqual(overview.status, 200)
        page = self.call("GET", "/", cookie=cookie).raw.decode("utf-8")
        self.assertIn(self.app.token, page)          # the app gets its mutation token
        self.assertNotIn("Set up FixPilot", page)

    def test_dashboard_requires_a_session(self) -> None:
        with self.assertRaises(ApiError):
            self.call("GET", "/api/dashboard")
        token = self.setup_owner()
        payload = json.loads(_json(self.call("GET", "/api/dashboard", cookie={SESSION_COOKIE: token})))
        self.assertIn("sessions", payload)
        self.assertIn("safety", payload)
        self.assertIn("codebase", payload)

    def test_signup_is_disabled_by_default(self) -> None:
        self.setup_owner()
        with self.assertRaises(ApiError) as caught:
            self.call("POST", "/api/auth/signup", body={"username": "stranger", "password": "another-secret-1"})
        self.assertEqual(caught.exception.status, 403)

    def test_signup_works_when_explicitly_enabled(self) -> None:
        self.setup_owner()
        self.app.settings.auth = AuthSettings(enabled=True, allow_signup=True)
        response = self.call("POST", "/api/auth/signup", body={"username": "teammate", "password": "another-secret-1"})
        self.assertEqual(response.status, 201)
        self.assertEqual(len(self.app.auth.list_users()), 2)

    def test_logout_revokes_the_session(self) -> None:
        token = self.setup_owner()
        cookie = {SESSION_COOKIE: token}
        self.assertEqual(self.call("POST", "/api/auth/logout", cookie=cookie).status, 200)
        with self.assertRaises(ApiError):
            self.call("GET", "/api/overview", cookie=cookie)

    def test_me_reports_the_signed_in_user(self) -> None:
        token = self.setup_owner()
        payload = json.loads(_json(self.call("GET", "/api/auth/me", cookie={SESSION_COOKIE: token})))
        self.assertTrue(payload["signed_in"])
        self.assertEqual(payload["user"]["username"], "alok")
        self.assertEqual(payload["user"]["role"], "owner")


class AuthDisabledTests(unittest.TestCase):
    """`--no-auth` / FIXPILOT_AUTH=false keeps the old trusted-local behaviour."""

    def setUp(self) -> None:
        self.repo = TempRepo()
        self.settings = self.repo.settings
        self.app = FixPilotServer(FixPilotAgent(self.settings), self.settings, require_token=False)
        self.assertFalse(self.app.auth_enabled)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_api_is_open_and_app_is_served(self) -> None:
        response = self.app.dispatch(Request(method="GET", path="/api/overview"))
        self.assertEqual(response.status, 200)
        page = self.app.dispatch(Request(method="GET", path="/")).raw.decode("utf-8")
        self.assertIn(self.app.token, page)


def _json(response) -> str:
    """Serialise a Response payload the way the handler would."""
    if response.raw is not None:
        return response.raw.decode("utf-8")
    return json.dumps(response.payload, ensure_ascii=False, default=str)


if __name__ == "__main__":
    unittest.main()
