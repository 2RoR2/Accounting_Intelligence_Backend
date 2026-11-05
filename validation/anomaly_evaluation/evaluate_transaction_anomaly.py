import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from validation.transactions.transaction_anomaly import detect_transaction_anomalies

BASE_DIR = Path(__file__).resolve().parent
DATASET_PATH = BASE_DIR / "transaction_anomaly_dataset.json"
REFERENCE_PATH = BASE_DIR / "transaction_anomaly_reference.json"


def calculate_metrics(actual, predicted):
    """Calculate binary classification metrics without extra dependencies."""
    tp = sum(a and p for a, p in zip(actual, predicted))
    tn = sum(not a and not p for a, p in zip(actual, predicted))
    fp = sum(not a and p for a, p in zip(actual, predicted))
    fn = sum(a and not p for a, p in zip(actual, predicted))

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0

    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )

    return {
        "true_positive": tp,
        "true_negative": tn,
        "false_positive": fp,
        "false_negative": fn,
        "precision": precision,
        "recall": recall,
        "f1_score": f1,
    }


def main():
    with DATASET_PATH.open("r", encoding="utf-8") as file:
        dataset = json.load(file)

    with REFERENCE_PATH.open("r", encoding="utf-8") as file:
        reference_dataset = json.load(file)

    predictions = detect_transaction_anomalies(
        dataset,
        reference_transactions=reference_dataset,
    )

    actual_by_id = {
        row["transaction_id"]: row["actual_anomaly"]
        for row in dataset
    }

    actual = [
        actual_by_id[result["transaction_id"]]
        for result in predictions
    ]

    predicted = [
        result["is_anomaly"]
        for result in predictions
    ]

    insufficient = [
        result["transaction_id"]
        for result in predictions
        if result["status"] == "insufficient_baseline"
    ]

    if insufficient:
        raise RuntimeError(
            "Evaluation is inconclusive: insufficient baseline for "
            + ", ".join(insufficient)
        )

    metrics = calculate_metrics(actual, predicted)

    print("\nTRANSACTION ANOMALY DETECTION EVALUATION")
    print("-" * 45)
    print(f"Dataset size:       {len(dataset)}")
    print(f"Actual anomalies:   {sum(actual)}")
    print(f"Predicted anomalies:{sum(predicted):>5}")
    print("\nConfusion Matrix")
    print(f"True Positive:      {metrics['true_positive']}")
    print(f"False Positive:     {metrics['false_positive']}")
    print(f"True Negative:      {metrics['true_negative']}")
    print(f"False Negative:     {metrics['false_negative']}")

    print("\nPerformance Metrics")
    print(f"Precision:          {metrics['precision']:.4f}")
    print(f"Recall:             {metrics['recall']:.4f}")
    print(f"F1-score:           {metrics['f1_score']:.4f}")

    print("\nTransaction-level Results")
    for result, expected in zip(predictions, actual):
        print(
            f"{result['transaction_id']}: "
            f"actual={'ANOMALY' if expected else 'NORMAL'}, "
            f"predicted={result['status']}"
        )


if __name__ == "__main__":
    main()
