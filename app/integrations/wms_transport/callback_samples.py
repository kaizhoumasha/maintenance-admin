"""联调用的样例回调:按任务的冻结事实构造一组合法的位置事件与结果消息。

只给 local 联调控制台用,不参与接收与应用。每条消息都会走真实的接收管线,所以这里只负责
「长得对」,是否被应用仍由 `callback/apply.py` 按判定表决定。
"""

POSITION_OPERATION = "transport.task.member_position_changed@v1"
RESULT_OPERATION = "transport.task.resulted@v1"
SCENARIOS = ("succeeded", "position_unknown")
BIN_KINDS = ("BIN_MOVE", "BIN_EXCHANGE")


def sample_callbacks(
    scenario: str, kind: str, transport_task_id: str, frozen: dict, revision: int
) -> list[tuple[str, dict]]:
    """返回 `(operation, data)` 列表,按应投递的先后顺序;`revision` 是结果版本号。"""
    base = {"transport_task_id": transport_task_id, "kind": kind, "outcome_revision": revision}
    unknown = scenario == "position_unknown"
    if kind in BIN_KINDS:
        moves = frozen["moves"]
        if unknown:
            results = [
                {
                    "container_id": move["container_id"],
                    "status": "FAILED",
                    "position_unknown": True,
                    "failure_code": "POSITION_UNKNOWN",
                }
                for move in moves
            ]
            return [(RESULT_OPERATION, {**base, "results": results})]
        placed = [
            (
                POSITION_OPERATION,
                {
                    "transport_task_id": transport_task_id,
                    "container_id": move["container_id"],
                    "milestone": "TARGET_PLACED",
                    "final_position": move["target"],
                },
            )
            for move in moves
        ]
        results = [
            {
                "container_id": move["container_id"],
                "status": "SUCCEEDED",
                "final_position": move["target"],
            }
            for move in moves
        ]
        return [*placed, (RESULT_OPERATION, {**base, "results": results})]
    rack = {**base, "rack_id": frozen["rack_id"]}
    if unknown:
        rack |= {"status": "FAILED", "position_unknown": True, "failure_code": "POSITION_UNKNOWN"}
    else:
        # 区域/货架类目标没有精确位置,用其编码占位,联调时按现场替换
        place = {"kind": "RACK_POSITION", "location_code": frozen["target"]["location_code"]}
        rack |= {"status": "SUCCEEDED", "final_position": place}
    return [(RESULT_OPERATION, rack)]
