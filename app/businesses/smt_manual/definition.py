"""Manual SMT resources; conveyor resources belong to BIN_Roller."""

from app.businesses.definitions import BusinessDefinition, PositionSlot

DEFINITION = BusinessDefinition(
    code="smt_manual",
    display_name="SMT 人工作业",
    position_slots=(
        PositionSlot(slot_key="RETURN_RACK", display_name="人工取料架位", required=False),
        PositionSlot(slot_key="TRANSFER_RACK", display_name="转运架位", required=False),
    ),
)
