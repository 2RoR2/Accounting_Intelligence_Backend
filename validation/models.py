"""
Module 3 - Validation Engine
Pydantic V2 models for invoice data validation.

"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Tolerance for floating point / rounding differences when comparing money values
AMOUNT_TOLERANCE = Decimal("0.01")


class LineItem(BaseModel):
    description: str = Field(min_length=1)
    quantity: float = Field(gt=0)
    unit_price: Decimal = Field(gt=0)
    amount: Decimal = Field(gt=0)

    @field_validator("unit_price", "amount", mode="before")
    @classmethod
    def coerce_line_item_amounts(cls, value):
        if isinstance(value, str):
            value = value.replace(",", "")
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError(f"'{value}' is not a valid monetary amount")

    @model_validator(mode="after")
    def check_amount_matches_quantity_price(self) -> "LineItem":
        expected = round(Decimal(str(self.quantity)) * self.unit_price, 2)
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
    subtotal: Decimal = Field(gt=0)
    tax_amount: Decimal = Field(ge=0)
    total_amount: Decimal = Field(gt=0)
    currency: str = Field(
    default="MYR",
    min_length=3,
    max_length=3,
    pattern=r"^[A-Za-z]{3}$"
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
                return date.fromisoformat(value)
            except ValueError:
                raise ValueError(
                    f"Date '{value}' is not in valid ISO format (YYYY-MM-DD)"
                )
        raise TypeError("Date must be a string in YYYY-MM-DD format")

    @field_validator("total_amount", "subtotal", "tax_amount", mode="before")
    @classmethod
    def coerce_to_decimal(cls, value):
        # Extraction engine (Path A) outputs amounts with thousand-separator
        # commas, e.g. "2,193,713.00" - strip them before parsing as Decimal.
        if isinstance(value, str):
            value = value.replace(",", "")
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError(f"'{value}' is not a valid monetary amount")

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
        expected_total = self.subtotal + self.tax_amount
        if abs(expected_total - self.total_amount) > AMOUNT_TOLERANCE:
            raise ValueError(
                f"subtotal + tax_amount = {expected_total} does not match "
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

                if error_type == "missing":
                    code = "EX-001"

                elif field in {"invoice_date", "due_date"}:
                    code = "EX-002"

                elif field in {"subtotal", "tax_amount", "total_amount", "currency"}:
                    code = "EX-002"

                elif "line_items" in field:
                    code = "EX-003"

                elif "total_amount" in message:
                    code = "EX-005"

                elif "sum(line_items.amount)" in message:
                    code = "EX-004"

                elif "due_date" in message:
                    code = "EX-006"

                else:
                    code = "EX-002"

                errors.append({
                    "code": code,
                    "field": field,
                    "message": message,
                })

        else:
            errors.append({
                "code": "VALIDATION_ERROR",
                "field": "",
                "message": str(exc),
            })

        return {
            "status": "REVIEW_REQUIRED",
            "invoice": None,
            "errors": errors,
        }
