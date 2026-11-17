from contextlib import closing

import pytest

from lab8.db import connect
from lab8.durability import InjectedCrash, committed_audit_count, get_refund
from lab8.policy import order_refunded_vnd
from lab8.shapes.workflow import run_claim


def test_workflow_quotes_from_order_and_effective_policy(public_db):
    with closing(connect(public_db)) as conn:
        # These policies would both qualify O-202 under their delay thresholds.
        # Leave the old policy superseded and mark the future policy current so
        # its effective date, rather than its flag, is what excludes it.
        conn.execute("UPDATE policies SET is_current = 1 WHERE policy_id = 'POL-2027-03'")
        conn.commit()

    # C-002 says five days in the ticket. The order records two.
    result = run_claim("C-002", seed=55, store_path=str(public_db))

    assert result.status == "denied"
    assert result.error_reason == "no_entitlement"
    assert result.proposal_amount == 0
    with closing(connect(public_db)) as conn:
        assert get_refund(conn, "C-002") is None
        assert committed_audit_count(conn, "C-002") == 0
        assert order_refunded_vnd(conn, "O-202") == 0


@pytest.mark.parametrize(
    ("crash_at", "total_at_crash"),
    [
        ("before_effect", 30000),
        ("after_effect_before_checkpoint", 150000),
        ("after_checkpoint", 150000),
    ],
)
def test_workflow_resumes_top_up_with_full_quote(public_db, crash_at, total_at_crash):
    store_path = str(public_db)
    with pytest.raises(InjectedCrash, match=crash_at):
        run_claim("C-014", seed=11, store_path=store_path, crash_at=crash_at)

    with closing(connect(public_db)) as conn:
        assert order_refunded_vnd(conn, "O-515") == total_at_crash

    resumed = run_claim("C-014", seed=11, store_path=store_path)
    replay = run_claim("C-014", seed=55, store_path=store_path)

    # Entitlement is 150,000 and a previous claim has already received 30,000.
    # The host must subtract that prior refund once and record only one top-up.
    for result in (resumed, replay):
        assert result.status == "resolved"
        assert result.proposal_amount == 150000
        assert [effect["amount_vnd"] for effect in result.refund_effects] == [120000]
    with closing(connect(public_db)) as conn:
        assert order_refunded_vnd(conn, "O-515") == 150000
        assert committed_audit_count(conn, "C-014") == 1
