import unittest
from unittest.mock import patch

from support import install_dependency_stubs

install_dependency_stubs()

from gateway_mcp.backends.hollihope import ALL_STUDENTS_SCOPE, OPERATIONS
from gateway_mcp.services.access_admin import admin_grant_access_package
from gateway_mcp.services.access_packages import access_package
from gateway_mcp.services.auth import DEFAULT_SUPPORTED_SCOPES
from gateway_mcp.services.policy import GatewayActor, has_scope


MANAGER_KEY = "progress-monitoring-manager"
LEAD_KEY = "progress-monitoring-lead"
ROUTE_SCOPES = {scope for _handler, scope in OPERATIONS.values()}


class ProgressMonitoringPackageTests(unittest.TestCase):
    def test_manager_package_is_read_only_and_limited_to_own_students(self) -> None:
        package = access_package(MANAGER_KEY)

        self.assertEqual(
            sorted(package["scopes"]),
            sorted(["tools:call", "hollihope_students:read", "hollihope_study:read", "hollihope_finance:read"]),
        )
        self.assertNotIn(ALL_STUDENTS_SCOPE, package["scopes"])

    def test_lead_package_adds_only_the_all_students_scope(self) -> None:
        manager = set(access_package(MANAGER_KEY)["scopes"])
        lead = set(access_package(LEAD_KEY)["scopes"])

        self.assertEqual(lead - manager, {ALL_STUDENTS_SCOPE})
        self.assertEqual(manager - lead, set())

    def test_packages_cover_every_hollihope_route_and_nothing_writable(self) -> None:
        for key in (MANAGER_KEY, LEAD_KEY):
            with self.subTest(package=key):
                package = access_package(key)
                scopes = set(package["scopes"])
                self.assertTrue(ROUTE_SCOPES <= scopes)
                self.assertIn("tools:call", scopes)
                self.assertNotIn("*", scopes)
                for scope in scopes:
                    self.assertIn(scope, DEFAULT_SUPPORTED_SCOPES)
                    self.assertFalse(scope.endswith((":write", ":admin", ":send", ":exec")), scope)
                self.assertEqual(int(package["version"]), 1)
                for resource in package["resources"]:
                    self.assertTrue(resource["system"].startswith("hollihope_"))
                    self.assertEqual(resource["actions"], ["read"])
                    self.assertEqual(resource["effect"], "allow")

    def test_scopes_translate_to_the_expected_visibility(self) -> None:
        manager = GatewayActor(subject="yandex:1", scopes=tuple(access_package(MANAGER_KEY)["scopes"]))
        lead = GatewayActor(subject="yandex:2", scopes=tuple(access_package(LEAD_KEY)["scopes"]))

        self.assertFalse(has_scope(manager, ALL_STUDENTS_SCOPE))
        self.assertTrue(has_scope(lead, ALL_STUDENTS_SCOPE))
        for scope in ROUTE_SCOPES:
            self.assertTrue(has_scope(manager, scope))
            self.assertTrue(has_scope(lead, scope))

    def test_grant_without_ttl_is_supported_and_dry_run_writes_nothing(self) -> None:
        admin = GatewayActor(subject="yandex:owner", scopes=("access:admin",))
        with patch("gateway_mcp.services.access_admin.insert_access_bundle") as insert:
            result = admin_grant_access_package(
                subject_type="user",
                subject_key="Manager@Yandex.ru",
                package_key=MANAGER_KEY,
                reason="Pilot",
                actor=admin,
                ttl_days=0,
                dry_run=True,
            )

        self.assertTrue(result["dry_run"])
        self.assertIsNone(result["package"]["ttl_days"])
        self.assertEqual(result["package"]["subject_key"], "manager@yandex.ru")
        self.assertEqual(result["package"]["package_key"], MANAGER_KEY)
        insert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
