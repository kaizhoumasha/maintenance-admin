import re

# 合同里的 operation 名:`域.名称@vN`,小写字母数字下划线,点分层。入站与出站两套注册表共用。
OPERATION_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+@v[1-9][0-9]*$")
MAX_OPERATION_NAME_CHARS = 256


def is_operation_name(name: str) -> bool:
    return len(name) <= MAX_OPERATION_NAME_CHARS and OPERATION_NAME.fullmatch(name) is not None
