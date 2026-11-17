"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, load_spec, per_item


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    problems: list[str] = []

    for label, report in (("PR", pr), ("baseline", base)):
        manifest = report.get("manifest") or {}
        rows = report.get("rows") or []
        seeds = manifest.get("seeds") or []
        items = manifest.get("items")
        if not isinstance(items, int) or items < 1 or not seeds:
            problems.append(f"{label}: invalid manifest items/seeds")
            continue
        if len(seeds) != len(set(seeds)):
            problems.append(f"{label}: duplicate manifest seed")
        if len(rows) != items * len(seeds):
            problems.append(f"{label}: row count {len(rows)} != {items} × {len(seeds)}")
        keys = [(r.get("item_id"), r.get("seed")) for r in rows]
        if len(keys) != len(set(keys)):
            problems.append(f"{label}: duplicate (item_id, seed) row")
        if {r.get("item_id") for r in rows} and len({r.get("item_id") for r in rows}) != items:
            problems.append(f"{label}: item count differs from manifest")
        if {r.get("seed") for r in rows} != set(seeds):
            problems.append(f"{label}: row seeds differ from manifest")
        if report.get("evaluator_failures"):
            problems.append(f"{label}: evaluator failures present")
        for metric, value in (report.get("metrics") or {}).items():
            if isinstance(value, float) and not math.isfinite(value):
                problems.append(f"{label}: {metric} is NaN or infinite")

    pr_rows = pr.get("rows") or []
    base_rows = base.get("rows") or []
    pr_by_key = {(r.get("item_id"), r.get("seed")): r for r in pr_rows}
    base_by_key = {(r.get("item_id"), r.get("seed")): r for r in base_rows}
    if set(pr_by_key) != set(base_by_key):
        problems.append("PR and baseline have different (item_id, seed) sets")
    for key in set(pr_by_key) & set(base_by_key):
        if set(pr_by_key[key].get("slices") or []) != set(base_by_key[key].get("slices") or []):
            problems.append(f"{key}: slice tags differ from baseline")

    for m in spec["metrics"]:
        mid = m["id"]
        if mid not in (pr.get("metrics") or {}):
            problems.append(f"PR: missing metric {mid}")
        # Aggregate-only metrics (such as pass_hat_5) do not have a per-trial row.
        if any(mid in row for row in base_rows):
            if any(row.get(mid) is None or
                   (isinstance(row.get(mid), float) and not math.isfinite(row[mid]))
                   for row in pr_rows):
                problems.append(f"PR: missing or invalid row value for {mid}")

    if base_spec is not None and spec.get("eval_set_version") == base_spec.get("eval_set_version"):
        def compare_thresholds(old: dict, new: dict, name: str) -> None:
            old_th, new_th = old.get("threshold", old), new.get("threshold", new)
            if old.get("blocking") and not new.get("blocking"):
                problems.append(f"ratchet: {name} changed from blocking to non-blocking")
            if "min" in old_th and ("min" not in new_th or new_th["min"] < old_th["min"]):
                problems.append(f"ratchet: {name} minimum loosened")
            if "max" in old_th and ("max" not in new_th or new_th["max"] > old_th["max"]):
                problems.append(f"ratchet: {name} maximum loosened")
            if "max_increase" in old_th and ("max_increase" not in new_th or
                                              new_th["max_increase"] > old_th["max_increase"]):
                problems.append(f"ratchet: {name} max increase loosened")
            if old_th.get("no_significant_drop") and not new_th.get("no_significant_drop"):
                problems.append(f"ratchet: {name} dropped no_significant_drop")

        current = {m["id"]: m for m in spec.get("metrics", [])}
        for old in base_spec.get("metrics", []):
            name = old["id"]
            if name not in current:
                problems.append(f"ratchet: metric {name} removed")
            else:
                compare_thresholds(old, current[name], name)
        current_slices = {(s["name"], s["metric"]): s for s in spec.get("slices", [])}
        for old in base_spec.get("slices", []):
            key = (old["name"], old["metric"])
            if key not in current_slices:
                problems.append(f"ratchet: slice {key} removed")
            else:
                compare_thresholds(old, current_slices[key], f"slice {key}")
    return problems


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for s in spec.get("slices", []):
        name, metric = s["name"], s["metric"]
        ident = f"slice:{name}/{metric}"
        pr_items = per_item(pr, metric, slice_name=name)
        base_items = per_item(base, metric, slice_name=name)
        if not pr_items or set(pr_items) != set(base_items):
            if s.get("blocking"):
                blocked.append(ident)
            lines.append(f"  {ident} missing or mismatched slice rows BLOCK")
            continue
        value = sum(pr_items.values()) / len(pr_items)
        fail = "min" in s and value < s["min"]
        note = ""
        if s.get("no_significant_drop"):
            keys = sorted(pr_items)
            delta, lo, hi = stats.paired_bootstrap([pr_items[k] for k in keys],
                                                  [base_items[k] for k in keys],
                                                  n_resamples=bs.get("n_resamples", 1000),
                                                  rng=bs.get("rng", 2027))
            note = f" Δ {delta:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            fail = fail or hi < 0
        if fail and s.get("blocking"):
            blocked.append(ident)
        lines.append(f"  {ident:<30} {value:.4f} min {s.get('min', '—')}{note} " +
                     ("BLOCK" if fail and s.get("blocking") else "warn" if fail else "ok"))
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid):
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
            note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            if hi < 0:
                verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
