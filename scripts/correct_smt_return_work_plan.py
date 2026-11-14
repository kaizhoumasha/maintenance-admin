"""Queue one return-rack work-plan correction after explicit WMS approval."""

import argparse
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.tasks._db import run_with_factory


async def correct(factory, original_id):
    from app.tasks.business_tasks import _runtime

    service = _runtime(factory, "smt_auto").services["smt_auto"]
    return await service.advance("CORRECT_RETURN_FACE_READY", original_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original_operation_id", type=UUID)
    parser.add_argument("--wms-approved", required=True, action="store_true")
    args = parser.parse_args()
    try:
        result = run_with_factory(
            get_settings().DATABASE_URL,
            lambda factory: correct(factory, str(args.original_operation_id)),
        )
    except (RuntimeError, SQLAlchemyError, ValueError):
        parser.exit(1, "更正登记失败，事务未提交。请按原 operation_id 核对。\n")
    if result is None:
        parser.exit(1, "原取盘授权不符合更正条件，未登记替换。\n")
    print(result)


if __name__ == "__main__":
    main()
