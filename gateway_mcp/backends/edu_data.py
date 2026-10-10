"""Read-only edu-data backend: student search and the student profile.

edu-data is the centre's student data service (registry, KIM results per task,
surveys, teacher checklists, attendance and grades from HolliHope). It applies
its own role rules, so the Gateway forwards the caller's e-mail and edu-data
answers exactly what this employee may see in its web interface: a manager
gets only the students he is responsible for, an operator gets nothing.

Design rules, enforced here:

* Only the operations in ``OPERATIONS`` exist; each calls one fixed GET path.
* The service token is read from the server environment, sent in the
  Authorization header, and never returned.
* Caller arguments are validated against an allowlist; the student id must be
  a UUID, so a caller cannot reach other edu-data paths.
* The acting user is the authenticated Gateway actor; a caller without an
  e-mail is denied.
"""

import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from gateway_mcp.backends.common import BackendConfigError, BackendRouteError, _env


BACKEND = "edu_data"
PROFILE_SCOPE = "edu_profile:read"

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MIN_QUERY = 3
_MAX_QUERY = 64
_MAX_DISCIPLINE = 64


def _actor():
    from gateway_mcp.services.auth import current_actor

    return current_actor()


def _base_url() -> str:
    url = _env("EDU_DATA_URL").rstrip("/")
    if not url.startswith("https://"):
        raise BackendConfigError("EDU_DATA_URL must use https")
    return url


def _token() -> str:
    token = _env("EDU_DATA_SERVICE_TOKEN")
    if len(token) < 32:
        raise BackendConfigError("EDU_DATA_SERVICE_TOKEN is too short")
    return token


def _reject_unknown(arguments: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise BackendRouteError(f"Unsupported arguments: {', '.join(unknown)}")


async def _get(path: str, params: dict[str, str]) -> dict[str, Any]:
    actor = _actor()
    email = str(getattr(actor, "email", "") or "").strip()
    if not email:
        raise PermissionError("edu-data needs the caller's e-mail to apply its access rules")
    timeout = float(os.getenv("GATEWAY_UPSTREAM_TIMEOUT_SECONDS", "60"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            "GET",
            f"{_base_url()}{path}",
            params=params,
            headers={"Authorization": f"Bearer {_token()}", "X-Acting-User": email, "Accept": "application/json"},
        )
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code == 403:
        raise PermissionError("edu-data: this employee has no access to the requested student")
    if response.status_code == 404:
        return {"error": "not_found"}
    if not response.is_success or not isinstance(data, dict):
        message = str(data.get("error") or "")[:100] if isinstance(data, dict) else ""
        raise BackendRouteError(f"edu-data {path.split('/')[2]} failed with HTTP {response.status_code}"
                                + (f": {message}" if message else ""))
    return data


async def _op_students_find(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"query"})
    query = str(arguments.get("query") or "").strip()
    if not _MIN_QUERY <= len(query) <= _MAX_QUERY:
        raise BackendRouteError(f"query must be {_MIN_QUERY} to {_MAX_QUERY} characters")
    return await _get("/api/students/find", {"q": query})


async def _op_student_profile(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, {"student_id", "discipline"})
    student_id = str(arguments.get("student_id") or "").strip().lower()
    if not _UUID.match(student_id):
        raise BackendRouteError("student_id must be the edu-data student id (UUID) from edu.students.find")
    params = {}
    discipline = str(arguments.get("discipline") or "").strip()
    if discipline:
        if len(discipline) > _MAX_DISCIPLINE:
            raise BackendRouteError("discipline is too long")
        params["discipline"] = discipline
    return await _get(f"/api/students/{quote(student_id)}/profile", params)


OPERATIONS: dict[str, tuple[Any, str]] = {
    "students.find": (_op_students_find, PROFILE_SCOPE),
    "student.profile": (_op_student_profile, PROFILE_SCOPE),
}


async def _call_edu_data(route: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    operation = str(route.get("operation") or "").strip()
    if operation not in OPERATIONS:
        raise BackendRouteError(f"edu-data route {route.get('name')} has an unsupported operation: {operation or '<missing>'}")
    handler, expected_scope = OPERATIONS[operation]
    if str(route.get("scope") or "") != expected_scope:
        raise BackendRouteError(f"edu-data route {route.get('name')} must declare scope {expected_scope}")
    if not isinstance(arguments, dict):
        raise BackendRouteError("edu-data arguments must be a JSON object")
    data = await handler(arguments)
    return {"ok": True, "status": 200, "backend": BACKEND, "operation": operation, "data": data}
