from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

SCHEMA = "enterprise_backend"


def create_database_engine(url: str, **options: Any) -> AsyncEngine:
    if make_url(url).get_backend_name() == "postgresql":
        settings = options.setdefault("connect_args", {}).setdefault("server_settings", {})
        settings["search_path"] = SCHEMA
    else:
        options.setdefault("execution_options", {})["schema_translate_map"] = {SCHEMA: None}
    return create_async_engine(url, **options)
