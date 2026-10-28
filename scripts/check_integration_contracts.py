"""Check discovered integration declarations once, without assembling application resources."""

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment",
        required=True,
        choices=("production", "local"),
        help="declaration scope; production never connects to a production environment",
    )
    args = parser.parse_args(argv)

    from app.diagnostics import log_exception

    phase = "load_formal_modules"
    try:
        from app.businesses import catalog
        from app.integrations.discovery import load_formal_modules, load_local_modules
        from app.integrations.ecs_callback.registry import events
        from app.integrations.wms_callback.registry import operations as callbacks
        from app.integrations.wms_request.operation import operations as requests

        load_formal_modules()
        phase = "validate_business_contracts"
        commands, annexes = catalog.validate_business_contracts()
        if args.environment == "local":
            phase = "load_local_modules"
            load_local_modules(environment="local")

        phase = "collect_report"
        entries = []
        for kind, items in (
            ("ecs_event", events()),
            ("wms_callback", callbacks()),
            ("wms_request", requests()),
        ):
            for key, item in items.items():
                role, name = key if kind == "ecs_event" else (None, key)
                entries.append(
                    {
                        "kind": kind,
                        "name": name,
                        "device_role": role,
                        **asdict(item.registration_source),
                    }
                )
        command_contracts = []
        for command in commands:
            annex = annexes[(command.annex_id, command.annex_version)]
            command_contracts.append(
                {
                    "kind": "ecs_command",
                    "name": command.task_type,
                    "device_role": command.device_role,
                    "annex_id": command.annex_id,
                    "annex_version": command.annex_version,
                    "timeout_min_ms": annex.timeout_min_ms,
                    "timeout_max_ms": annex.timeout_max_ms,
                    **asdict(command.registration_source),
                }
            )
        checked = [
            "Discovered and imported formal declarations in the existing three registries",
            "Formal businesses from explicit catalog.entries() only",
            "Existing registration and validate_business_contracts rules only",
        ]
        if args.environment == "local":
            checked.append("Discovered local declarations after formal contract validation")
        report = {
            "environment": args.environment,
            "businesses": sorted(definition.code for definition in catalog.definitions()),
            "entries": sorted(
                entries, key=lambda row: (row["kind"], row["device_role"] or "", row["name"])
            ),
            "command_contracts": sorted(
                command_contracts,
                key=lambda row: (
                    row["device_role"],
                    row["name"],
                    row["annex_id"],
                    row["annex_version"],
                ),
            ),
            "scope": {
                "checked": checked,
                "excluded": [
                    "Arbitrary source files not discovered and imported",
                    "Runtime routes, generic v3 debug commands and ECS Status capability",
                    "Business models/table creation and database migrations",
                    "Network connectivity, deployment configuration and permissions",
                    "Handler/factory execution, message replay and physical/business completion",
                    "Future declaration syntax migration eligibility",
                ],
            },
        }
        output = json.dumps(report, ensure_ascii=False, sort_keys=True)
    except Exception as error:
        log_exception(
            logging.getLogger(__name__),
            "Integration contract check failed; "
            "修复: 检查来源处的名称、文件名、角色、附录和依赖声明",
            error,
            component="integration_contract_check",
            phase=phase,
        )
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
