"""WMS Transport 提交协议常量;条款来源 wes_backend 的 transport-fulfillment-contract(0.4.0)。"""

OPERATION = "transport.task.submit@v1"
PROTOCOL = "wms-transport"
DEFAULT_SUBMIT_PATH = "/api/v1/wes/transport-requests"

# 合同 §4.2:最后一项是已确认的完整线上字面量,逐字保持,不得改写或拆分匹配。
REJECTION_REASONS = frozenset(
    {
        "INVALID_ENVELOPE",
        "UNSUPPORTED_OPERATION",
        "INVALID_DATA",
        "COORDINATED_BIN_EXCHANGE_UNSUPPORTED",
        "There are other unfinished tasks on the shelf任务下发失败："
        "There are other unfinished tasks on the shelf",
    }
)
