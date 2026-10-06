# HolliHope CRM connector (read-only)

A read-only bridge between GatewayMCP and the HolliHope (Hollihop) CRM API 2.0.
It exists so that agents can read student progress data without ever holding
the HolliHope API key and without seeing contact details.

## What it guarantees

- **Read-only.** Only the operations listed below exist. Each one calls a fixed
  HolliHope `Get*` function. `Add*`, `Edit*` and `Set*` functions are unreachable.
- **The key stays on the server.** `HOLLIHOPE_API_KEY` is read from the Gateway
  environment and sent in the POST body, never in a URL, never to the caller.
- **Field allowlists.** Every response is reduced to an explicit list of fields.
  Phones, e-mails, addresses, birthdays, parent surnames, payer names and custom
  card fields are dropped. New fields added by HolliHope are dropped by default.
- **Argument allowlists.** Callers can pass only the documented arguments.
- **One student at a time.** Every per-student route requires `student_client_id`;
  search returns at most 10 matches for a term of 3+ characters.
- **Own students only by default.** See "Who sees which student".

## Routes

| Route | Scope | Arguments |
|---|---|---|
| `hollihope.students.find` | `hollihope_students:read` | `term` or `student_client_id` |
| `hollihope.study.enrollments` | `hollihope_study:read` | `student_client_id`, `date_from`, `date_to` |
| `hollihope.study.test_results` | `hollihope_study:read` | `student_client_id`, `date_from`, `date_to` |
| `hollihope.study.reports` | `hollihope_study:read` | `student_client_id`, `date_from`, `date_to` |
| `hollihope.reference.test_types` | `hollihope_study:read` | none |
| `hollihope.reference.disciplines` | `hollihope_study:read` | none |
| `hollihope.finance.balance` | `hollihope_finance:read` | `student_client_id`, `balance_date` |
| `hollihope.finance.payments` | `hollihope_finance:read` | `student_client_id`, `date_from`, `date_to` |
| `hollihope.finance.payer_terms` | `hollihope_finance:read` | `student_client_id`, `date_from`, `date_to` |

Dates use `YYYY-MM-DD`. Routes are called through `gateway_call_tool`
(or `gateway_call_tool_sanitized`), so the caller also needs `tools:call`.

## Who sees which student

- A caller with the `hollihope_students:all` scope (or `*`) can read any student.
- Any other caller must be linked to a HolliHope employee id and can read a
  student only if that employee is listed among the student's assignees.
- A caller who is not linked is denied.

Grant or revoke `hollihope_students:all` in the admin console like any other
scope; the change is audited and can carry an expiry.

The link between Gateway users and HolliHope employees lives in a server-side
JSON file (`HOLLIHOPE_USER_MAP_FILE`, default `/config/hollihope-users.json`):

```json
{"users": {"manager@yandex.ru": 15, "another.login": 22}}
```

Keys are the Gateway e-mail, login or subject (case-insensitive); values are
HolliHope employee ids. The file is re-read on every call. Mount the directory
that contains it, not the single file, so that edits are picked up.

## Configuration

| Variable | Meaning |
|---|---|
| `HOLLIHOPE_BASE_URL` | `https://<school>.t8s.ru/Api/V2` (https only) |
| `HOLLIHOPE_API_KEY` | key from HolliHope: Settings, Integration, API |
| `HOLLIHOPE_USER_MAP_FILE` | path to the user map inside the container |
| `HOLLIHOPE_MAX_TAKE` | upstream page size, 1 to 1000, default 500 |

## Adding a route later

1. Add a handler and an entry in `OPERATIONS` in `gateway_mcp/backends/hollihope.py`
   with its own field allowlist and required scope.
2. Declare the route in `gateway-tools.json` with the same scope and no `http_method`.
3. Extend `tests/test_backends_hollihope.py`.

Write operations are deliberately absent. Adding one requires a `:write` scope,
`requires_approval_ref` and a separate review.
