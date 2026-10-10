from typing import Any


def _resource(
    system: str,
    resource_type: str,
    pattern: str,
    *actions: str,
) -> dict[str, Any]:
    return {
        "system": system,
        "resource_type": resource_type,
        "resource_pattern": pattern,
        "actions": list(actions),
        "effect": "allow",
        "priority": 100,
    }


_PACKAGES: tuple[dict[str, Any], ...] = (
    {
        "key": "project-manager",
        "version": 2,
        "title": "Руководитель проекта",
        "description": "Контекст проекта, задачи, документы, память и CRM-срез.",
        "risk": "standard",
        "scopes": (
            "tools:call",
            "company:read",
            "process:read",
            "memory:read",
            "memory:write",
            "files:read",
            "files:write",
            "llm:proxy",
            "privacy:use",
            "yonote:read",
            "yonote:write",
            "tracker:read",
            "tracker:write",
            "bitrix24:read",
            "yandex_disk:read",
            "yandex_disk:write",
        ),
        "resources": (
            _resource("yonote", "document", "*", "read", "write"),
            _resource("tracker", "issue", "*", "read", "write"),
            _resource("bitrix24", "crm", "*", "read"),
            _resource("yandex_disk", "path", "*", "read", "write"),
        ),
    },
    {
        "key": "developer",
        "version": 2,
        "title": "AI-Native инженер",
        "description": "Код, задачи, проектный контекст и локальное исполнение.",
        "risk": "elevated",
        "scopes": (
            "tools:call",
            "company:read",
            "memory:read",
            "memory:write",
            "files:read",
            "files:write",
            "llm:proxy",
            "privacy:use",
            "yonote:read",
            "tracker:read",
            "tracker:write",
            "gitlab:read",
            "gitlab:write",
            "factory:read",
            "factory:write",
        ),
        "resources": (
            _resource("yonote", "document", "*", "read"),
            _resource("tracker", "issue", "*", "read", "write"),
            _resource("gitlab", "project", "*", "read", "write"),
        ),
    },
    {
        "key": "factory-worker",
        "version": 2,
        "title": "Исполнитель фабрики разработки",
        "description": "Получение Work Contract, работа с кодом и фиксация результата.",
        "risk": "elevated",
        "scopes": (
            "tools:call",
            "company:read",
            "memory:read",
            "files:read",
            "files:write",
            "llm:proxy",
            "privacy:use",
            "yonote:read",
            "tracker:read",
            "tracker:write",
            "gitlab:read",
            "gitlab:write",
            "factory:read",
            "factory:write",
            "factory:claim",
        ),
        "resources": (
            _resource("yonote", "document", "*", "read"),
            _resource("tracker", "issue", "*", "read", "write"),
            _resource("gitlab", "project", "*", "read", "write"),
        ),
    },
    {
        "key": "sales-manager",
        "version": 2,
        "title": "Менеджер продаж",
        "description": "CRM, КП и договоры, проектный контекст, почта и документы.",
        "risk": "elevated",
        "scopes": (
            "tools:call",
            "company:read",
            "memory:read",
            "memory:write",
            "files:read",
            "files:write",
            "llm:proxy",
            "privacy:use",
            "yonote:read",
            "bitrix24:read",
            "bitrix24:write",
            "yandex_disk:read",
            "yandex_disk:write",
            "mail:read",
            "mail:send",
        ),
        "resources": (
            _resource("yonote", "document", "*", "read"),
            _resource("bitrix24", "crm", "*", "read", "write"),
            _resource("yandex_disk", "path", "*", "read", "write"),
            _resource("mail", "mailbox", "*", "read", "write"),
        ),
    },
    {
        "key": "knowledge-curator",
        "version": 2,
        "title": "Куратор корпоративных знаний",
        "description": "Память, Yonote и файловое хранилище без доступа к CRM и коду.",
        "risk": "standard",
        "scopes": (
            "tools:call",
            "company:read",
            "memory:read",
            "memory:write",
            "files:read",
            "files:write",
            "llm:proxy",
            "privacy:use",
            "yonote:read",
            "yonote:write",
            "yandex_disk:read",
            "yandex_disk:write",
        ),
        "resources": (
            _resource("yonote", "document", "*", "read", "write"),
            _resource("yandex_disk", "path", "*", "read", "write"),
        ),
    },
    {
        "key": "independent-reviewer",
        "version": 2,
        "title": "Независимый ревьюер",
        "description": "Чтение кода и требований, комментарии в MR и решение по согласованию.",
        "risk": "standard",
        "scopes": (
            "tools:call",
            "company:read",
            "memory:read",
            "llm:proxy",
            "privacy:use",
            "yonote:read",
            "tracker:read",
            "gitlab:read",
            "gitlab:write",
            "factory:read",
            "factory:write",
            "approvals:read",
            "approvals:write",
        ),
        "resources": (
            _resource("yonote", "document", "*", "read"),
            _resource("tracker", "issue", "*", "read"),
            _resource("gitlab", "project", "*", "read", "write"),
        ),
    },
    {
        "key": "progress-monitoring-manager",
        "version": 1,
        "title": "Менеджер: мониторинг прогресса обучения",
        "description": (
            "Чтение данных HolliHope только по своим ученикам: карточка, занятия, "
            "оценки, отчёты педагогов и оплата обучения. Без записи."
        ),
        "risk": "standard",
        "scopes": (
            "tools:call",
            "hollihope_students:read",
            "hollihope_study:read",
            "hollihope_finance:read",
        ),
        "resources": (
            _resource("hollihope_students", "student", "*", "read"),
            _resource("hollihope_study", "student", "*", "read"),
            _resource("hollihope_finance", "student", "*", "read"),
        ),
    },
    {
        "key": "progress-monitoring-lead",
        "version": 1,
        "title": "Руководитель: мониторинг прогресса обучения",
        "description": (
            "То же, что у менеджера, по всем ученикам школы: карточка, занятия, "
            "оценки, отчёты педагогов и оплата обучения. Без записи."
        ),
        "risk": "elevated",
        "scopes": (
            "tools:call",
            "hollihope_students:read",
            "hollihope_students:all",
            "hollihope_study:read",
            "hollihope_finance:read",
        ),
        "resources": (
            _resource("hollihope_students", "student", "*", "read"),
            _resource("hollihope_study", "student", "*", "read"),
            _resource("hollihope_finance", "student", "*", "read"),
        ),
    },
    {
        "key": "student-profile-reader",
        "version": 2,
        "title": "Профиль ученика для обратной связи",
        "description": (
            "Чтение профиля ученика в сервисе данных учеников (edu-data): КИМ, "
            "посещаемость, оценки, опросы, чек-листы. Какие ученики доступны, "
            "определяет роль сотрудника в edu-data. Агент может сохранить свой черновик "
            "скрипта обратной связи в профиль ученика; данные ученика не меняются."
        ),
        "risk": "standard",
        "scopes": (
            "tools:call",
            "edu_profile:read",
            "edu_feedback:write",
        ),
        "resources": (
            _resource("edu_profile", "student", "*", "read"),
            _resource("edu_feedback", "student", "*", "write"),
        ),
    },
)


def access_package_catalog() -> list[dict[str, Any]]:
    return [_copy_package(package) for package in _PACKAGES]


def access_package(package_key: str) -> dict[str, Any]:
    key = str(package_key or "").strip().casefold()
    package = next((item for item in _PACKAGES if item["key"] == key), None)
    if package is None:
        raise ValueError(f"unknown access package: {key}")
    return _copy_package(package)


def _copy_package(package: dict[str, Any]) -> dict[str, Any]:
    return {
        **package,
        "scopes": list(package.get("scopes") or []),
        "resources": [dict(item) for item in package.get("resources") or []],
    }
