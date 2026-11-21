-- settlement_allocation_guard as it was before migration 20261121 (counted undone allocations). Test fixture only.
CREATE OR REPLACE FUNCTION settlement_allocation_guard() RETURNS trigger AS $$
DECLARE
    s settlements%ROWTYPE;
    r standardised_records%ROWTYPE;
    used_settlement NUMERIC(18,2);
    used_record NUMERIC(18,2);
BEGIN
    IF NEW.status <> 'active' THEN RETURN NEW; END IF;
    SELECT * INTO s FROM settlements WHERE id = NEW.settlement_id AND tenant_id = NEW.tenant_id FOR UPDATE;
    IF s.status <> 'recorded' THEN RAISE EXCEPTION 'Only recorded settlements can be allocated.'; END IF;
    SELECT * INTO r FROM standardised_records WHERE id = NEW.record_id AND tenant_id = NEW.tenant_id FOR UPDATE;
    IF NOT FOUND OR NOT r.is_current OR COALESCE(r.amount_due, r.total_amount) IS NULL THEN
        RAISE EXCEPTION 'Only a current approved record with a total can be allocated.';
    END IF;
    IF r.currency <> s.currency THEN RAISE EXCEPTION 'Settlement and record currencies must match.'; END IF;
    SELECT COALESCE(SUM(amount),0) INTO used_settlement FROM settlement_allocations
      WHERE settlement_id=NEW.settlement_id AND tenant_id=NEW.tenant_id AND status='active';
    SELECT COALESCE(SUM(amount),0) INTO used_record FROM settlement_allocations
      WHERE record_id=NEW.record_id AND tenant_id=NEW.tenant_id AND status='active';
    IF used_settlement + NEW.amount > s.amount THEN RAISE EXCEPTION 'Settlement allocation exceeds the unmatched amount.'; END IF;
    IF used_record + NEW.amount > COALESCE(r.amount_due, r.total_amount) THEN RAISE EXCEPTION 'Record allocation exceeds the unmatched amount.'; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
