"""Transport 回调在 `transport_task` 行上的应用规则(回调接收设计 §6)。

调用方已用 `SELECT ... FOR UPDATE` 锁住任务行;这里的方法只改这一行,JSON 列一律整体重新赋值。
**对已通过 DTO 校验的消息不得抛异常**:事实与冻结任务对不上、与已确定终态矛盾等一律用
`ctx.note(...)` 记诊断并按「已接纳、未应用」处理,调用方仍返回 202。
"""

from app.core.strict_json import canonical_digest as digest_of
from app.integrations.wms_callback import CallbackContext, Conflict, Duplicate
from app.models.transport import TransportTask

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
BIN_KINDS = ("BIN_MOVE", "BIN_EXCHANGE")


class TaskCallbacks:
    def __init__(self, task: TransportTask, ctx: CallbackContext):
        self.task, self.ctx = task, ctx
        self.frozen: dict = task.frozen or {}
        # 料箱任务的冻结成员 {container_id: 冻结目标位置}
        self.members = {m["container_id"]: m["target"] for m in self.frozen.get("moves", [])}

    def _skip(self, reason: str, **detail) -> None:
        self.ctx.note(reason, transport_task_id=self.task.transport_task_id, **detail)

    # ---- 位置事件 ----
    def apply_position(self, data: dict) -> None:
        """位置事实按料箱累积成集合,与到达先后无关;唯一的判定依赖是「已放到冻结目标」。"""
        container, milestone = data["container_id"], data["milestone"]
        target = self.members.get(container)
        if target is None:
            return self._skip("UNKNOWN_MEMBER", container_id=container)
        if milestone == "POSITION_UNKNOWN":  # 不存状态:任务是否 UNKNOWN 只由结果消息决定
            return self._skip("POSITION_UNKNOWN_REPORTED", container_id=container)
        placed = milestone == "TARGET_PLACED"
        if placed and data["final_position"] != target:
            return self._skip("TARGET_MISMATCH", container_id=container, data=data)
        positions = self.task.member_positions or {}
        facts = positions.get(container) or {}
        value = data["final_position"] if placed else True
        if facts.get(milestone) == value:  # 同一事实重复到达:幂等
            return
        self.task.member_positions = {**positions, container: {**facts, milestone: value}}
        self.complete_pending()

    # ---- 最终结果 ----
    def register_revision(self, data: dict) -> type[Duplicate | Conflict] | None:
        """同版本判重:相同规范化 data 返回 Duplicate,不同返回 Conflict;新版本记录摘要。"""
        digests = dict(self.task.outcome_digests or {})
        key, digest = str(data["outcome_revision"]), digest_of(data)
        if key in digests:
            return Duplicate if digests[key] == digest else Conflict
        digests[key] = digest
        self.task.outcome_digests = digests
        return None

    def apply_result(self, data: dict) -> None:
        """首次应用与完成暂存结果走同一个函数:版本、终态规则、前置位置每次都重做。"""
        revision, applied = data["outcome_revision"], self.task.applied_outcome_revision
        if applied is not None and revision <= applied:
            self._clear_pending_if_stale()
            return
        if self.task.outcome_status in TERMINAL:
            self.task.pending_outcome = None  # 终态不会被改写,暂存的更高版本再也用不上
            return self._skip("TERMINAL_CONFLICT", data=data)
        reason = self._mismatch(data)
        if reason:
            return self._skip(reason, data=data)
        if not self._prerequisites_met(data):
            pending = self.task.pending_outcome
            if pending is None or pending["revision"] <= revision:
                self.task.pending_outcome = {"revision": revision, "data": data}
            return
        outcome = self._normalized(data)
        self.task.outcome_status = self._status_of(outcome)
        self.task.applied_outcome = outcome
        self.task.applied_outcome_revision = revision
        determined = self.task.outcome_status in TERMINAL
        pending = self.task.pending_outcome
        if pending is not None and (determined or pending["revision"] <= revision):
            self.task.pending_outcome = None  # 已确定终态,或暂存的版本已不高于刚应用的版本

    def complete_pending(self) -> None:
        pending = self.task.pending_outcome
        if pending is not None:
            self.apply_result(pending["data"])

    def _clear_pending_if_stale(self) -> None:
        pending, applied = self.task.pending_outcome, self.task.applied_outcome_revision
        if pending is not None and applied is not None and pending["revision"] <= applied:
            self.task.pending_outcome = None
            self._skip("PENDING_SUPERSEDED", revision=pending["revision"])

    # ---- 判定表(设计 §6.2) ----
    def _mismatch(self, data: dict) -> str | None:
        if data["kind"] != self.task.kind:
            return "KIND_MISMATCH"
        if self.task.kind in BIN_KINDS:
            members = self.members
            if {item["container_id"] for item in data["results"]} != set(members):
                return "MEMBERS_MISMATCH"
            for item in data["results"]:
                if (
                    item["status"] == "SUCCEEDED"
                    and item["final_position"] != members[item["container_id"]]
                ):
                    return "TARGET_MISMATCH"
            return None
        if data["rack_id"] != self.frozen.get("rack_id"):
            return "RACK_MISMATCH"
        if data["status"] == "SUCCEEDED":
            target = self.frozen.get("target") or {}
            # RACK/ZONE 目标没有位置归属主数据可核对,信任 WMS/RCS 的解析
            if target.get("kind") == "RACK_POSITION" and data["final_position"] != target:
                return "TARGET_MISMATCH"
            face = self.frozen.get("target_face")
            if face and data["arrival_face"] and data["arrival_face"] != face:
                return "FACE_MISMATCH"
        return None

    def _prerequisites_met(self, data: dict) -> bool:
        """料箱成功回架的成员,要求其冻结目标上的 TARGET_PLACED 已应用。"""
        if self.task.kind not in BIN_KINDS:
            return True
        positions = self.task.member_positions or {}
        for item in data["results"]:
            if item["status"] != "SUCCEEDED":
                continue
            container_id = item["container_id"]
            target = self.members[container_id]
            if target["kind"] != "RACK_BIN_SLOT":
                continue
            if (positions.get(container_id) or {}).get("TARGET_PLACED") != target:
                return False
        return True

    def _normalized(self, data: dict) -> dict:
        outcome = dict(data)
        face = self.frozen.get("target_face")
        if self.task.kind not in BIN_KINDS and data["status"] == "SUCCEEDED" and face:
            outcome["arrival_face"] = data["arrival_face"] or face  # 成功缺省时用冻结目标面补齐
        return outcome

    @staticmethod
    def _status_of(outcome: dict) -> str:
        if outcome["kind"] in BIN_KINDS:
            results = outcome["results"]
            if any(item["position_unknown"] for item in results):
                return "UNKNOWN"
            return "FAILED" if any(item["status"] == "FAILED" for item in results) else "SUCCEEDED"
        if outcome["position_unknown"]:
            return "UNKNOWN"
        return outcome["status"]
