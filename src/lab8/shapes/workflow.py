"""Shape A — deterministic workflow."""

from __future__ import annotations

import sqlite3

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import compute_compensation, get_claim_view, lookup_order, tool_cost
from lab8.trace import Trace


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    """Resolve a claim from structured order data and the host's refund decision."""
    shape = "workflow"
    run_id = make_run_id(shape, claim_id, seed)
    trace = Trace(run_id)
    budget = Budget()
    proposal_amount = 0
    conn: sqlite3.Connection | None = None

    def complete(connection: sqlite3.Connection, status: str, reason: str | None) -> RunResult:
        trace.add("decision", {"status": status, "reason": reason})
        return finish(
            connection,
            shape=shape,
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal_amount,
            budget=budget,
            trace=trace,
            error_reason=reason,
        )

    try:
        conn = connect(store_path)
        budget.add_tool(tool_cost())
        claim = get_claim_view(conn, claim_id)
        trace.add("claim_read", {"found": claim["found"]})
        if not claim["found"]:
            return complete(conn, "failed", "unknown_claim")

        order_id = claim["order_id"]
        if not order_id:
            return complete(conn, "needs_info", "missing_order_id")

        budget.add_tool(tool_cost())
        lookup = lookup_order(conn, order_id, claim["principal_id"])
        trace.add("order_lookup", {"visible": lookup["visible"], "reason": lookup["reason"]})
        if not lookup["visible"]:
            return complete(conn, "escalated", lookup["reason"])

        # The adapter selects the effective policies and quotes the scoped order.
        budget.add_tool(tool_cost())
        quote = compute_compensation(conn, lookup["order"])
        proposal_amount = quote["amount_vnd"]
        trace.add("compensation_quote", quote)
        if proposal_amount == 0:
            return complete(conn, "denied", quote["reason"])

        # The host applies approval, earlier refunds on the order, and idempotency.
        budget.add_tool(tool_cost())
        refund = issue_refund(conn, claim_id, proposal_amount, crash_at=crash_at)
        trace.add(
            "refund_attempt",
            {"ok": refund["ok"], "reason": refund["reason"], "conflict": refund["conflict"]},
        )
        if refund["conflict"]:
            return complete(conn, "failed", refund["reason"])
        if not refund["ok"]:
            return complete(conn, "denied", refund["reason"])
        return complete(conn, "resolved", None)

    except InjectedCrash:
        raise
    except Exception as exc:
        reason = type(exc).__name__
        trace.add("error", {"type": reason})
        if conn is not None:
            try:
                return complete(conn, "failed", reason)
            except sqlite3.Error:
                # An unreadable store cannot provide ledger or audit details.
                pass
        return RunResult(
            run_id=run_id,
            claim_id=claim_id,
            shape=shape,
            seed=seed,
            status="failed",
            proposal_amount=proposal_amount,
            refund_effects=[],
            committed_audit_count=0,
            model_calls=0,
            tool_calls=budget.tool_calls,
            input_tokens=0,
            output_tokens=0,
            cost_estimate=round(budget.cost_estimate, 6),
            trace_path=trace.write(),
            error_reason=reason,
        )
    finally:
        if conn is not None:
            conn.close()
