"""Shape C — orchestrator plus read-only investigators."""

from __future__ import annotations

import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.handoff import make_handoff
from lab8.mock_llm import complete
from lab8.runtime import run_readonly_investigators
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import get_claim_view, tool_cost
from lab8.trace import Trace


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    """Coordinate read-only investigators; only this host writes a refund."""
    shape = "orchestrator"
    run_id = make_run_id(shape, claim_id, seed)
    trace = Trace(run_id)
    budget = Budget()
    conn: sqlite3.Connection | None = None
    proposal_amount = 0

    try:
        conn = connect(store_path)

        # Read only the claim fields needed to establish the principal scope.
        # The ticket is deliberately not copied into the handoff or trace.
        budget.add_tool(tool_cost())
        claim = get_claim_view(conn, claim_id)

        trace.add(
            "claim_read",
            {
                "found": bool(claim.get("found")),
                "claim_id": claim_id,
                "order_id": claim.get("order_id"),
            },
        )

        if not claim.get("found"):
            status = "failed"
            error_reason = "unknown_claim"
            return finish(
                conn,
                shape=shape,
                claim_id=claim_id,
                seed=seed,
                status=status,
                proposal_amount=0,
                budget=budget,
                trace=trace,
                error_reason=error_reason,
            )

        if not claim.get("order_id"):
            return finish(
                conn,
                shape=shape,
                claim_id=claim_id,
                seed=seed,
                status="needs_info",
                proposal_amount=0,
                budget=budget,
                trace=trace,
                error_reason="missing_order_id",
            )

        # make_handoff applies a strict field allowlist and rejects write tools.
        handoff = make_handoff(
            request_id=run_id,
            claim_id=claim_id,
            task=(
                "Verify the claim using the owner's order, current policy, "
                "and refund history. Return structured evidence only."
            ),
            facts={},
            allowed_tools=[
                "get_claim_view",
                "lookup_order",
                "lookup_policy",
                "lookup_refund_history",
                "compute_compensation",
            ],
            max_output_tokens=400,
        )
        trace.add("handoff", handoff)

        # The peer model call is for coordination only; it does not authorize
        # a refund or provide a monetary amount.
        complete(
            budget,
            seed=seed,
            purpose="peer",
            prompt=(
                f"request_id={run_id}; claim_id={claim_id}; "
                f"allowed_tools={handoff['allowed_tools']}"
            ),
        )

        investigation = run_readonly_investigators(
            conn,
            budget,
            handoff,
            principal_id=str(claim["principal_id"]),
        )
        tools: dict[str, Any] = investigation.get("tools") or {}

        order_result = tools.get("lookup_order") or {}
        history_result = tools.get("lookup_refund_history") or {}
        quote_result = tools.get("compute_compensation") or {}

        # Trace only the structured, allowlisted investigator results.
        order = order_result.get("order") or {}
        trace.add(
            "investigator_results",
            {
                "claim_view": investigation.get("claim_view") or {},
                "order": order,
                "order_visible": bool(order_result.get("visible")),
                "order_reason": order_result.get("reason"),
                "history_visible": bool(history_result.get("visible")),
                "refund_count": history_result.get("refund_count"),
                "refunded_vnd": history_result.get("refunded_vnd"),
                "quote": quote_result,
            },
        )

        if not order_result.get("visible"):
            status = "escalated"
            error_reason = str(order_result.get("reason", "order_not_visible"))
        elif not history_result.get("visible"):
            status = "escalated"
            error_reason = str(history_result.get("reason", "refund_history_not_visible"))
        elif not quote_result:
            status = "failed"
            error_reason = "missing_compensation_quote"
        else:
            proposal_amount = int(quote_result.get("amount_vnd") or 0)
            if proposal_amount <= 0:
                status = "denied"
                error_reason = str(quote_result.get("reason", "zero_compensation"))
            else:
                status = "proposed"
                error_reason = None

        # Drafting is informational only and cannot alter the host decision.
        complete(
            budget,
            seed=seed,
            purpose="draft",
            prompt=(
                f"claim_id={claim_id}; status={status}; "
                f"proposal={proposal_amount}"
            ),
        )

        if status == "proposed":
            # Pass the full policy quote. The host enforces approval,
            # per-order caps, and idempotency inside issue_refund().
            budget.add_tool(tool_cost())
            refund = issue_refund(
                conn,
                claim_id,
                proposal_amount,
                crash_at=crash_at,
            )
            trace.add(
                "refund_attempt",
                {
                    "ok": bool(refund.get("ok")),
                    "reason": refund.get("reason"),
                    "conflict": bool(refund.get("conflict")),
                    "amount_vnd": (
                        refund.get("effect", {}).get("amount_vnd")
                        if refund.get("effect")
                        else None
                    ),
                },
            )

            if refund.get("conflict"):
                status = "failed"
                error_reason = str(refund.get("reason", "amount_conflict"))
            elif not refund.get("ok"):
                status = "denied"
                error_reason = str(refund.get("reason", "policy_denied"))
            else:
                status = "resolved"
                error_reason = None

        trace.add("decision", {"status": status, "reason": error_reason})
        return finish(
            conn,
            shape=shape,
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal_amount,
            budget=budget,
            trace=trace,
            error_reason=error_reason,
        )

    except InjectedCrash as exc:
        trace.add("crash", {"point": str(exc)})
        trace.write()
        raise
    except Exception as exc:
        trace.add("error", {"type": type(exc).__name__})
        if conn is not None:
            return finish(
                conn,
                shape=shape,
                claim_id=claim_id,
                seed=seed,
                status="failed",
                proposal_amount=proposal_amount,
                budget=budget,
                trace=trace,
                error_reason=type(exc).__name__,
            )

        trace_path = trace.write()
        return RunResult(
            run_id=run_id,
            claim_id=claim_id,
            shape=shape,
            seed=seed,
            status="failed",
            proposal_amount=proposal_amount,
            refund_effects=[],
            committed_audit_count=0,
            model_calls=budget.model_calls,
            tool_calls=budget.tool_calls,
            input_tokens=budget.input_tokens,
            output_tokens=budget.output_tokens,
            cost_estimate=round(budget.cost_estimate, 6),
            trace_path=trace_path,
            error_reason=type(exc).__name__,
        )
    finally:
        if conn is not None:
            conn.close()
