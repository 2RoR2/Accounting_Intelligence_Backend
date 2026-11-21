-- Existing databases: the trial balance kept only status 'posted' journals, so after a reversal the reversed
-- original dropped out while its reversal (posted) stayed, leaving the opposite balance instead of zero.
-- Same view as the consolidated schema; CREATE OR REPLACE keeps it idempotent.
CREATE OR REPLACE VIEW internal_trial_balance AS
SELECT j.tenant_id,
       l.account_id,
       a.code,
       a.name,
       a.account_type,
       COALESCE(SUM(l.debit), 0)::NUMERIC(18,2) AS total_debit,
       COALESCE(SUM(l.credit), 0)::NUMERIC(18,2) AS total_credit,
       COALESCE(SUM(l.debit - l.credit), 0)::NUMERIC(18,2) AS net_balance
FROM journal_headers j
JOIN journal_lines l ON l.journal_id = j.id AND l.tenant_id = j.tenant_id
JOIN ledger_accounts a ON a.id = l.account_id AND a.tenant_id = l.tenant_id
WHERE j.status IN ('posted', 'reversed')
GROUP BY j.tenant_id, l.account_id, a.code, a.name, a.account_type;
