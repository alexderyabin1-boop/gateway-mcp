import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import install_dependency_stubs

install_dependency_stubs()

from gateway_mcp.backends import hollihope
from gateway_mcp.backends.common import BackendConfigError, BackendRouteError
from gateway_mcp.backends.hollihope import OPERATIONS, _call_hollihope
from gateway_mcp.backends.router import BACKEND_DISPATCH
from gateway_mcp.services.auth import DEFAULT_SUPPORTED_SCOPES
from gateway_mcp.services.policy import GatewayActor


API_KEY = "test-hollihope-key-0123456789"
BASE_URL = "https://school.example.test/Api/V2"
REPO_ROOT = Path(__file__).resolve().parents[1]

STUDENT_1 = {
    "ClientId": 101,
    "Id": 9001,
    "FirstName": "Anna",
    "LastName": "Sample",
    "MiddleName": "Hidden",
    "Birthday": "2010-01-02",
    "Mobile": "+70000000001",
    "EMail": "anna@example.test",
    "Address": "Hidden street 1",
    "Status": "Active",
    "LearningTypes": ["Group"],
    "Disciplines": [{"Discipline": "Math", "Level": "OGE"}],
    "OfficesAndCompanies": [{"Id": 1, "Name": "Main"}],
    "Assignees": [{"Id": 15, "FullName": "Manager One"}],
    "Agents": [
        {
            "FirstName": "Irina",
            "MiddleName": "Petrovna",
            "LastName": "HiddenParent",
            "WhoIs": "Mother",
            "Mobile": "+70000000002",
            "EMail": "parent@example.test",
            "IsCustomer": True,
        }
    ],
    "ExtraFields": [{"Name": "Contract", "Value": "C-1"}],
}
STUDENT_2 = {
    "ClientId": 202,
    "FirstName": "Boris",
    "LastName": "Other",
    "Assignees": [{"Id": 77, "FullName": "Manager Two"}],
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.text = ""

    def json(self):
        if self.payload is None:
            raise ValueError("no json")
        return self.payload


class FakeAsyncClient:
    calls: list = []
    handlers: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def request(self, method, url, **kwargs):
        FakeAsyncClient.calls.append((method, url, kwargs))
        function = url.rsplit("/", 1)[1]
        handler = FakeAsyncClient.handlers.get(function)
        if handler is None:
            return FakeResponse({"Error": "unexpected function"}, 400)
        result = handler(kwargs.get("json") or {})
        if isinstance(result, FakeResponse):
            return result
        return FakeResponse(result)


def _students_handler(body):
    if "clientId" in body:
        return {"Students": [s for s in (STUDENT_1, STUDENT_2) if s["ClientId"] == body["clientId"]]}
    return {"Students": [STUDENT_1, STUDENT_2]}


def _route(operation, scope=None, name=None):
    return {
        "name": name or f"hollihope.{operation}",
        "backend": "hollihope",
        "transport": "hollihope-rest",
        "operation": operation,
        "scope": scope or OPERATIONS[operation][1],
        "status": "implemented",
    }


OWNER = GatewayActor(subject="yandex:1", email="owner@example.test", login="owner", scopes=("*",))
HEAD = GatewayActor(
    subject="yandex:2",
    email="head@example.test",
    scopes=("hollihope_students:read", "hollihope_students:all", "hollihope_study:read"),
)
MANAGER = GatewayActor(
    subject="yandex:3",
    email="Manager@Example.test",
    login="manager",
    scopes=("hollihope_students:read", "hollihope_study:read", "hollihope_finance:read"),
)


class HollihopeBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeAsyncClient.calls = []
        FakeAsyncClient.handlers = {"GetStudents": _students_handler}
        hollihope._assignee_cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.map_path = os.path.join(self._tmp.name, "hollihope-users.json")
        with open(self.map_path, "w", encoding="utf-8") as handle:
            json.dump({"users": {"manager@example.test": 15}}, handle)
        env = patch.dict(
            os.environ,
            {
                "HOLLIHOPE_BASE_URL": BASE_URL,
                "HOLLIHOPE_API_KEY": API_KEY,
                "HOLLIHOPE_USER_MAP_FILE": self.map_path,
            },
        )
        env.start()
        self.addCleanup(env.stop)
        client = patch("gateway_mcp.backends.hollihope.httpx.AsyncClient", FakeAsyncClient)
        client.start()
        self.addCleanup(client.stop)
        self.actor = OWNER
        actor = patch("gateway_mcp.backends.hollihope._actor", side_effect=lambda: self.actor)
        actor.start()
        self.addCleanup(actor.stop)

    def _functions_called(self):
        return [url.rsplit("/", 1)[1] for _, url, _ in FakeAsyncClient.calls]

    # --- secrets and transport ---------------------------------------------

    async def test_key_travels_in_post_body_and_is_never_returned(self) -> None:
        result = await _call_hollihope(_route("students.find"), {"term": "Sample"})

        method, url, kwargs = FakeAsyncClient.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{BASE_URL}/GetStudents")
        self.assertNotIn(API_KEY, url)
        self.assertNotIn("params", kwargs)
        self.assertEqual(kwargs["json"]["authkey"], API_KEY)
        self.assertEqual(kwargs["json"]["take"], 10)
        self.assertNotIn(API_KEY, json.dumps(result))

    async def test_upstream_error_does_not_leak_key(self) -> None:
        FakeAsyncClient.handlers["GetStudents"] = lambda body: FakeResponse({"Error": "Bad argument."}, 400)

        with self.assertRaises(BackendRouteError) as ctx:
            await _call_hollihope(_route("students.find"), {"term": "Sample"})

        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("Bad argument.", str(ctx.exception))
        self.assertNotIn(API_KEY, str(ctx.exception))

    async def test_non_json_upstream_response_is_an_error(self) -> None:
        FakeAsyncClient.handlers["GetStudents"] = lambda body: FakeResponse(None, 200)

        with self.assertRaises(BackendRouteError):
            await _call_hollihope(_route("students.find"), {"term": "Sample"})

    async def test_missing_configuration_is_reported(self) -> None:
        with patch.dict(os.environ, {"HOLLIHOPE_API_KEY": ""}):
            with self.assertRaises(BackendConfigError):
                await _call_hollihope(_route("students.find"), {"term": "Sample"})
        with patch.dict(os.environ, {"HOLLIHOPE_BASE_URL": "http://school.example.test/Api/V2"}):
            with self.assertRaises(BackendConfigError):
                await _call_hollihope(_route("students.find"), {"term": "Sample"})

    # --- field allowlist ----------------------------------------------------

    async def test_student_projection_drops_contacts_and_parent_surname(self) -> None:
        result = await _call_hollihope(_route("students.find"), {"student_client_id": 101})

        student = result["data"]["students"][0]
        self.assertEqual(student["FirstName"], "Anna")
        self.assertEqual(student["LastName"], "Sample")
        self.assertEqual(student["Assignees"], [{"Id": 15, "FullName": "Manager One"}])
        self.assertEqual(
            student["Agents"],
            [{"FirstName": "Irina", "MiddleName": "Petrovna", "WhoIs": "Mother", "IsCustomer": True}],
        )
        self.assertEqual(student["OfficesAndCompanies"], [{"Name": "Main"}])
        dumped = json.dumps(result)
        for hidden in ("Hidden", "+7000", "example.test", "2010-01-02", "Contract", "C-1", "9001"):
            self.assertNotIn(hidden, dumped)

    async def test_enrollments_drop_contacts_and_use_fixed_upstream_arguments(self) -> None:
        FakeAsyncClient.handlers["GetEdUnitStudents"] = lambda body: {
            "EdUnitStudents": [
                {
                    "EdUnitId": 5,
                    "EdUnitName": "Math-9",
                    "EdUnitDiscipline": "Math",
                    "StudentClientId": 101,
                    "StudentName": "Sample Anna",
                    "StudentMobile": "+70000000001",
                    "StudentAgents": [{"FirstName": "Irina", "Mobile": "+70000000002"}],
                    "StudentExtraFields": [{"Name": "Contract", "Value": "C-1"}],
                    "Days": [{"Date": "2026-10-01", "Pass": True, "StudentPayableMinutes": 90.0}],
                }
            ]
        }

        result = await _call_hollihope(
            _route("study.enrollments"),
            {"student_client_id": 101, "date_from": "2026-09-01", "date_to": "2026-10-05"},
        )

        body = FakeAsyncClient.calls[-1][2]["json"]
        self.assertEqual(body["studentClientId"], 101)
        self.assertEqual(body["dateFrom"], "2026-09-01")
        self.assertEqual(body["dateTo"], "2026-10-05")
        self.assertIs(body["queryDays"], True)
        self.assertNotIn("queryPayers", body)
        enrollment = result["data"]["enrollments"][0]
        self.assertEqual(enrollment["Days"], [{"Date": "2026-10-01", "Pass": True}])
        dumped = json.dumps(result)
        for hidden in ("+7000", "Irina", "Contract", "Sample Anna", "StudentPayableMinutes"):
            self.assertNotIn(hidden, dumped)

    async def test_finance_routes_drop_names(self) -> None:
        FakeAsyncClient.handlers["GetBalances"] = lambda body: {
            "Balances": [
                {
                    "ClientId": 101,
                    "DebtMoney": "1500",
                    "HasAnyDebtMoney": True,
                    "StudyBalance": {"BalanceMoney": "0", "DebtMoney": "1500", "IsDebtMoney": True},
                    "EdUnitsBalances": [{"EdUnitId": 5, "EdUnitName": "Math-9", "DebtMoney": "1500"}],
                    "ClientName": "Sample Anna",
                }
            ]
        }
        FakeAsyncClient.handlers["GetPayments"] = lambda body: {
            "Payments": [
                {"Id": 1, "ClientId": 101, "ClientName": "Sample Anna", "Date": "2026-09-10", "Value": "5000", "State": "Paid"},
                {"Id": 2, "ClientId": 101, "ClientName": "Sample Anna", "Date": "2025-01-10", "Value": "4000", "State": "Paid"},
            ]
        }
        FakeAsyncClient.handlers["GetEdUnitStudents"] = lambda body: {
            "EdUnitStudents": [
                {
                    "EdUnitId": 5,
                    "EdUnitName": "Math-9",
                    "StudentClientId": 101,
                    "Payers": [{"ClientId": 101, "Name": "HiddenParent Irina", "Actual": True, "DebtDate": "2026-10-20"}],
                }
            ]
        }

        balance = await _call_hollihope(_route("finance.balance"), {"student_client_id": 101, "balance_date": "2026-10-05"})
        self.assertEqual(FakeAsyncClient.calls[-1][2]["json"]["balanceDate"], "2026-10-05")
        self.assertEqual(FakeAsyncClient.calls[-1][2]["json"]["clientId"], 101)
        self.assertEqual(balance["data"]["balances"][0]["StudyBalance"]["DebtMoney"], "1500")

        payments = await _call_hollihope(
            _route("finance.payments"), {"student_client_id": 101, "date_from": "2026-01-01"}
        )
        self.assertEqual([item["Id"] for item in payments["data"]["payments"]], [1])

        terms = await _call_hollihope(_route("finance.payer_terms"), {"student_client_id": 101})
        self.assertIs(FakeAsyncClient.calls[-1][2]["json"]["queryPayers"], True)
        self.assertEqual(terms["data"]["payer_terms"][0]["Payers"], [{"Actual": True, "DebtDate": "2026-10-20"}])

        for result in (balance, payments, terms):
            self.assertNotIn("Sample Anna", json.dumps(result))
            self.assertNotIn("HiddenParent", json.dumps(result))

    async def test_test_results_merge_both_sources_and_filter_by_date(self) -> None:
        FakeAsyncClient.handlers["GetEdUnitTestResults"] = lambda body: {
            "EdUnitTestResults": [
                {"Date": "2026-09-20", "StudentClientId": 101, "StudentName": "Sample Anna", "TestTypeName": "KIM 1",
                 "Skills": [{"SkillId": 1, "SkillName": "Part 1", "Score": 10.0, "MaxScore": 12.0, "ValidScore": 6.0}]},
                {"Date": "2026-05-20", "StudentClientId": 101, "TestTypeName": "Old"},
            ]
        }
        FakeAsyncClient.handlers["GetPersonalTestResults"] = lambda body: {
            "PersonalTestResults": [
                {"DateTime": "2026-09-25T10:00:00", "StudentClientId": 101, "TeacherId": 4, "TeacherName": "Teacher",
                 "TestTypeName": "Expected score", "Skills": []}
            ]
        }

        result = await _call_hollihope(
            _route("study.test_results"), {"student_client_id": 101, "date_from": "2026-09-01"}
        )

        self.assertEqual([item["TestTypeName"] for item in result["data"]["group_tests"]], ["KIM 1"])
        self.assertEqual(
            result["data"]["group_tests"][0]["Skills"],
            [{"SkillName": "Part 1", "Score": 10.0, "MaxScore": 12.0, "ValidScore": 6.0}],
        )
        self.assertEqual(result["data"]["personal_tests"][0]["TeacherName"], "Teacher")
        self.assertNotIn("Sample Anna", json.dumps(result))

    async def test_reports_and_reference_routes(self) -> None:
        FakeAsyncClient.handlers["GetEdUnitStudentReports"] = lambda body: {
            "EdUnitStudentReports": [
                {"Created": "2026-10-01T09:00:00", "Month": 9, "StudentClientId": 101, "StudentName": "Sample Anna",
                 "Criterions": [{"CriterionName": "Motivation", "Value": 4}], "CommentText": "Good", "CommentHtml": "<p>Good</p>"}
            ]
        }
        FakeAsyncClient.handlers["GetOfflineTestTypes"] = lambda body: {
            "Categories": [{"Id": 2, "Name": "KIM", "TestTypes": [{"Id": 3, "Name": "KIM 1", "Skills": [{"Id": 5, "Name": "Part 1", "MaxScore": 12}]}]}]
        }
        FakeAsyncClient.handlers["GetDisciplines"] = lambda body: {"Disciplines": ["Math", "Physics", {"bad": 1}]}

        reports = await _call_hollihope(_route("study.reports"), {"student_client_id": 101})
        self.assertEqual(
            reports["data"]["reports"],
            [{"Created": "2026-10-01T09:00:00", "Month": 9, "Criterions": [{"CriterionName": "Motivation", "Value": 4}], "CommentText": "Good"}],
        )
        types = await _call_hollihope(_route("reference.test_types"), {})
        self.assertEqual(types["data"]["categories"][0]["TestTypes"][0]["Skills"], [{"Id": 5, "Name": "Part 1", "MaxScore": 12}])
        disciplines = await _call_hollihope(_route("reference.disciplines"), {})
        self.assertEqual(disciplines["data"]["disciplines"], ["Math", "Physics"])

    # --- argument allowlist -------------------------------------------------

    async def test_unknown_and_dangerous_arguments_are_rejected(self) -> None:
        for arguments in (
            {"term": "Sample", "take": 1000},
            {"term": "Sample", "authkey": "other"},
            {"student_client_id": 101, "queryPayers": True},
            {"student_client_id": 101, "function": "EditEdUnitStudent"},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(BackendRouteError):
                    await _call_hollihope(_route("students.find"), arguments)
        self.assertEqual(FakeAsyncClient.calls, [])

    async def test_search_term_and_student_id_validation(self) -> None:
        for arguments in (
            {"term": "ab"},
            {"term": "x" * 65},
            {},
            {"term": "Sample", "student_client_id": 101},
            {"student_client_id": 0},
            {"student_client_id": "abc"},
            {"student_client_id": True},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(BackendRouteError):
                    await _call_hollihope(_route("students.find"), arguments)

        with self.assertRaises(BackendRouteError):
            await _call_hollihope(_route("study.enrollments"), {})

    async def test_date_window_validation(self) -> None:
        for arguments in (
            {"student_client_id": 101, "date_from": "2026-10-05", "date_to": "2026-10-01"},
            {"student_client_id": 101, "date_from": "2024-01-01", "date_to": "2026-10-01"},
            {"student_client_id": 101, "date_from": "01.10.2026"},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(BackendRouteError):
                    await _call_hollihope(_route("study.enrollments"), arguments)

    async def test_default_window_is_sixty_days(self) -> None:
        FakeAsyncClient.handlers["GetEdUnitStudents"] = lambda body: {"EdUnitStudents": []}
        with patch("gateway_mcp.backends.hollihope._today", return_value=datetime.date(2026, 10, 5)):
            await _call_hollihope(_route("study.enrollments"), {"student_client_id": 101})

        body = FakeAsyncClient.calls[-1][2]["json"]
        self.assertEqual((body["dateFrom"], body["dateTo"]), ("2026-08-06", "2026-10-05"))

    # --- who may see which student ------------------------------------------

    async def test_owner_and_all_students_scope_skip_the_assignee_check(self) -> None:
        FakeAsyncClient.handlers["GetEdUnitStudents"] = lambda body: {"EdUnitStudents": []}
        for actor in (OWNER, HEAD):
            with self.subTest(actor=actor.email):
                FakeAsyncClient.calls = []
                self.actor = actor
                await _call_hollihope(_route("study.enrollments"), {"student_client_id": 202})
                self.assertEqual(self._functions_called(), ["GetEdUnitStudents"])

    async def test_manager_reads_assigned_student_only(self) -> None:
        FakeAsyncClient.handlers["GetEdUnitStudents"] = lambda body: {
            "EdUnitStudents": [{"EdUnitId": 5, "StudentClientId": body["studentClientId"]}]
        }
        self.actor = MANAGER

        allowed = await _call_hollihope(_route("study.enrollments"), {"student_client_id": 101})
        self.assertEqual(allowed["data"]["enrollments"], [{"EdUnitId": 5}])
        self.assertEqual(self._functions_called(), ["GetStudents", "GetEdUnitStudents"])

        FakeAsyncClient.calls = []
        with self.assertRaises(PermissionError):
            await _call_hollihope(_route("study.enrollments"), {"student_client_id": 202})
        self.assertEqual(self._functions_called(), ["GetStudents"])

    async def test_manager_finance_access_follows_the_same_rule(self) -> None:
        FakeAsyncClient.handlers["GetBalances"] = lambda body: {"Balances": [{"ClientId": body["clientId"], "DebtMoney": "0"}]}
        self.actor = MANAGER

        with self.assertRaises(PermissionError):
            await _call_hollihope(_route("finance.balance"), {"student_client_id": 202})
        self.assertNotIn("GetBalances", self._functions_called())

    async def test_manager_search_returns_only_assigned_students(self) -> None:
        self.actor = MANAGER

        result = await _call_hollihope(_route("students.find"), {"term": "Sam"})

        self.assertEqual([item["ClientId"] for item in result["data"]["students"]], [101])
        by_id = await _call_hollihope(_route("students.find"), {"student_client_id": 202})
        self.assertEqual(by_id["data"]["students"], [])

    async def test_unmapped_manager_is_denied(self) -> None:
        self.actor = GatewayActor(subject="yandex:9", email="new@example.test", scopes=("hollihope_study:read",))

        with self.assertRaises(PermissionError) as ctx:
            await _call_hollihope(_route("study.reports"), {"student_client_id": 101})
        self.assertIn("not linked", str(ctx.exception))
        self.assertEqual(FakeAsyncClient.calls, [])

    async def test_missing_user_map_denies_managers_but_not_owner(self) -> None:
        FakeAsyncClient.handlers["GetEdUnitStudentReports"] = lambda body: {"EdUnitStudentReports": []}
        with patch.dict(os.environ, {"HOLLIHOPE_USER_MAP_FILE": os.path.join(self._tmp.name, "absent.json")}):
            self.actor = MANAGER
            with self.assertRaises(PermissionError):
                await _call_hollihope(_route("study.reports"), {"student_client_id": 101})
            self.actor = OWNER
            await _call_hollihope(_route("study.reports"), {"student_client_id": 101})

    async def test_broken_user_map_is_a_configuration_error(self) -> None:
        with open(self.map_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.actor = MANAGER

        with self.assertRaises(BackendConfigError):
            await _call_hollihope(_route("study.reports"), {"student_client_id": 101})

    async def test_assignee_lookup_is_cached(self) -> None:
        FakeAsyncClient.handlers["GetEdUnitStudentReports"] = lambda body: {"EdUnitStudentReports": []}
        self.actor = MANAGER

        await _call_hollihope(_route("study.reports"), {"student_client_id": 101})
        await _call_hollihope(_route("study.reports"), {"student_client_id": 101})

        self.assertEqual(self._functions_called().count("GetStudents"), 1)

    async def test_records_of_other_students_are_dropped_even_if_upstream_returns_them(self) -> None:
        FakeAsyncClient.handlers["GetEdUnitStudentReports"] = lambda body: {
            "EdUnitStudentReports": [
                {"StudentClientId": 101, "Month": 9},
                {"StudentClientId": 202, "Month": 8},
            ]
        }
        FakeAsyncClient.handlers["GetPayments"] = lambda body: {
            "Payments": [{"Id": 1, "ClientId": 101}, {"Id": 2, "ClientId": 202}]
        }

        reports = await _call_hollihope(_route("study.reports"), {"student_client_id": 101})
        payments = await _call_hollihope(_route("finance.payments"), {"student_client_id": 101})

        self.assertEqual(reports["data"]["reports"], [{"Month": 9}])
        self.assertEqual(payments["data"]["payments"], [{"Id": 1}])

    # --- route declarations -------------------------------------------------

    async def test_route_with_wrong_scope_or_operation_is_refused(self) -> None:
        with self.assertRaises(BackendRouteError):
            await _call_hollihope(_route("finance.balance", scope="hollihope_study:read"), {"student_client_id": 101})
        with self.assertRaises(BackendRouteError):
            await _call_hollihope({"name": "x", "operation": "students.edit", "scope": "hollihope_students:read"}, {})
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_registry_declares_every_operation_as_read_only(self) -> None:
        registry = json.loads((REPO_ROOT / "gateway-tools.json").read_text(encoding="utf-8"))
        routes = [item for item in registry["tools"] if item.get("backend") == "hollihope"]

        self.assertEqual(sorted(item["operation"] for item in routes), sorted(OPERATIONS))
        self.assertIn("hollihope-rest", BACKEND_DISPATCH)
        for item in routes:
            with self.subTest(route=item["name"]):
                self.assertEqual(item["name"], f"hollihope.{item['operation']}")
                self.assertEqual(item["transport"], "hollihope-rest")
                self.assertEqual(item["scope"], OPERATIONS[item["operation"]][1])
                self.assertTrue(item["scope"].endswith(":read"))
                self.assertIn(item["scope"], DEFAULT_SUPPORTED_SCOPES)
                # http_method would make the Gateway treat the route as mutating.
                self.assertNotIn("http_method", item)
                self.assertEqual(item["status"], "implemented")
        self.assertIn(hollihope.ALL_STUDENTS_SCOPE, DEFAULT_SUPPORTED_SCOPES)

    def test_only_read_functions_are_reachable(self) -> None:
        source = (REPO_ROOT / "gateway_mcp" / "backends" / "hollihope.py").read_text(encoding="utf-8")
        import re

        functions = set(re.findall(r'_post\(\s*"([A-Za-z]+)"', source))
        self.assertTrue(functions)
        for function in functions:
            self.assertTrue(function.startswith("Get"), function)


if __name__ == "__main__":
    unittest.main()
