"""WMS Transport 提交协议常量;条款来源 wes_backend 的 transport-fulfillment-contract(0.4.0)。"""

OPERATION = "transport.task.submit@v1"
PROTOCOL = "wms-transport"
DEFAULT_SUBMIT_PATH = "/api/v1/wes/transport-requests"
# 按场景区分模板(用户 2026-10-22 指定):搬运只用 CTU01/CTU03/F01,换面只用 CTU02/CK04。
# 合同 0.4.0 §4.1 只列 CTU01/CTU02/CTU03/F01;CK04(转运货架换面)由现场确认补入,
# 合同文本尚未同步,WMS 若不接受会以 422 REJECTED 返回。
MOVE_TEMPLATES = frozenset({"CTU01", "CTU03", "F01"})
ROTATE_TEMPLATES = frozenset({"CTU02", "CK04"})
DEFAULT_MOVE_TEMPLATE = "F01"  # 换面不设默认:模板随货架角色而异

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
