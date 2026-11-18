import { test } from "node:test";
import assert from "node:assert/strict";
import { imageEvidenceBox } from "../src/lib/document-viewer";
import type { SourceEvidence } from "../src/types";

const evidence: SourceEvidence = { page: 1, coordinate_units: "pixels", page_size: { width: 3000, height: 4000 },
  bbox: { l: 300, t: 800, r: 1500, b: 1200, coord_origin: "TOPLEFT" } };

test("receipt pixel evidence scales with the displayed image", () => {
  assert.deepEqual(imageEvidenceBox(evidence, 3000, 4000), { left: "10%", top: "20%", width: "40%", height: "10%" });
  assert.deepEqual(imageEvidenceBox({ ...evidence, page_size: undefined }, 3000, 4000), imageEvidenceBox(evidence, 3000, 4000));
});
test("document points require explicit page size with a matching image aspect ratio", () => {
  assert.ok(imageEvidenceBox({ ...evidence, coordinate_units: "document_points" }, 1500, 2000));
  assert.equal(imageEvidenceBox({ ...evidence, coordinate_units: "document_points", page_size: undefined }, 3000, 4000), null);
  assert.equal(imageEvidenceBox(evidence, 4000, 3000), null);
});
test("ambiguous, out-of-page and malformed coordinates never produce a highlight", () => {
  for (const item of [
    { ...evidence, page: 2 }, { ...evidence, page: undefined }, { ...evidence, coordinate_units: "unknown" },
    { ...evidence, bbox: { ...evidence.bbox!, coord_origin: "BOTTOMLEFT" } },
    ...[{ l: -1 }, { r: 3001 }, { t: NaN }, { b: 0 }, { r: 200 }].map(change => ({ ...evidence, bbox: { ...evidence.bbox!, ...change } })),
  ]) assert.equal(imageEvidenceBox(item, 3000, 4000), null);
});
