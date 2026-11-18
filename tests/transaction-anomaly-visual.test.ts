import { test } from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { TransactionAnomalyVisual } from "../src/features/common/documents/transaction-anomaly-visual";
import type { TransactionAnomaly } from "../src/types/validation";

test("transaction anomaly visualization renders current amount and threshold for review", () => {
  const result: TransactionAnomaly = {
    transaction_id: "synthetic-current",
    amount: "50000.00",
    currency: "MYR",
    is_anomaly: true,
    status: "anomaly",
    anomaly_score: "125",
    baseline_median: "100.00",
    upper_bound: "400.00",
    baseline_count: 5,
    reason: "Amount 50000 MYR exceeds the statistical upper bound of 400 MYR.",
  };
  const html = renderToStaticMarkup(React.createElement(TransactionAnomalyVisual, { result }));

  assert.match(html, /Transaction anomaly analysis/);
  assert.match(html, /MYR 50,000\.00/);
  assert.match(html, /MYR 100\.00/);
  assert.match(html, /MYR 400\.00/);
  assert.match(html, /Anomaly flagged/);
  assert.match(html, /does not determine fraud/);
  assert.match(html, /transaction-anomaly-track/);
});

test("insufficient history clearly renders unavailable baseline without a misleading chart", () => {
  const result: TransactionAnomaly = {
    transaction_id: "synthetic-current",
    amount: "75.00",
    currency: "MYR",
    is_anomaly: false,
    status: "insufficient_baseline",
    anomaly_score: null,
    baseline_median: null,
    upper_bound: null,
    baseline_count: 3,
    reason: "Insufficient reference transactions for MYR; at least 5 are required.",
  };
  const html = renderToStaticMarkup(React.createElement(TransactionAnomalyVisual, { result }));

  assert.match(html, /Baseline unavailable/);
  assert.match(html, /Not available/);
  assert.doesNotMatch(html, /transaction-anomaly-track/);
});
