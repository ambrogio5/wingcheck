"""
model_comparison_sia.py - RESEARCH-ONLY comparison of the production
baseline against a fresh model trained on the new SIA-first labels.
Never writes weights.json (asserted in main()).

Honest scope, decided by the data actually available (see the PR /
docs/DATA_ARCHITECTURE.md): SIA wind observations exist only for
2026-01-01..2026-07-16, so SIA-first labels cover only 535 of the 3,111
backtest feature rows (May-Jul 2026, one partial season). That makes the
full multi-year rolling-origin protocol impossible for now - what CAN be
run honestly is:

  Model A (baseline reference): the CURRENT production weights scored
    against the SIA-labeled rows. NOT a fair holdout - production weights
    were trained on these same hours under Samedan-proxy labels - so A's
    numbers are a reference point for "what the deployed model would have
    said", never a claim of generalization.
  Model B (label-improvement experiment): model.new_weights() trained
    from scratch on SIA-labeled rows only, evaluated with day-grouped
    chronological expanding-window folds INSIDE the labeled span
    (train May -> validate June; train May+June -> validate July). Every
    fold trains strictly before its validation days; no calendar day
    crosses the boundary; label source/policy recorded per fold.

Both models use the exact same 22 production features - this isolates the
LABEL change alone (Part 17's Model B), which is the only comparison the
current label coverage can support. Models C/D/E (SIA issue-time features
etc.) are deliberately NOT attempted yet: with a single partial season of
labels, adding engineered-feature variations on top would be exactly the
kind of repeated inspection of one small dataset this repo's research
conventions prohibit. They become possible once the 2010-2025 SIA gap is
filled (or lake coverage accumulates).

Output: a timestamped JSON report under logs/historical/reports/.
"""

import json
import os
from datetime import datetime, timezone

import ground_truth
import metrics
import model

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLES_PATH = os.path.join(BASE_DIR, "logs", "historical", "datasets", "retraining_samples.jsonl")
REPORT_DIR = os.path.join(BASE_DIR, "logs", "historical", "reports")

EPOCHS = 40  # same as backtest.py's production protocol


def day_of(row) -> str:
    return row["date"][:10]


def default_boundaries(rows):
    """Fold boundaries chosen from the labeled data actually present:
    with labels spanning multiple years, expanding-window YEAR folds
    (train earlier years -> validate the next whole year - the standard
    protocol this repo's backtest uses); with a single labeled year, fall
    back to intra-year MONTH folds (all the data can support)."""
    years = sorted({r["date"][:4] for r in rows})
    if len(years) > 1:
        return tuple(years[1:])
    months = sorted({r["date"][:7] for r in rows})
    return tuple(months[1:])


def chronological_day_folds(rows, boundaries):
    """Expanding-window folds: each boundary is an ISO prefix ('YYYY' for
    a whole validation year, 'YYYY-MM' for a validation month) - train on
    all days strictly before it, validate on the days matching it.
    Day-grouped by construction (split on calendar date, never on hourly
    rows); ISO prefixes compare lexicographically so both granularities
    use the same comparison."""
    folds = []
    for boundary in boundaries:
        width = len(boundary)
        train = [r for r in rows if r["date"][:width] < boundary]
        validate = [r for r in rows if r["date"][:width] == boundary]
        train_days = {day_of(r) for r in train}
        validate_days = {day_of(r) for r in validate}
        assert not (train_days & validate_days), "a calendar day crossed a fold boundary"
        if train and validate:
            folds.append({"validation_period": boundary, "train": train, "validate": validate,
                          "n_train_days": len(train_days), "n_validate_days": len(validate_days)})
    return folds


def evaluate(rows, weights) -> dict:
    labels = [r["outcome"] for r in rows]
    probs = [model.score(r["features"], weights) for r in rows]
    return metrics.classification_report(labels, probs, threshold=0.5)


def main(argv=None):
    weights_before = open(os.path.join(BASE_DIR, "weights.json"), "rb").read()

    rows = ground_truth.load_jsonl(SAMPLES_PATH)
    if not rows:
        raise SystemExit("no retraining samples on disk - run retraining_dataset.py first")
    rows.sort(key=lambda r: r["date"])
    label_sources = sorted({r["label_provenance"]["source"] for r in rows})

    production = model.load_weights()

    folds = chronological_day_folds(rows, boundaries=default_boundaries(rows))
    fold_reports = []
    for fold in folds:
        fresh = model.new_weights()
        model.validate_schema(fresh)
        model.train_epochs(fresh, fold["train"], epochs=EPOCHS, seed=model.DEFAULT_TRAIN_SEED)
        fold_reports.append({
            "validation_period": fold["validation_period"],
            "n_train": len(fold["train"]), "n_validate": len(fold["validate"]),
            "n_train_days": fold["n_train_days"], "n_validate_days": fold["n_validate_days"],
            "model_a_production_reference": evaluate(fold["validate"], production),
            "model_b_fresh_sia_labels": evaluate(fold["validate"], fresh),
        })

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "samples_path": SAMPLES_PATH,
        "n_rows": len(rows),
        "label_sources": label_sources,
        "label_policy_version": rows[0]["label_provenance"].get("policy_version"),
        "epochs": EPOCHS,
        "seed": model.DEFAULT_TRAIN_SEED,
        "folds": fold_reports,
        "caveats": [
            "Model A (production reference) was trained on these same hours under "
            "Samedan-proxy labels - its numbers are a deployed-behaviour reference, "
            "not a fair holdout result.",
            "One partial season (May-Jul 2026) of SIA labels cannot support the full "
            "multi-year rolling-origin protocol - these two intra-season folds are "
            "preliminary evidence only.",
            "No production weights were modified by this script.",
        ],
        "weights_modified": False,
    }

    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out = os.path.join(REPORT_DIR, f"model_comparison_sia_{stamp}.json")
    with open(out, "w") as f:
        json.dump(report, f, indent=2)

    weights_after = open(os.path.join(BASE_DIR, "weights.json"), "rb").read()
    assert weights_before == weights_after, "model_comparison_sia.py must never modify weights.json"

    print(json.dumps({"report": out, "n_rows": len(rows), "n_folds": len(fold_reports)}, indent=2))
    for fr in fold_reports:
        a, b = fr["model_a_production_reference"], fr["model_b_fresh_sia_labels"]
        print(f"\nfold validate={fr['validation_period']} "
              f"(train {fr['n_train']} rows/{fr['n_train_days']} days -> "
              f"validate {fr['n_validate']} rows/{fr['n_validate_days']} days)")
        for name, m in (("A(prod ref)", a), ("B(fresh SIA)", b)):
            print(f"  {name}: acc={m['accuracy']} bal_acc={m['balanced_accuracy']} "
                  f"auc={m['roc_auc']} pr_auc={m['pr_auc']} brier={m['brier_score']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
