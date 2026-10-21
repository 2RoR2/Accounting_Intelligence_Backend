-- Adds AP matching and approval-policy tables to an existing database.
CREATE TABLE IF NOT EXISTS purchase_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vendor_id UUID,
    po_number TEXT NOT NULL,
    order_date DATE,
    currency CHAR(3),
    total_amount NUMERIC(14,2) CHECK (total_amount >= 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('draft','open','partially_received','closed','cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, po_number),
    FOREIGN KEY (vendor_id, tenant_id) REFERENCES vendors(id, tenant_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS purchase_order_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purchase_order_id UUID NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL CHECK (line_number >= 1),
    description TEXT NOT NULL,
    quantity NUMERIC(14,3) NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(14,4) NOT NULL CHECK (unit_price >= 0),
    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
    UNIQUE (purchase_order_id, line_number)
);
CREATE TABLE IF NOT EXISTS goods_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    purchase_order_id UUID NOT NULL REFERENCES purchase_orders(id) ON DELETE RESTRICT,
    receipt_number TEXT NOT NULL,
    received_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'received' CHECK (status IN ('draft','received','cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, receipt_number)
);
CREATE TABLE IF NOT EXISTS goods_receipt_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goods_receipt_id UUID NOT NULL REFERENCES goods_receipts(id) ON DELETE CASCADE,
    purchase_order_line_id UUID REFERENCES purchase_order_lines(id) ON DELETE RESTRICT,
    line_number INTEGER NOT NULL CHECK (line_number >= 1),
    description TEXT NOT NULL,
    quantity NUMERIC(14,3) NOT NULL CHECK (quantity > 0),
    UNIQUE (goods_receipt_id, line_number)
);
ALTER TABLE validation_rules ADD COLUMN IF NOT EXISTS approval_limit_amount NUMERIC(14,2);
ALTER TABLE validation_rules ADD COLUMN IF NOT EXISTS approval_limit_currency CHAR(3);
ALTER TABLE validation_rules ADD COLUMN IF NOT EXISTS require_po_for_ap BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE validation_rules ADD COLUMN IF NOT EXISTS require_grn_for_ap BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_purchase_orders_tenant_status ON purchase_orders(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_goods_receipts_po ON goods_receipts(purchase_order_id, received_date);
