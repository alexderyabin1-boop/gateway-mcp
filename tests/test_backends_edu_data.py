import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from support import install_dependency_stubs

install_dependency_stubs()

from gateway_mcp.backends import edu_data
from gateway_mcp.backends.common import BackendConfigError, BackendRouteError
from gateway_mcp.backends.edu_data import OPERATIONS, _call_edu_data
from gateway_mcp.backends.router import BACKEND_DISPATCH
from gateway_mcp.services.access_packages import access_package_catalog
from gateway_mcp.services.auth import DEFAULT_SUPPORTED_SCOPES
from gateway_mcp.services.policy import GatewayActor


TOKEN = "edu-data-service-token-0123456789abcdef"
BASE_URL = "https://data.example.test"
REPO_ROOT = Path(__file__).resolve().parents[1]
STUDENT_ID = "5bd7b6e0-2813-4341-9b04-285e727f8e18"
MANAGER = GatewayActor(subject="yandex:3", email="Manager@Example.test", scopes=("tools:call", "edu_profile:read"))


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self):
        if self.payload is None:
            raise ValueError("no json")
        return self.payload


class FakeAsyncClient:
    calls: list = []
    response = FakeResponse({})

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def request(self, method, url, **kwargs):
        FakeAsyncClient.calls.append((method, url, kwargs))
        return FakeAsyncClient.response


def _route(operation, scope="edu_profile:read"):
    return {"name": f"edu.{operation}", "backend": "edu_data", "transport": "edu-data-rest",
            "operation": operation, "scope": scope, "status": "implemented"}


class EduDataBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeAsyncClient.calls = []
        FakeAsyncClient.response = FakeResponse({"students": []})
        self.actor = MANAGER
        for p in (
            patch.dict(os.environ, {"EDU_DATA_URL": BASE_URL + "/", "EDU_DATA_SERVICE_TOKEN": TOKEN}),
            patch("gateway_mcp.backends.edu_data.httpx.AsyncClient", FakeAsyncClient),
            patch("gateway_mcp.backends.edu_data._actor", side_effect=lambda: self.actor),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def test_find_forwards_actor_and_token_in_headers_only(self) -> None:
        FakeAsyncClient.response = FakeResponse({"students": [{"id": STUDENT_ID, "name": "Белов Никита"}]})
        result = await _call_edu_data(_route("students.find"), {"query": "Белов"})
        self.assertEqual(result["data"]["students"][0]["id"], STUDENT_ID)
        method, url, kwargs = FakeAsyncClient.calls[0]
        self.assertEqual((method, url), ("GET", BASE_URL + "/api/students/find"))
        self.assertEqual(kwargs["params"], {"q": "Белов"})
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(kwargs["headers"]["X-Acting-User"], "Manager@Example.test")
        self.assertNotIn(TOKEN, json.dumps(result, ensure_ascii=False))

    async def test_profile_path_and_discipline(self) -> None:
        FakeAsyncClient.response = FakeResponse({"student": {"id": STUDENT_ID}, "subjects": []})
        await _call_edu_data(_route("student.profile"), {"student_id": STUDENT_ID.upper(), "discipline": "Математика"})
        _, url, kwargs = FakeAsyncClient.calls[0]
        self.assertEqual(url, f"{BASE_URL}/api/students/{STUDENT_ID}/profile")
        self.assertEqual(kwargs["params"], {"discipline": "Математика"})

    async def test_arguments_are_validated(self) -> None:
        for args in ({"query": "Бе"}, {"query": "x" * 65}, {"query": "Белов", "path": "/admin"}):
            with self.assertRaises(BackendRouteError):
                await _call_edu_data(_route("students.find"), args)
        for args in ({"student_id": "../../staff"}, {"student_id": "123"}, {"student_id": STUDENT_ID, "x": 1},
                     {"student_id": STUDENT_ID, "discipline": "м" * 65}):
            with self.assertRaises(BackendRouteError):
                await _call_edu_data(_route("student.profile"), args)
        self.assertEqual(FakeAsyncClient.calls, [])

    async def test_denials_and_errors(self) -> None:
        FakeAsyncClient.response = FakeResponse({"error": "forbidden"}, 403)
        with self.assertRaises(PermissionError):
            await _call_edu_data(_route("student.profile"), {"student_id": STUDENT_ID})
        FakeAsyncClient.response = FakeResponse({"error": "not_found"}, 404)
        result = await _call_edu_data(_route("student.profile"), {"student_id": STUDENT_ID})
        self.assertEqual(result["data"], {"error": "not_found"})
        FakeAsyncClient.response = FakeResponse(None, 502)
        with self.assertRaises(BackendRouteError) as ctx:
            await _call_edu_data(_route("students.find"), {"query": "Белов"})
        self.assertNotIn(TOKEN, str(ctx.exception))

    async def test_actor_without_email_is_denied(self) -> None:
        self.actor = GatewayActor(subject="yandex:9", scopes=("edu_profile:read",))
        with self.assertRaises(PermissionError):
            await _call_edu_data(_route("students.find"), {"query": "Белов"})
        self.assertEqual(FakeAsyncClient.calls, [])

    async def test_configuration_is_checked(self) -> None:
        with patch.dict(os.environ, {"EDU_DATA_URL": "http://data.example.test"}):
            with self.assertRaises(BackendConfigError):
                await _call_edu_data(_route("students.find"), {"query": "Белов"})
        with patch.dict(os.environ, {"EDU_DATA_SERVICE_TOKEN": "short"}):
            with self.assertRaises(BackendConfigError):
                await _call_edu_data(_route("students.find"), {"query": "Белов"})

    async def test_route_with_wrong_scope_or_operation_is_refused(self) -> None:
        with self.assertRaises(BackendRouteError):
            await _call_edu_data(_route("students.find", scope="tools:call"), {"query": "Белов"})
        with self.assertRaises(BackendRouteError):
            await _call_edu_data(_route("staff.list"), {})

    def test_registry_routes_scopes_and_package(self) -> None:
        self.assertIn("edu-data-rest", BACKEND_DISPATCH)
        tools = json.loads((REPO_ROOT / "gateway-tools.json").read_text(encoding="utf-8"))["tools"]
        routes = [t for t in tools if t.get("backend") == "edu_data"]
        self.assertEqual({r["operation"] for r in routes}, set(OPERATIONS))
        for r in routes:
            self.assertEqual(r["transport"], "edu-data-rest")
            self.assertEqual(r["scope"], OPERATIONS[r["operation"]][1])
            self.assertNotIn("http_method", r)
        self.assertIn("edu_profile:read", DEFAULT_SUPPORTED_SCOPES)
        package = next(p for p in access_package_catalog() if p["key"] == "student-profile-reader")
        self.assertEqual(set(package["scopes"]), {"tools:call", "edu_profile:read"})
        self.assertFalse(any(s.endswith(":write") for s in package["scopes"]))


if __name__ == "__main__":
    unittest.main()
