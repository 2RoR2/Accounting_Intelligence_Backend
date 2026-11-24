# Team test script (per role)

Use this once the team demo data is in place (`docs/TEAM_DEMO_DATA_PLAN.md`, `docs/COMPANY_PACKS.md`). Every company
and document is fictional. Write down anything that looks wrong, with the company, page and document number.

**First sign-in**
1. On the sign-in page, choose **Forgot password**, enter your address for that company and type the emailed code.
   Then choose your own password.
2. Sign in with that password.
   - Accountants then type an emailed code.
   - Local admins scan the authenticator QR code once, then type its code.

## Accountant (about 45 minutes per company)

Your Today and Documents pages show only the documents assigned to you. The books, Sales, Purchases, Bank and reports
are company-wide.

1. **Today.** You have 12 documents (6 for a starter set). Note the counts per lane and the notification "Document
   assigned to you".
2. **To review (3).**
   - Open the invoice with "Confirm the goods were received". Check it against the preview, then submit it for
     approval.
   - Do the same for the electricity bill, then for the supplier debit note (it refers to your posted invoice).
3. **Ready (3).** Submit the invoice, the bill and the supplier credit note. The credit note should point to its
   original invoice.
4. **Needs fixing (3).**
   - *Unknown supplier*: create the supplier (or match it), then revalidate.
   - *Possible duplicate*: compare it with the original invoice of the same number and exclude it.
   - *Missing number*: the printed document has none, so type the supplier's reference, then revalidate.
5. **Waiting approval (2).** These wait for your local admin. Check you cannot approve them yourself.
6. **Sales.**
   - Make a quotation for an existing customer, convert it into an invoice and issue it. Tax is typed as printed;
     check nothing is recalculated.
   - Make a credit note against one of your invoices.
7. **Bank.**
   - Open the November statement.
   - Find the line "Transfer pending identification". It stays unmatched; read why the reconciliation still balances.
   - Match one open customer payment if you see one.
8. **Customers.** Open a statement of account. Check the ageing buckets against Reports, then AR ageing.
9. **Search and trace.** Search for your posted invoice number and trace it from the document to its journal.
10. **Live upload** (spends AI quota, only on your day): see your upload pack's `README.md`.

## Local admin (about 45 minutes per company)

1. **Approvals.**
   - Approve one waiting document.
   - Return the other with a note. Check the accountant sees the note.
2. **Post.** Post an approved supplier invoice. Open the journal and check the account and the typed tax.
3. **Assign work.** Reassign one document from one accountant to another. Check it moves from one person's Today
   to the other's.
4. **Bank.** Confirm a suggested match and post its payment, then complete the November reconciliation (it should
   refuse until every line is accounted for).
5. **Periods.** September is closed: try to post into it (refused), reopen it with a reason, then close it again.
6. **Team.**
   - Invite a test accountant (fictional `.test` address), set them Active and check the seat count.
   - Disable them again.
   - Note: an invite does not send an activation email yet.
7. **Reports.** Check the trial balance, P&L, AR and AP ageing and the business summary.

## When something is wrong

Report the company code, your role, the page, the document number and what you expected. The data is rebuilt from
code, so nothing you do can damage it permanently. A rebuild on hosted UAT needs Jonathan's go-ahead.
