"""
Module 3 - Validation Engine
Pydantic V2 models for invoice data validation.

"""

from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Existing accounting tolerance; all arithmetic remains Decimal.
AMOUNT_TOLERANCE = Decimal("0.01")


def finite_decimal(value):
    """Keep financial inputs exact; reject floats, booleans and bad grouping."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('Expected an exact decimal string or Decimal.')
    if isinstance(value, str):
        if not re.fullmatch(r'-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?', value):
            raise ValueError('Malformed decimal value.')
        value = value.replace(',', '')
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError('Malformed decimal value.') from None
    if not result.is_finite():
        raise ValueError('Expected a finite decimal value.')
    return result


class LineItem(BaseModel):
    description: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    amount: Decimal = Field(ge=0)

    @field_validator("quantity", "unit_price", "amount", mode="before")
    @classmethod
    def coerce_line_item_amounts(cls, value):
        return finite_decimal(value)

    @model_validator(mode="after")
    def check_amount_matches_quantity_price(self) -> "LineItem":
        expected = round(self.quantity * self.unit_price, 2)
        if abs(expected - self.amount) > AMOUNT_TOLERANCE:
            raise ValueError(
                f"Line item amount ({self.amount}) does not match "
                f"quantity * unit_price ({expected})"
            )
        return self


class Invoice(BaseModel):
    # NOTE: renamed from invoice_id -> invoice_number to match both
    # Jonathan's extraction output and Aleeya's DB column (standardised_records.invoice_number)
    invoice_number: str = Field(min_length=1)
    vendor_name: str = Field(min_length=1)
    invoice_date: date
    due_date: Optional[date] = None
    line_items: List[LineItem] = Field(min_length=1)
    subtotal: Decimal = Field(ge=0)
    tax_amount: Decimal = Field(ge=0)
    total_amount: Decimal = Field(ge=0)
    discount: Optional[Decimal] = Field(default=None, ge=0)
    currency: str = Field(
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    )

    # ---------------------------------------------------------------
    # Field-level type checking 
    # ---------------------------------------------------------------
    @field_validator("invoice_date", "due_date", mode="before")
    @classmethod
    def parse_date(cls, value):
        if value is None or isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
                    raise ValueError
                return date.fromisoformat(value)
            except ValueError:
                raise ValueError(
                    f"Date '{value}' is not in valid ISO format (YYYY-MM-DD)"
                )
        raise ValueError("Date must be a string in YYYY-MM-DD format")

    @field_validator("total_amount", "subtotal", "tax_amount", "discount", mode="before")
    @classmethod
    def coerce_to_decimal(cls, value):
        if value is None:
            return None
        return finite_decimal(value)

    # ---------------------------------------------------------------
    # Cross-field math validation , built using @model_validator
    # techniques researched
    #
    # Why mode="after": by this point every individual field has already
    # passed its own type/format validation, so self.subtotal, self.tax_amount
    # etc. are guaranteed to be real Decimal/date values we can safely compare.
    # mode="before" would run before that, on raw unvalidated input.
    # ---------------------------------------------------------------
    @model_validator(mode="after")
    def check_subtotal_matches_line_items(self) -> "Invoice":
        computed_subtotal = sum((item.amount for item in self.line_items), Decimal("0"))
        if abs(computed_subtotal - self.subtotal) > AMOUNT_TOLERANCE:
            raise ValueError(
                f"sum(line_items.amount) = {computed_subtotal} does not match "
                f"subtotal = {self.subtotal}"
            )
        return self

    @model_validator(mode="after")
    def check_total_matches_subtotal_plus_tax(self) -> "Invoice":
        expected_total = self.subtotal + self.tax_amount - (self.discount if self.discount is not None else Decimal('0'))
        if abs(expected_total - self.total_amount) > AMOUNT_TOLERANCE:
            raise ValueError(
                f"subtotal + tax_amount - discount = {expected_total} does not match "
                f"total_amount = {self.total_amount}"
            )
        return self

    @model_validator(mode="after")
    def check_due_date_after_invoice_date(self) -> "Invoice":
        if self.due_date and self.due_date < self.invoice_date:
            raise ValueError("due_date cannot be earlier than invoice_date")
        return self

def validate_invoice(invoice_data: Dict[str, Any]) -> dict:
    """
    Validate extracted invoice data and return a backend-friendly result.

    Returns:
        {
            "status": "VALID" | "REVIEW_REQUIRED",
            "invoice": Invoice.model_dump() | None,
            "errors": [...]
        }
    """
    try:
        invoice = Invoice(**invoice_data)

        return {
            "status": "VALID",
            "invoice": invoice.model_dump(),
            "errors": [],
        }

    except Exception as exc:
        errors = []

        if hasattr(exc, "errors"):
            for error in exc.errors():
                field = ".".join(str(x) for x in error.get("loc", []))
                message = error.get("msg", "Validation failed")

                error_type = error.get("type", "")

                if (error_type == 'missing' or error.get('input') is None
                        or error.get('input') == '' or (field == 'line_items' and error.get('input') == [])):
                    code = "EX-001"
                elif 'does not match quantity * unit_price' in message:
                    code = "EX-003"
                elif not field and "total_amount" in message:
                    code = "EX-005"
                elif not field and "sum(line_items.amount)" in message:
                    code = "EX-004"
                elif not field and "due_date" in message:
                    code = "EX-006"

                else:
                    code = "EX-002"

                if not field:
                    field = {'EX-004': 'subtotal', 'EX-005': 'total_amount', 'EX-006': 'due_date'}.get(code, 'fields')

                errors.append({
                    "code": code,
                    "field": field,
                    "message": message,
                })

        else:
            errors.append({
                "code": "EX-002",
                "field": "",
                "message": 'Unsupported invoice value or structure.',
            })

        return {
            "status": "REVIEW_REQUIRED",
            "invoice": None,
            "errors": errors,
        }
