"""Read-only HolliHope (Hollihop) CRM backend.

Design rules, all enforced in this module:

* Only the operations listed in ``OPERATIONS`` exist. Each one maps to a fixed
  HolliHope ``Get*`` function; callers cannot choose the function name, so the
  mutating API functions (``Add*``, ``Edit*``, ``Set*``) are unreachable.
* The API key is read from the server environment and sent in the POST body,
  never in the URL, and is never returned to the caller.
* Upstream arguments are built explicitly from a small validated allowlist.
  Unknown caller arguments are rejected.
* Every response is projected through a per-operation field allowlist, so
  phones, e-mails, addresses, birthdays and parent surnames never leave the
  Gateway. Anything HolliHope adds to its responses later is dropped by default.
* Per-student operations require a student id. A caller without the
  ``hollihope_students:all`` scope must be mapped to a HolliHope employee and
  may only read students that list this employee as an assignee.
"""

import datetime
import json
import os
import time
from typing import Any

import httpx

from gateway_mcp.backends.common import BackendConfigError, BackendRouteError, _env


BACKEND = "hollihope"
ALL_STUDENTS_SCOPE = "hollihope_students:all"
DEFAULT_USER_MAP_FILE = "/config/hollihope-users.json"

_SEARCH_MIN_TERM = 3
_SEARCH_MAX_TERM = 64
_SEARCH_TAKE = 10
_DEFAULT_WINDOW_DAYS = 60
_MAX_WINDOW_DAYS = 400
_ASSIGNEE_CACHE_TTL_SECONDS = 300.0
_ASSIGNEE_CACHE_MAX = 2000

_assignee_cache: dict[int, tuple[float, frozenset[int]]] = {}


# --- field allowlists -------------------------------------------------------
# True  -> keep a scalar (or a list of scalars) as is
# dict  -> keep a nested object / list of objects, projected recursively
# Anything not listed is dropped.

_STUDENT_FIELDS: dict[str, Any] = {
    "ClientId": True,
    "FirstName": True,
    "LastName": True,
    "Status": True,
    "Maturity": True,
    "LearningTypes": True,
    "Disciplines": {"Discipline": True, "Level": True},
    "OfficesAndCompanies": {"Name": True},
    "Assignees": {"Id": True, "FullName": True},
    "Agents": {"FirstName": True, "MiddleName": True, "WhoIs": True, "IsCustomer": True},
}

_ENROLLMENT_FIELDS: dict[str, Any] = {
    "EdUnitId": True,
    "EdUnitType": True,
    "EdUnitName": True,
    "EdUnitDiscipline": True,
    "EdUnitLevel": True,
    "EdUnitMaturity": True,
    "EdUnitLearningType": True,
    "EdUnitOfficeOrCompanyName": True,
    "BeginDate": True,
    "EndDate": True,
    "BeginTime": True,
    "EndTime": True,
    "Weekdays": True,
    "Status": True,
    "StudyUnits": True,
    "Days": {"Date": True, "Minutes": True, "Pass": True, "Accepted": True, "Description": True},
}

_SKILL_FIELDS: dict[str, Any] = {"SkillName": True, "Score": True, "MaxScore": True, "ValidScore": True}

_GROUP_TEST_FIELDS: dict[str, Any] = {
    "Date": True,
    "EdUnitName": True,
    "TestTypeCategoryName": True,
    "TestTypeName": True,
    "Skills": _SKILL_FIELDS,
    "CommentText": True,
}

_PERSONAL_TEST_FIELDS: dict[str, Any] = {
    "DateTime": True,
    "Discipline": True,
    "TeacherName": True,
    "TestTypeCategoryName": True,
    "TestTypeName": True,
    "Skills": _SKILL_FIELDS,
    "CommentText": True,
}

_REPORT_FIELDS: dict[str, Any] = {
    "Created": True,
    "Month": True,
    "EdUnitName": True,
    "Discipline": True,
    "Criterions": {"CriterionName": True, "Value": True},
    "CommentText": True,
}

_TEST_TYPE_CATEGORY_FIELDS: dict[str, Any] = {
    "Id": True,
    "Name": True,
    "TestTypes": {
        "Id": True,
        "Name": True,
        "Skills": {"Id": True, "Name": True, "MaxScore": True, "ValidScore": True},
    },
}

_BALANCE_FIELDS: dict[str, Any] = {
    "ClientId": True,
    "BalanceUnits": True,
    "BalanceMoney": True,
    "DebtUnits": True,
    "DebtMoney": True,
    "HasAnyDebtUnits": True,
    "HasAnyDebtMoney": True,
    "HasAggregateDebtUnits": True,
    "HasAggregateDebtMoney": True,
    "StudyBalance": {"BalanceMoney": True, "DebtMoney": True, "IsDebtMoney": True},
    "EdUnitsBalances": {
        "EdUnitId": True,
        "EdUnitType": True,
        "EdUnitName": True,
        "BalanceUnits": True,
        "BalanceMoney": True,
        "DebtUnits": True,
        "DebtMoney": True,
        "IsDebtUnits": True,
        "IsDebtMoney": True,
    },
}

_PAYMENT_FIELDS: dict[str, Any] = {
    "Id": True,
    "Created": True,
    "Type": True,
    "Date": True,
    "PaidDate": True,
    "OfficeOrCompanyName": True,
    "State": True,
    "Value": True,
    "ValueQuantity": True,
    "ValueCurrency": True,
    "PaymentMethodName": True,
    "Description": True,
}

_PAYER_TERMS_FIELDS: dict[str, Any] = {
    "EdUnitId": True,
    "EdUnitName": True,
    "EdUnitDiscipline": True,
    "Status": True,
    "BeginDate": True,
    "EndDate": True,
    "Payers": {
        "IsCompany": True,
        "Actual": True,
        "PayableMinutes": True,
        "PayableUnits": True,
        "PayableMinutesRanged": True,
        "PayableUnitsRanged": True,
        "ValuePaidRanged": True,
        "DebtDate": True,
    },
}


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _project(value: Any, fields: dict[str, Any]) -> Any:
    """Return only allowlisted fields of an object or a list of objects."""
    if isinstance(value, list):
        return [_project(item, fields) for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key, rule in fields.items():
        if key not in value:
            continue
        item = value[key]
        if rule is True:
            if _is_scalar(item):
                result[key] = item
            elif isinstance(item, list) and all(_is_scalar(entry) for entry in item):
                result[key] = list(item)
        elif isinstance(rule, dict) and isinstance(item, (dict, list)):
            result[key] = _project(item, rule)
    return result


# --- configuration ----------------------------------------------------------


def _base_url() -> str:
    base_url = _env("HOLLIHOPE_BASE_URL").strip().rstrip("/")
    if not base_url.casefold().startswith("https://"):
        raise BackendConfigError("HOLLIHOPE_BASE_URL must be an https:// URL")
    return base_url


def _api_key() -> str:
    return _env("HOLLIHOPE_API_KEY").strip()


def _max_take() -> int:
    try:
        value = int(os.getenv("HOLLIHOPE_MAX_TAKE", "500"))
    except ValueError:
        value = 500
    return max(1, min(value, 1000))


def _today() -> datetime.date:
    return datetime.date.today()


# --- upstream call ----------------------------------------------------------


async def _post(function: str, upstream_args: dict[str, Any]) -> dict[str, Any]:
    """Call one HolliHope API function. The key travels in the POST body only."""
    body = dict(upstream_args)
    body["authkey"] = _api_key()
    timeout = float(os.getenv("GATEWAY_UPSTREAM_TIMEOUT_SECONDS", "60"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            "POST",
            f"{_base_url()}/{function}",
            headers={"Content-Type": "application/json;charset=utf-8", "Accept": "application/json"},
            json=body,
        )
    try:
        data = response.json()
    except ValueError:
        data = None
    if not response.is_success or not isinstance(data, dict):
        message = ""
        if isinstance(data, dict):
            message = str(data.get("Error") or "")[:200]
        raise BackendRouteError(
            f"HolliHope {function} failed with HTTP {response.status_code}"
            + (f": {message}" if message else "")
        )
    return data


# --- caller arguments -------------------------------------------------------


def _reject_unknown(arguments: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(key for key in arguments if key not in allowed)
    if unknown:
        raise BackendRouteError(
            "Unsupported HolliHope argument(s): " + ", ".join(unknown) + ". Allowed: " + ", ".join(sorted(allowed))
        )


def _student_id(arguments: dict[str, Any], *, required: bool = True) -> int | None:
    raw = arguments.get("student_client_id")
    if raw is None or raw == "":
        if required:
            raise BackendRouteError("student_client_id is required")
        return None
    if isinstance(raw, bool):
        raise BackendRouteError("student_client_id must be a positive integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise BackendRouteError("student_client_id must be a positive integer") from exc
    if value <= 0:
        raise BackendRouteError("student_client_id must be a positive integer")
    return value


def _date_arg(arguments: dict[str, Any], name: str) -> datetime.date | None:
    raw = arguments.get(name)
    if raw is None or raw == "":
        return None
    try:
        return datetime.date.fromisoformat(str(raw))
    except ValueError as exc:
        raise BackendRouteError(f"{name} must be a date in YYYY-MM-DD format") from exc


def _window(arguments: dict[str, Any]) -> tuple[datetime.date, datetime.date]:
    date_to = _date_arg(arguments, "date_to") or _today()
    date_from = _date_arg(arguments, "date_from") or (date_to - datetime.timedelta(days=_DEFAULT_WINDOW_DAYS))
    if date_from > date_to:
        raise BackendRouteError("date_from must not be later than date_to")
    if (date_to - date_from).days > _MAX_WINDOW_DAYS:
        raise BackendRouteError(f"date range must not exceed {_MAX_WINDOW_DAYS} days")
    return date_from, date_to


def _record_date(record: dict[str, Any], *keys: str) -> datetime.date | None:
    for key in keys:
        raw = record.get(key)
        if isinstance(raw, str) and len(raw) >= 10:
            try:
                return datetime.date.fromisoformat(raw[:10])
            except ValueError:
                continue
    return None


def _filter_by_dates(
    records: list[dict[str, Any]],
    arguments: dict[str, Any],
    *date_keys: str,
) -> list[dict[str, Any]]:
    date_from = _date_arg(arguments, "date_from")
    date_to = _date_arg(arguments, "date_to")
    if date_from and date_to and date_from > date_to:
        raise BackendRouteError("date_from must not be later than date_to")
    if not date_from and not date_to:
        return records
    kept = []
    for record in records:
        when = _record_date(record, *date_keys)
        if when is None:
            continue
        if date_from and when < date_from:
            continue
        if date_to and when > date_to:
            continue
        kept.append(record)
    return kept


def _records(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _only_student(records: list[dict[str, Any]], key: str, student_id: int) -> list[dict[str, Any]]:
    """Defense in depth: never trust the upstream filter alone."""
    return [record for record in records if record.get(key) == student_id]


# --- who may see which student ----------------------------------------------


def _actor():
    from gateway_mcp.services.auth import current_actor

    return current_actor()


def _sees_all_students(actor: Any) -> bool:
    from gateway_mcp.services.policy import has_scope

    return has_scope(actor, ALL_STUDENTS_SCOPE)


def _user_map() -> dict[str, int]:
    path = os.getenv("HOLLIHOPE_USER_MAP_FILE", DEFAULT_USER_MAP_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise BackendConfigError(f"HolliHope user map is unreadable: {type(exc).__name__}") from exc
    users = raw.get("users") if isinstance(raw, dict) else None
    if not isinstance(users, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in users.items():
        if isinstance(value, bool):
            continue
        try:
            employee_id = int(value)
        except (TypeError, ValueError):
            continue
        if employee_id > 0:
            result[str(key).strip().casefold()] = employee_id
    return result


def _employee_id(actor: Any) -> int:
    users = _user_map()
    for candidate in (getattr(actor, "email", ""), getattr(actor, "login", ""), getattr(actor, "subject", "")):
        key = str(candidate or "").strip().casefold()
        if key and key in users:
            return users[key]
    raise PermissionError(
        "HolliHope access denied: your Gateway account is not linked to a HolliHope employee. "
        "Ask a Gateway administrator to link it."
    )


def _assignee_ids(student: dict[str, Any]) -> frozenset[int]:
    ids = set()
    for assignee in student.get("Assignees") or []:
        if isinstance(assignee, dict) and isinstance(assignee.get("Id"), int) and not isinstance(assignee.get("Id"), bool):
            ids.add(assignee["Id"])
    return frozenset(ids)


def _cache_assignees(student_id: int, ids: frozenset[int]) -> None:
    if len(_assignee_cache) >= _ASSIGNEE_CACHE_MAX:
        _assignee_cache.clear()
    _assignee_cache[student_id] = (time.monotonic(), ids)


async def _student_assignees(student_id: int) -> frozenset[int]:
    cached = _assignee_cache.get(student_id)
    if cached and time.monotonic() - cached[0] < _ASSIGNEE_CACHE_TTL_SECONDS:
        return cached[1]
    data = await _post("GetStudents", {"clientId": student_id})
    students = _only_student(_records(data, "Students"), "ClientId", student_id)
    ids = _assignee_ids(students[0]) if students else frozenset()
    _cache_assignees(student_id, ids)
    return ids


async def _require_student_access(student_id: int) -> None:
    actor = _actor()
    if _sees_all_students(actor):
        return
    employee_id = _employee_id(actor)
    if employee_id not in await _student_assignees(student_id):
        raise PermissionError("HolliHope access denied: this student is not assigned to you.")


# --- operations -------------------------------------------------------------


async def _op_students_find(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"term", "student_client_id"})
    student_id = _student_id(arguments, required=False)
    term = str(arguments.get("term") or "").strip()
    if student_id is not None and term:
        raise BackendRouteError("Pass either term or student_client_id, not both")
    if student_id is not None:
        data = await _post("GetStudents", {"clientId": student_id})
        students = _only_student(_records(data, "Students"), "ClientId", student_id)
    else:
        if len(term) < _SEARCH_MIN_TERM:
            raise BackendRouteError(f"term must contain at least {_SEARCH_MIN_TERM} characters")
        if len(term) > _SEARCH_MAX_TERM:
            raise BackendRouteError(f"term must contain at most {_SEARCH_MAX_TERM} characters")
        data = await _post("GetStudents", {"term": term, "take": _SEARCH_TAKE})
        students = _records(data, "Students")[:_SEARCH_TAKE]

    actor = _actor()
    if not _sees_all_students(actor):
        employee_id = _employee_id(actor)
        students = [student for student in students if employee_id in _assignee_ids(student)]
    for student in students:
        if isinstance(student.get("ClientId"), int):
            _cache_assignees(student["ClientId"], _assignee_ids(student))
    return {"students": _project(students, _STUDENT_FIELDS)}


async def _op_study_enrollments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"student_client_id", "date_from", "date_to"})
    student_id = _student_id(arguments)
    date_from, date_to = _window(arguments)
    await _require_student_access(student_id)
    data = await _post(
        "GetEdUnitStudents",
        {
            "studentClientId": student_id,
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "queryDays": True,
            "take": _max_take(),
        },
    )
    records = _only_student(_records(data, "EdUnitStudents"), "StudentClientId", student_id)
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "enrollments": _project(records, _ENROLLMENT_FIELDS),
    }


async def _op_study_test_results(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"student_client_id", "date_from", "date_to"})
    student_id = _student_id(arguments)
    await _require_student_access(student_id)
    upstream = {"studentClientId": student_id, "take": _max_take()}
    group_data = await _post("GetEdUnitTestResults", dict(upstream))
    personal_data = await _post("GetPersonalTestResults", dict(upstream))
    group = _only_student(_records(group_data, "EdUnitTestResults"), "StudentClientId", student_id)
    personal = _only_student(_records(personal_data, "PersonalTestResults"), "StudentClientId", student_id)
    group = _filter_by_dates(group, arguments, "Date")
    personal = _filter_by_dates(personal, arguments, "DateTime")
    return {
        "group_tests": _project(group, _GROUP_TEST_FIELDS),
        "personal_tests": _project(personal, _PERSONAL_TEST_FIELDS),
    }


async def _op_study_reports(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"student_client_id", "date_from", "date_to"})
    student_id = _student_id(arguments)
    await _require_student_access(student_id)
    data = await _post("GetEdUnitStudentReports", {"studentClientId": student_id, "take": _max_take()})
    records = _only_student(_records(data, "EdUnitStudentReports"), "StudentClientId", student_id)
    records = _filter_by_dates(records, arguments, "Created")
    return {"reports": _project(records, _REPORT_FIELDS)}


async def _op_reference_test_types(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, set())
    data = await _post("GetOfflineTestTypes", {})
    return {"categories": _project(_records(data, "Categories"), _TEST_TYPE_CATEGORY_FIELDS)}


async def _op_reference_disciplines(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, set())
    data = await _post("GetDisciplines", {})
    disciplines = data.get("Disciplines")
    if not isinstance(disciplines, list):
        disciplines = []
    return {"disciplines": [item for item in disciplines if isinstance(item, str)]}


async def _op_finance_balance(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"student_client_id", "balance_date"})
    student_id = _student_id(arguments)
    balance_date = _date_arg(arguments, "balance_date") or _today()
    await _require_student_access(student_id)
    data = await _post("GetBalances", {"clientId": student_id, "balanceDate": balance_date.isoformat()})
    records = _only_student(_records(data, "Balances"), "ClientId", student_id)
    return {"balance_date": balance_date.isoformat(), "balances": _project(records, _BALANCE_FIELDS)}


async def _op_finance_payments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"student_client_id", "date_from", "date_to"})
    student_id = _student_id(arguments)
    await _require_student_access(student_id)
    data = await _post("GetPayments", {"clientId": student_id, "take": _max_take()})
    records = _only_student(_records(data, "Payments"), "ClientId", student_id)
    records = _filter_by_dates(records, arguments, "Date", "Created")
    return {"payments": _project(records, _PAYMENT_FIELDS)}


async def _op_finance_payer_terms(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"student_client_id", "date_from", "date_to"})
    student_id = _student_id(arguments)
    date_from, date_to = _window(arguments)
    await _require_student_access(student_id)
    data = await _post(
        "GetEdUnitStudents",
        {
            "studentClientId": student_id,
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "queryPayers": True,
            "take": _max_take(),
        },
    )
    records = _only_student(_records(data, "EdUnitStudents"), "StudentClientId", student_id)
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "payer_terms": _project(records, _PAYER_TERMS_FIELDS),
    }


# operation -> (handler, scope the route must declare)
OPERATIONS: dict[str, tuple[Any, str]] = {
    "students.find": (_op_students_find, "hollihope_students:read"),
    "study.enrollments": (_op_study_enrollments, "hollihope_study:read"),
    "study.test_results": (_op_study_test_results, "hollihope_study:read"),
    "study.reports": (_op_study_reports, "hollihope_study:read"),
    "reference.test_types": (_op_reference_test_types, "hollihope_study:read"),
    "reference.disciplines": (_op_reference_disciplines, "hollihope_study:read"),
    "finance.balance": (_op_finance_balance, "hollihope_finance:read"),
    "finance.payments": (_op_finance_payments, "hollihope_finance:read"),
    "finance.payer_terms": (_op_finance_payer_terms, "hollihope_finance:read"),
}


async def _call_hollihope(route: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    operation = str(route.get("operation") or "").strip()
    if operation not in OPERATIONS:
        raise BackendRouteError(f"HolliHope route {route.get('name')} has an unsupported operation: {operation or '<missing>'}")
    handler, expected_scope = OPERATIONS[operation]
    # A route file edited by mistake must not be able to expose an operation
    # under a weaker scope than the one it was reviewed with.
    if str(route.get("scope") or "") != expected_scope:
        raise BackendRouteError(
            f"HolliHope route {route.get('name')} must declare scope {expected_scope}"
        )
    if not isinstance(arguments, dict):
        raise BackendRouteError("HolliHope arguments must be a JSON object")
    data = await handler(arguments)
    return {
        "ok": True,
        "status": 200,
        "backend": BACKEND,
        "operation": operation,
        "data": data,
    }
