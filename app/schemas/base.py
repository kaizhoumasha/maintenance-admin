"""Pydantic 字段上的审计标记:Field(json_schema_extra=NO_AUDIT) 表示该字段的值不写进审计 detail。"""

NO_AUDIT = {"audit": False}
