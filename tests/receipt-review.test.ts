import { test } from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { addExtractedLineItem } from "../src/lib/api/document.service";
import { SourceEvidenceList } from "../src/components/common/documents/document-viewer";

test("a missing printed row sends exact amounts, source explanation and displayed revision", async () => {
  const previous = globalThis.fetch;
  const input = { description: "Public item", quantity: "1", unitPrice: null, amount: "6.50", sourcePage: 1,
    sourceNote: "Printed price 6.80 less printed line discount 0.30; net amount 6.50.", expectedContentRevision: 4 };
  globalThis.fetch = async (url, init) => {
    assert.equal(url, "http://127.0.0.1:8000/api/documents/public-test/line-items");
    assert.equal(init?.method, "POST");
    assert.deepEqual(JSON.parse(String(init?.body)), input);
    return Response.json({ id: "line", contentRevision: 5 });
  };
  try { assert.equal((await addExtractedLineItem("public-test", input)).contentRevision, 5); }
  finally { globalThis.fetch = previous; }
});

test("manual source evidence shows the reviewer's note and links the source without invented coordinates", () => {
  const html = renderToStaticMarkup(createElement(SourceEvidenceList, { documentId: "public-test", evidence: [
    { page: 1, text: "6.50", method: "HUMAN", source_note: "Printed discount <0.30> already applied." },
  ] }));
  assert.match(html, /Reviewer source note:/);
  assert.match(html, /Printed discount &lt;0.30&gt; already applied/);
  assert.match(html, /public-test\/file#page=1/);
  assert.match(html, /Source coordinates not available/);
});
