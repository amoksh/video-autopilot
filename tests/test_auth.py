import os
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import server


class AuthenticationTests(unittest.TestCase):
    env_names = (
        "VA_ACCESS_PASSWORD",
        "VA_SESSION_SECRET",
        "VA_LOCAL_AUTH_BYPASS",
        "VA_ENV",
        "ENVIRONMENT",
        "RENDER",
        "CODESPACES",
    )

    def setUp(self):
        self.saved_env = {name: os.environ.get(name) for name in self.env_names}
        for name in self.env_names:
            os.environ.pop(name, None)
        self.test_password = secrets.token_urlsafe(24)
        os.environ["VA_ACCESS_PASSWORD"] = self.test_password
        os.environ["VA_SESSION_SECRET"] = secrets.token_urlsafe(48)

    def tearDown(self):
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_login_cookie_protects_routes_and_logout_clears_session(self):
        with TestClient(
            server.app, base_url="https://testserver", follow_redirects=False,
        ) as client:
            protected = client.get("/", headers={"accept": "text/html"})
            self.assertEqual(protected.status_code, 303)
            self.assertTrue(protected.headers["location"].startswith("/login?"))
            self.assertIn("no-store", protected.headers["cache-control"])

            page = client.get("/login")
            self.assertEqual(page.status_code, 200)
            self.assertNotIn("stylesheet", page.text)
            self.assertIn("name=\"password\"", page.text)

            login_headers = {"origin": "https://testserver"}
            invalid = client.post(
                "/login",
                data={"password": "wrong-password"},
                headers=login_headers,
            )
            self.assertEqual(invalid.status_code, 401)
            self.assertNotIn("set-cookie", invalid.headers)

            logged_in = client.post(
                "/login",
                data={"password": self.test_password},
                headers=login_headers,
            )
            self.assertEqual(logged_in.status_code, 303)
            cookie = logged_in.headers["set-cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("Secure", cookie)
            self.assertIn("SameSite=lax", cookie)
            self.assertIn("Max-Age=43200", cookie)

            projects = client.get("/api/studio/projects")
            self.assertEqual(projects.status_code, 200)
            self.assertIn("no-store", projects.headers["cache-control"])
            index = client.get("/", headers={"accept": "text/html"})
            self.assertEqual(index.status_code, 200)

            logout = client.post("/logout", headers=login_headers)
            self.assertEqual(logout.status_code, 303)
            self.assertIn("Max-Age=0", logout.headers["set-cookie"])
            self.assertEqual(client.get("/api/studio/projects").status_code, 401)

    def test_api_download_and_static_assets_are_protected(self):
        with TestClient(server.app, follow_redirects=False) as client:
            for method, path in (
                ("GET", "/api/channels"),
                ("GET", "/api/studio/projects"),
                ("POST", "/api/studio/projects/private/media"),
                ("GET", "/api/studio/media/private/media-id"),
                ("GET", "/api/studio/media/private/media-id/thumbnail"),
                ("POST", "/api/studio/projects/private/render"),
                ("GET", "/api/download/private/video"),
            ):
                with self.subTest(method=method, path=path):
                    response = client.request(method, path)
                    self.assertEqual(response.status_code, 401)
                    self.assertIn("Authentication required", response.text)

            static = client.get("/app.js")
            self.assertEqual(static.status_code, 401)
            self.assertNotIn("const $", static.text)

    def test_invalid_or_tampered_session_cookie_is_rejected(self):
        with TestClient(server.app) as client:
            client.cookies.set(server.AUTH_COOKIE_NAME, "invalid.signature")
            response = client.get("/api/channels")
            self.assertEqual(response.status_code, 401)

    def test_expired_session_cookie_is_rejected(self):
        with TestClient(server.app, follow_redirects=False) as client:
            client.post(
                "/login",
                data={"password": self.test_password},
                headers={"origin": "http://testserver"},
            )
            future = time.time() + server.AUTH_SESSION_MAX_AGE + 1
            with patch.object(server, "time", SimpleNamespace(time=lambda: future)):
                response = client.get("/api/channels")
            self.assertEqual(response.status_code, 401)

    def test_state_changes_require_same_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(server, "STUDIO_PROJECTS", Path(temporary)):
                with TestClient(server.app, follow_redirects=False) as client:
                    client.post(
                        "/login",
                        data={"password": self.test_password},
                        headers={"origin": "http://testserver"},
                    )
                    cross_origin = client.post(
                        "/api/studio/projects",
                        json={"name": "Blocked"},
                        headers={"origin": "https://attacker.example"},
                    )
                    self.assertEqual(cross_origin.status_code, 403)
                    self.assertEqual(list(Path(temporary).iterdir()), [])

                    same_origin = client.post(
                        "/api/studio/projects",
                        json={"name": "Allowed"},
                        headers={"origin": "http://testserver"},
                    )
                    self.assertEqual(same_origin.status_code, 200)
                    self.assertEqual(len(list(Path(temporary).iterdir())), 1)

    def test_https_forwarded_login_uses_public_origin_and_secure_cookie(self):
        for proxy_env in ("CODESPACES", "RENDER"):
            with self.subTest(proxy=proxy_env):
                os.environ[proxy_env] = "true"
                with TestClient(
                    server.app,
                    base_url="http://testserver:8000",
                    follow_redirects=False,
                ) as client:
                    response = client.post(
                        "/login",
                        data={"password": self.test_password},
                        headers={
                            "origin": "https://testserver",
                            "x-forwarded-proto": "https",
                        },
                    )
                self.assertEqual(response.status_code, 303)
                self.assertIn("Secure", response.headers["set-cookie"])
                os.environ.pop(proxy_env, None)

    def test_login_rejects_mismatched_origin_behind_https_proxy(self):
        os.environ["CODESPACES"] = "true"
        with TestClient(
            server.app,
            base_url="http://testserver:8000",
            follow_redirects=False,
        ) as client:
            response = client.post(
                "/login",
                data={"password": self.test_password},
                headers={
                    "origin": "https://attacker.example",
                    "x-forwarded-proto": "https",
                },
            )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("set-cookie", response.headers)

    def test_forwarded_headers_cannot_spoof_trusted_proxy_origin(self):
        with TestClient(
            server.app,
            base_url="http://testserver:8000",
            follow_redirects=False,
        ) as client:
            response = client.post(
                "/login",
                data={"password": self.test_password},
                headers={
                    "origin": "https://testserver",
                    "x-forwarded-proto": "https",
                    "x-forwarded-host": "testserver",
                    "x-forwarded-port": "443",
                },
            )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("set-cookie", response.headers)

    def test_production_fails_closed_when_configuration_is_missing(self):
        os.environ.pop("VA_ACCESS_PASSWORD", None)
        os.environ.pop("VA_SESSION_SECRET", None)
        os.environ["RENDER"] = "true"
        os.environ["VA_LOCAL_AUTH_BYPASS"] = "1"
        with TestClient(server.app) as client:
            self.assertEqual(client.get("/api/studio/projects").status_code, 503)
            self.assertEqual(client.get("/login").status_code, 503)

        os.environ.pop("VA_LOCAL_AUTH_BYPASS", None)
        os.environ["VA_ACCESS_PASSWORD"] = secrets.token_urlsafe(24)
        with TestClient(server.app) as client:
            self.assertEqual(client.get("/api/studio/projects").status_code, 503)


if __name__ == "__main__":
    unittest.main()
