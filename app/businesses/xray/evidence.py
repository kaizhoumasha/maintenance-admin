"""Correlate physical evidence with this reel's original actions."""

from .contracts import BinActual, RackActual


def select_events(events):
    facts, requests = {}, {}
    for event in events:
        payload = event.payload
        if event.stage == "NG_REQUEST":
            by_key, key = requests, payload["sourceEventId"]
        else:
            by_key, key = facts, event.stage
        previous = by_key.setdefault(key, event)
        if previous is not event and without_event_id(previous.payload) != without_event_id(
            payload
        ):
            return facts, requests, "STAGE_EVENT_CONFLICT"
    return facts, requests, None


def without_event_id(payload):
    return {name: value for name, value in payload.items() if name != "eventId"}


def command_evidence(reel, actions, views):
    for stage, action in actions.items():
        view = views[stage]
        if view is None or view.needs_reconciliation:
            return "COMMAND_EVIDENCE_UNAVAILABLE"
        if not view.result:
            continue
        data = view.result["data"]
        if data["reelId"] != reel.reel_id:
            return "RESULT_REEL_CONFLICT"
        if view.result["result"] != "SUCCESS":
            continue
        params = action["params"]
        if "decision" in params and data.get("decision") != params["decision"]:
            return "RESULT_DECISION_CONFLICT"
        if stage == "OUTLET":
            model = RackActual if "rack_locator" in action else BinActual
            target = {key: params["target"][key] for key in model.model_fields}
            if data["actualTarget"] != target:
                return "RESULT_TARGET_CONFLICT"
    return None


def terminal(views, key):
    view = views.get(key)
    return view.result if view else None


def failed(result):
    return result is not None and result["result"] == "FAILED"


def succeeded(result):
    return result is not None and result["result"] == "SUCCESS"


def event_actions(facts):
    if "COUNT" in facts:
        yield "ENTRY"
    if "VERIFY" in facts:
        yield "COUNT"
    if "OUTLET" in facts:
        yield "ENTRY" if facts["OUTLET"].payload["mode"] == "BYPASS" else "VERIFY"


def fact_conflict(facts, actions, views):
    entry = facts["ENTRY"].payload
    for phase in ("COUNT", "VERIFY", "OUTLET", "NG"):
        event = facts.get(phase)
        if (
            event
            and entry.get("barcode")
            and "barcode" in event.payload
            and (entry["barcode"] != event.payload["barcode"])
        ):
            return "EVENT_BARCODE_CONFLICT"
    count, verify, outlet = (facts.get(key) for key in ("COUNT", "VERIFY", "OUTLET"))
    if entry["scanStatus"] == "NG" and (count or verify or outlet):
        return "ENTRY_EVIDENCE_CONFLICT"
    entry_decision = actions.get("ENTRY", {}).get("params", {}).get("decision")
    if (count or verify) and entry_decision == "BYPASS_COUNT":
        return "EVENT_ROUTE_CONFLICT"
    if count and entry_decision == "COUNT_REQUIRED":
        result = terminal(views, "ENTRY")
        ok = count.payload["countStatus"] == "OK"
        if (failed(result) and ok) or (
            succeeded(result) and (not ok or count.payload["qty"] != result["data"]["qty"])
        ):
            return "COUNT_EVIDENCE_CONFLICT"
    if verify and failed(terminal(views, "COUNT")):
        return "LABEL_EVIDENCE_CONFLICT"
    if outlet:
        mode = outlet.payload["mode"]
        if (mode == "BYPASS" and entry_decision == "COUNT_REQUIRED") or (
            mode == "COUNTED" and entry_decision == "BYPASS_COUNT"
        ):
            return "OUTLET_MODE_CONFLICT"
        if (
            mode == "COUNTED"
            and verify
            and (
                verify.payload["scanStatus"] != "OK"
                or outlet.payload["new_barcode"] != verify.payload.get("new_barcode")
            )
        ):
            return "OUTLET_BARCODE_CONFLICT"
        original_stage = "ENTRY" if mode == "BYPASS" else "VERIFY"
        if failed(terminal(views, original_stage)):
            return "OUTLET_EVIDENCE_CONFLICT"
    return None


def ng_conflicts(stage, facts, actions, views):
    if stage == "ENTRY":
        return "COUNT" in facts or "VERIFY" in facts or succeeded(terminal(views, "ENTRY"))
    if stage in {"COUNT", "LABEL"}:
        if actions.get("ENTRY", {}).get("params", {}).get("decision") == "BYPASS_COUNT":
            return True
        return "VERIFY" in facts or succeeded(terminal(views, "COUNT"))
    return actions.get("ENTRY", {}).get("params", {}).get("decision") == "BYPASS_COUNT"
