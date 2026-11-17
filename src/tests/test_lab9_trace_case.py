"""Regression case from the first wrong step in C-002@11."""

import pytest

from lab8.shapes import SHAPES



@pytest.mark.student
def test_ticket_delay_cannot_override_order_record():
    from lab9.trace_case import investigate
    original_shapes = set(SHAPES)
    try:
        bad = investigate("seeded_regression", "C-002", 11)["trial"]
        good = investigate("fixed", "C-002", 11)["trial"]

        assert bad["events"][2]["kind"] == "model_override"
        assert bad["events"][2]["payload"]["delay_days"] == 5
        assert bad["snapshot"]["refund_vnd"] == 20000
        assert not bad["lab8_pass"]

        assert all(event["kind"] != "model_override" for event in good["events"])
        assert good["snapshot"]["refund_vnd"] == 0
        assert good["result"]["status"] == "denied"
        assert good["lab8_pass"]
    finally:
        for shape in set(SHAPES) - original_shapes:
            SHAPES.pop(shape, None)
