"""Shape B — one bounded ReAct agent with host-only refund writes."""

from __future__ import annotations

import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.mock_llm import complete
from lab8.runtime import run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import tool_cost
from lab8.trace import Trace


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    """Process a claim through one ReAct loop; the host alone may issue refunds."""
    shape = "single"
    run_id = make_run_id(shape, claim_id, seed)
    trace = Trace(run_id)
    budget = Budget()
    conn: sqlite3.Connection | None = None
    proposal_amount = 0

    # The closure retains only the allowlisted results needed to make the next
    # tool choice. Ticket text is never used to calculate compensation.
    state: dict[str, Any] = {"claim_id": claim_id}

    def decide(observation: dict[str, Any]) -> dict[str, Any]:
        last_tool = observation.get("last_tool")
        last_result = observation.get("last_result") or {}

        # The mock model records one model call per ReAct decision. Its output
        # is not treated as authorization or as a source of monetary values.
        prompt = (
            f"shape=single; claim_id={claim_id}; "
            f"last_tool={last_tool or 'start'}; "
            f"result_keys={sorted(last_result.keys()) if isinstance(last_result, dict) else []}"
        )
        complete(budget, seed=seed, purpose="react", prompt=prompt)

        if last_tool is None:
            return {"tool": "get_claim_view", "args": {"claim_id": claim_id}}

        if last_tool == "get_claim_view":
            claim = last_result
            state["claim"] = claim
            if not claim.get("found"):
                return {
                    "final": {
                        "status": "failed",
                        "proposal_amount": 0,
                        "error_reason": "unknown_claim",
                    }
                }
            if not claim.get("order_id"):
                return {
                    "final": {
                        "status": "needs_info",
                        "proposal_amount": 0,
                        "error_reason": "missing_order_id",
                    }
                }
            return {
                "tool": "lookup_order",
                "args": {
                    "order_id": claim.get("order_id"),
                    "principal_id": claim.get("principal_id"),
                },
            }

        if last_tool == "lookup_order":
            lookup = last_result
            state["lookup"] = lookup
            if not lookup.get("visible"):
                return {
                    "final": {
                        "status": "escalated",
                        "proposal_amount": 0,
                        "error_reason": str(lookup.get("reason", "order_not_visible")),
                    }
                }
            return {
                "tool": "lookup_refund_history",
                "args": {
                    "order_id": (lookup.get("order") or {}).get("order_id"),
                    "principal_id": state["claim"].get("principal_id"),
                },
            }

        if last_tool == "lookup_refund_history":
            history = last_result
            state["history"] = history
            if not history.get("visible"):
                return {
                    "final": {
                        "status": "escalated",
                        "proposal_amount": 0,
                        "error_reason": str(history.get("reason", "order_not_visible")),
                    }
                }
            order = (state.get("lookup") or {}).get("order") or {}
            return {"tool": "compute_compensation", "args": {"order": order}}

        if last_tool == "compute_compensation":
            quote = last_result
            state["quote"] = quote
            amount = int(quote.get("amount_vnd") or 0)
            if amount <= 0:
                return {
                    "final": {
                        "status": "denied",
                        "proposal_amount": amount,
                        "error_reason": str(quote.get("reason", "zero_compensation")),
                    }
                }
            return {
                "final": {
                    "status": "proposed",
                    "proposal_amount": amount,
                    "error_reason": None,
                }
            }

        return {
            "final": {
                "status": "failed",
                "proposal_amount": 0,
                "error_reason": "unexpected_react_state",
            }
        }

    try:
        conn = connect(store_path)

        loop = run_react(
            conn,
            budget,
            observation={"claim": {"claim_id": claim_id}},
            decide=decide,
            max_steps=8,
            # Restrict the model to the read-tool allowlist, just like a worker.
            worker=True,
        )
        final = loop.get("final") or {
            "status": "failed",
            "proposal_amount": 0,
            "error_reason": "step_cap",
        }
        status = str(final.get("status", "failed"))
        proposal_amount = int(final.get("proposal_amount") or 0)
        error_reason = final.get("error_reason")

        trace.add(
            "react_loop",
            {
                "stopped": loop.get("stopped"),
                "model_calls": budget.model_calls,
                "tool_calls": budget.tool_calls,
            },
        )

        claim = state.get("claim") or {}
        trace.add(
            "claim_read",
            {
                "found": bool(claim.get("found")),
                "claim_id": claim_id,
                "order_id": claim.get("order_id"),
            },
        )

        lookup = state.get("lookup") or {}
        if lookup:
            trace.add(
                "order_lookup",
                {
                    "visible": bool(lookup.get("visible")),
                    "reason": lookup.get("reason"),
                    "order_id": claim.get("order_id"),
                },
            )

        history = state.get("history") or {}
        if history:
            trace.add(
                "refund_history",
                {
                    "visible": bool(history.get("visible")),
                    "refund_count": history.get("refund_count"),
                    "refunded_vnd": history.get("refunded_vnd"),
                },
            )

        quote = state.get("quote") or {}
        if quote:
            trace.add(
                "compensation_quote",
                {
                    "amount_vnd": proposal_amount,
                    "reason": quote.get("reason"),
                    "clause_ids": quote.get("clause_ids", []),
                    "parts": quote.get("parts", {}),
                },
            )

        # Draft only after a completed decision. Exhausting the ReAct budget
        # must not trigger another model call or a refund attempt.
        if loop.get("stopped") == "final":
            complete(
                budget,
                seed=seed,
                purpose="draft",
                prompt=f"claim_id={claim_id}; status={status}; proposal={proposal_amount}",
            )

        if status == "proposed":
            # The host counts and performs the write. Pass the FULL policy
            # quote; durability.issue_refund enforces authorization and caps.
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
