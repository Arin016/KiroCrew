"""RED test: validate_field accepts bool where a strict int is required.

bool is a subclass of int, so ``isinstance(True, int)`` is True. A bool
``task_id`` therefore slips through TASKKEEPER_COMPLETE_SCHEMA's int field
(True == 1, which is neither < min_val=1 nor > max_val=999999).
"""

from __future__ import annotations

import pytest

from kiro_claw.validation import (
    TASKKEEPER_COMPLETE_SCHEMA,
    FieldSpec,
    ValidationError,
    validate_field,
    validate_tool_args,
)


def test_agent_defect() -> None:
    # A bool must be rejected for a strict-int field, not silently accepted.
    with pytest.raises(ValidationError):
        validate_tool_args({"task_id": True}, TASKKEEPER_COMPLETE_SCHEMA)

    # Direct field-level check too (bool not in the spec's allowed types).
    spec = FieldSpec("task_id", int, required=True, min_val=1, max_val=999999)
    with pytest.raises(ValidationError):
        validate_field(True, spec)
    with pytest.raises(ValidationError):
        validate_field(False, spec)

    # Genuine bool fields must still accept bool values.
    bool_spec = FieldSpec("silent", bool)
    assert validate_field(True, bool_spec) is True
    assert validate_field(False, bool_spec) is False

    # Real integers still pass.
    assert validate_field(42, spec) == 42
