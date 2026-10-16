import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core import AppError
from app.models import User
from app.schemas.base import DatabaseId
from app.schemas.query import CursorParams, FilterGroup, UserListParams, parse_filter
from app.services.filtering import FieldSpec, apply_filter


def group(*conditions, couple="and"):
    return {"couple": couple, "conditions": list(conditions)}


def condition(field="username", op="eq", value="Alpha"):
    return {"field": field, "op": op, "value": value}


def parse(value):
    return parse_filter(json.dumps(value, ensure_ascii=False))


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        None,
        group(),
        group(condition(), condition(), couple="not"),
        group(couple="not"),
        {**group(condition()), "coupl": "or"},
        group({**condition(), "extra": "hidden-value"}),
        group({**group(condition()), "extra": 1}),
        group({**condition(), **group(condition())}),
        group(condition(op="in", value=[])),
        group(condition(op="in", value=[None])),
        group(condition(op="in", value=list(range(101)))),
        group(condition(op="between", value=[1])),
        group(condition(op="between", value=[])),
        group(condition(op="between", value=[1, None])),
        group(condition(op="between", value=[1, 2, 3])),
        group(condition(value=None)),
        group(condition(value={})),
        group(condition(op="is_null", value=1)),
        group(condition(op="ilike", value="a" * 65)),
        group(condition(op="ilike", value="abc\\")),
    ],
)
def test_rejects_invalid_filter_structure(value):
    with pytest.raises(AppError) as error:
        parse(value)
    assert "hidden-value" not in str(error.value)
    assert error.value.code == "INVALID_REQUEST"


def test_depth_and_raw_boundaries():
    value = group(group(group(condition())))
    assert isinstance(parse(value), FilterGroup)
    with pytest.raises(AppError):
        parse(group(value))
    assert parse_filter(None) is None
    for raw in ("", "null", "[" * 900 + "]" * 900, "bad-json", "\ud800"):
        with pytest.raises(AppError):
            parse_filter(raw)
    raw = json.dumps(group(condition(value="汉")), ensure_ascii=False)
    raw += " " * (2048 - len(raw.encode()))
    assert parse_filter(raw)
    with pytest.raises(AppError):
        parse_filter(raw + " ")


@pytest.mark.parametrize("count", [1, 100])
def test_in_accepts_boundary_lengths(count):
    parsed = parse(group(condition(op="in", value=["a"] * count)))
    assert len(parsed.conditions[0].value) == count


def test_complete_query_models():
    from pydantic import ValidationError

    assert UserListParams(filter="{}", page=2).page == 2
    assert CursorParams(filter="{}", cursor="abc").limit == 20
    for cls, params in (
        (CursorParams, {"page": 1}),
        (UserListParams, {"filer": "{}"}),
        (CursorParams, {"limit": 0}),
        (CursorParams, {"limit": 101}),
        (CursorParams, {"cursor": "x" * 65}),
        (UserListParams, {"filter": "汉" * 683}),
    ):
        with pytest.raises(ValidationError):
            cls(**params)


SPECS = {
    "username": FieldSpec(User.username, str, ("eq", "in", "ilike")),
    "is_active": FieldSpec(User.is_active, bool, ("eq",)),
    "id": FieldSpec(User.id, DatabaseId, ("eq", "in", "is_null", "not_null")),
    "created_at": FieldSpec(User.created_at, datetime, ("ge", "le", "between")),
}


@pytest.mark.parametrize(
    ("field", "op", "value"),
    [
        ("unknown", "eq", "x"),
        ("hashed_password", "eq", "x"),
        ("username", "ge", "x"),
        ("username", "eq", 1),
        ("username", "eq", "x\x00"),
        ("id", "eq", True),
        ("id", "eq", 1.0),
        ("id", "eq", "1"),
        ("id", "eq", 0),
        ("id", "eq", 2147483648),
        ("id", "in", [1, False]),
        ("is_active", "eq", 1),
        ("is_active", "eq", "true"),
        ("created_at", "ge", 1700000000),
        *[
            ("created_at", "ge", v)
            for v in (
                "1700000000",
                "1700000000.5",
                "1700000000000",
                "20261017",
                "2026-10-17",
                "2026-10-17T12:00:00",
                "9999-12-31T23:59:59-01:00",
                "0001-01-01T00:00:00+01:00",
            )
        ],
        ("created_at", "between", ["2026-10-17T00:00:00Z", "9999-12-31T23:59:59-01:00"]),
    ],
)
def test_rejects_field_operator_and_values(field, op, value):
    with pytest.raises(AppError) as error:
        apply_filter(select(User), parse(group(condition(field, op, value))), SPECS)
    assert error.value.code == "INVALID_REQUEST"
    assert "9999" not in (error.value.detail or "")


async def test_operators_and_groups_return_matching_rows(db):
    db.add_all(
        [
            User(
                username=name,
                email=f"{i}@e.test",
                hashed_password="x",
                is_active=active,
                created_at=datetime(2026, 10, day, tzinfo=UTC),
            )
            for i, (name, active, day) in enumerate(
                [("Alpha", True, 15), ("a_b%\\", False, 16), ("Beta", True, 17)]
            )
        ]
    )
    await db.commit()
    cases = [
        (condition(), ["Alpha"]),
        (condition(op="in", value=["Alpha", "Beta"]), ["Alpha", "Beta"]),
        (condition(op="ilike", value="a%"), ["Alpha", "a_b%\\"]),
        (condition(op="ilike", value=r"a\_b\%\\"), ["a_b%\\"]),
        (condition("is_active", "eq", False), ["a_b%\\"]),
        (condition("id", "is_null", None), []),
        (condition("id", "not_null", None), ["Alpha", "a_b%\\", "Beta"]),
        (condition("created_at", "ge", "2026-10-16T00:00:00Z"), ["a_b%\\", "Beta"]),
        (condition("created_at", "le", "2026-10-15T08:00:00+08:00"), ["Alpha"]),
        (
            condition("created_at", "between", ["2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z"]),
            ["Alpha", "a_b%\\"],
        ),
        (group(condition(), condition(value="Beta"), couple="or"), ["Alpha", "Beta"]),
        (group(condition(), couple="not"), ["a_b%\\", "Beta"]),
    ]
    for node, expected in cases:
        query = apply_filter(select(User), parse(group(node)), SPECS).order_by(User.id)
        assert [u.username for u in await db.scalars(query)] == expected
    stmt = select(User)
    assert apply_filter(stmt, None, SPECS) is stmt


def test_unknown_and_forbidden_fields_have_identical_errors():
    messages = []
    for name in ("hashed_password", "detail", "unknown"):
        with pytest.raises(AppError) as error:
            apply_filter(select(User), parse(group(condition(name))), SPECS)
        messages.append((error.value.message, error.value.detail))
    assert len(set(messages)) == 1


# 新增算子时:FilterOp、check_value、filtering._CLAUSES 三处必须同步;这里按 FilterOp 逐个验证
_SAMPLES = {
    "eq": "x",
    "in": ["x"],
    "ilike": "x%",
    "ge": "2026-01-01T00:00:00Z",
    "le": "2026-01-01T00:00:00Z",
    "between": ["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"],
    "is_null": None,
    "not_null": None,
}


def test_every_op_is_handled():
    from typing import get_args

    from app.schemas.query import FilterOp
    from app.services.filtering import _CLAUSES

    ops = set(get_args(FilterOp))
    assert ops == set(_CLAUSES) == set(_SAMPLES)
    for op in ops:
        field, value_type = (
            ("created_at", datetime)
            if op in ("ge", "le", "between")
            else (
                "username",
                str,
            )
        )
        spec = {field: FieldSpec(getattr(User, field), value_type, (op,))}
        node = group(condition(field, op, _SAMPLES[op]))
        stmt = apply_filter(select(User), parse(node), spec)
        assert "WHERE" in str(stmt), op
