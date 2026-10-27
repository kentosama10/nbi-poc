"""Train the AI match scorer (logistic regression) and save it as JSON.

    python -m scripts.train_model        (takes a few minutes)

Training uses ONLY applicants whose split is 'train'. The answer key
(ground_truth.csv) is used here as training labels and nowhere at scoring time.
"""
import csv
import json
import sys
import time
from datetime import datetime, timezone

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold, cross_val_predict

from app.candidates import CANDIDATE_LIMIT, search_candidates
from app.clients import ES_INDEX, get_es
from ml.features import FEATURE_NAMES, compute_features
from ml.model import ARTIFACT_PATH, DEFAULT_AUTO_CLEAR, ESCALATE_THRESHOLD, HitModel
from scripts.generate_data import DATA_DIR

MODEL_VERSION = "logreg-v1"
SAFETY_FACTOR = 0.5     # auto-clear threshold = half the lowest true-match score


def read_csv(name: str) -> list[dict]:
    path = DATA_DIR / name
    if not path.exists():
        sys.exit(f"Missing {path}. Run first: python -m scripts.generate_data")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def new_classifier() -> LogisticRegression:
    # class_weight="balanced": true matches are rare, so make missing one costlier
    return LogisticRegression(class_weight="balanced", C=1.0, max_iter=1000,
                              random_state=42)


def collect_training_pairs(applicants, wl_by_no, truth):
    """Search candidates for each training applicant; label each pair 1 only if
    it is the applicant's true record."""
    X, y, groups = [], [], []
    true_total = true_found = 0
    for n, a in enumerate(applicants):
        t = truth[a["reference_no"]]
        is_true = t["is_true_match"] == "1"
        found = False
        for c in search_candidates(a["first_name"], a["middle_name"], a["last_name"]):
            w = wl_by_no[c["record_no"]]
            feats = compute_features(a, w)
            positive = is_true and t["linked_record_no"] == c["record_no"]
            found = found or positive
            X.append([feats[name] for name in FEATURE_NAMES])
            y.append(1 if positive else 0)
            groups.append(n)            # keeps one applicant's pairs together
        true_total += is_true
        true_found += is_true and found
        if (n + 1) % 1000 == 0:
            print(f"  searched {n + 1} of {len(applicants)} training applicants...")
    return (np.array(X), np.array(y), np.array(groups),
            {"true_total": true_total, "true_found": true_found})


def evaluate_split(model, applicants, wl_by_no, truth) -> dict:
    """Score every applicant in a split the way the live API would."""
    s = dict(applications=0, flagged=0, need_review=0, pairs=0, pairs_review=0,
             true_total=0, true_not_found=0, true_cleared=0)
    for a in applicants:
        t = truth[a["reference_no"]]
        is_true = t["is_true_match"] == "1"
        cands = search_candidates(a["first_name"], a["middle_name"], a["last_name"])
        s["applications"] += 1
        s["flagged"] += bool(cands)
        needs_review = found = False
        for c in cands:
            band = model.band(model.predict(
                compute_features(a, wl_by_no[c["record_no"]])))
            s["pairs"] += 1
            if band != "AUTO_CLEAR":
                s["pairs_review"] += 1
                needs_review = True
            if is_true and t["linked_record_no"] == c["record_no"]:
                found = True
                s["true_cleared"] += band == "AUTO_CLEAR"
        s["need_review"] += needs_review
        if is_true:
            s["true_total"] += 1
            s["true_not_found"] += not found
    return s


def main() -> None:
    started = time.perf_counter()
    try:
        if get_es().count(index=ES_INDEX)["count"] == 0:
            raise RuntimeError("empty index")
    except Exception:
        sys.exit("Elasticsearch has no watchlist. Run: python -m scripts.load_data --reload")

    wl_by_no = {w["record_no"]: w for w in read_csv("watchlist.csv")}
    truth = {t["applicant_reference_no"]: t for t in read_csv("ground_truth.csv")}
    applicants = read_csv("applicants.csv")
    train = [a for a in applicants if truth[a["reference_no"]]["split"] == "train"]
    test = [a for a in applicants if truth[a["reference_no"]]["split"] == "test"]
    print(f"Training applicants: {len(train)}   (test applicants kept aside: {len(test)})")

    print("Step 1/4: searching candidates for the training applicants...")
    X, y, groups, st = collect_training_pairs(train, wl_by_no, truth)
    if y.sum() == 0:
        sys.exit("No true-match pairs found. Check the data and Elasticsearch.")
    print(f"  {len(y)} candidate pairs, {int(y.sum())} are true matches")
    print(f"  search found the true record for {st['true_found']} of "
          f"{st['true_total']} training true matches")

    print("Step 2/4: out-of-fold scores (5 folds, grouped by applicant)...")
    oof = cross_val_predict(new_classifier(), X, y, cv=GroupKFold(n_splits=5),
                            groups=groups, method="predict_proba")[:, 1]
    lowest_true = float(oof[y == 1].min())
    auto_clear = round(min(DEFAULT_AUTO_CLEAR, SAFETY_FACTOR * lowest_true), 6)
    print(f"  lowest out-of-fold score of any true match: {lowest_true:.4f}")
    print(f"  chosen auto-clear threshold: {auto_clear}  "
          f"(= min(0.20, {SAFETY_FACTOR} x lowest))")

    print("Step 3/4: fitting the final model on all training pairs...")
    clf = new_classifier().fit(X, y)
    data = {
        "model_version": MODEL_VERSION,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feature_names": FEATURE_NAMES,
        "coefficients": [float(w) for w in clf.coef_[0]],
        "intercept": float(clf.intercept_[0]),
        "thresholds": {"auto_clear": auto_clear, "escalate": ESCALATE_THRESHOLD},
        "training": {
            "split_used": "train only", "applicants": len(train), "pairs": int(len(y)),
            "true_pairs": int(y.sum()), "candidate_limit": CANDIDATE_LIMIT,
            "lowest_true_oof_probability": round(lowest_true, 6),
            "safety_factor": SAFETY_FACTOR,
        },
    }
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  saved {ARTIFACT_PATH}")
    print("  learned weights:")
    for name, w in zip(FEATURE_NAMES, data["coefficients"]):
        print(f"    {name:<18} {w:+.3f}")
    print(f"    {'intercept':<18} {data['intercept']:+.3f}")

    print("Step 4/4: quick check on the held-out TEST split (information only; "
          "NOT used for training or tuning)...")
    model = HitModel(data)
    r = evaluate_split(model, test, wl_by_no, truth)
    avoided = r["flagged"] - r["need_review"]
    print(f"  test applications                       : {r['applications']}")
    print(f"  baseline: flagged by name -> manual     : {r['flagged']}")
    print(f"  with AI: still need manual review       : {r['need_review']}")
    print(f"  manual reviews avoided                  : {avoided} "
          f"({100 * avoided / max(r['flagged'], 1):.1f}%)")
    print(f"  candidate pairs: {r['pairs']} total, {r['pairs_review']} need review")
    print(f"  test true matches                       : {r['true_total']}")
    print(f"  true record not found by search         : {r['true_not_found']}")
    print(f"  true matches AUTO-CLEARED (must be 0)   : {r['true_cleared']}")
    print(f"\nDone in {time.perf_counter() - started:.0f} seconds.")


if __name__ == "__main__":
    main()