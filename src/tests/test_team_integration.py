"""Group integration check: three implementations must agree on the same input.

C-002 at seed 55 is especially useful: its ticket says five days late while
the authoritative order records two. The reference swarm gets this case wrong.
"""

from __future__ import annotations

import json
import time
from contextlib import closing

import pytest

from lab8.db import connect, init_store
from lab8.metrics import trial_pass
from lab8.shapes import SHAPES
from tests.conftest import BY_ID, CLAIMS, WORLD


@pytest.mark.parametrize(("claim_id", "seed"), [("C-001", 11), ("C-002", 55)])
def test_team_shapes_agree_on_result_and_write_valid_trace(tmp_path, claim_id, seed):
    outcomes = []
    for shape in ("workflow", "single", "orchestrator"):
        store = tmp_path / f"{shape}-{claim_id}-{seed}.sqlite"
        init_store(store, WORLD, CLAIMS).close()
        started_at = time.time()
        result = SHAPES[shape](claim_id, seed=seed, store_path=str(store))

        with closing(connect(store)) as conn:
            passed, reason = trial_pass(
                result.as_dict(), BY_ID[claim_id]["expected"], conn, started_at=started_at
            )
        assert passed, f"{shape}: {reason}"

        with open(result.trace_path, encoding="utf-8") as stream:
            trace = json.load(stream)
        assert trace["run_id"] == result.run_id
        outcomes.append(
            (
                result.status,
                result.proposal_amount,
                tuple(effect["amount_vnd"] for effect in result.refund_effects),
            )
        )

    assert outcomes[0] == outcomes[1] == outcomes[2]
