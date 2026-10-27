"""兼容既有导入；纯 JSON 实现不加载应用基础设施。"""

from app.strict_json import canonical_digest, json_object

__all__ = ["canonical_digest", "json_object"]
