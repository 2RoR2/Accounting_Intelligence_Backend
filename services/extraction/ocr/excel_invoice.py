"""Small, deterministic helpers for invoices exported from formatted Excel sheets.

Excel invoices often use merged cells for labels and headings.  ``header=None``
keeps Pandas from guessing a misleading header row; forward-filling is applied
only to labels, never to numeric values, and rows are classified before parsing.
"""
from __future__ import annotations

from io import BytesIO
from typing import Any

import pandas as pd


def read_invoice_sheet(content: bytes, sheet_name: str | int = 0) -> pd.DataFrame:
    """Read a worksheet without treating merged title rows as column headers."""
    raw = pd.read_excel(BytesIO(content), sheet_name=sheet_name, header=None, dtype=object)
    raw = raw.dropna(axis=0, how="all").dropna(axis=1, how="all")
    if raw.empty:
        return pd.DataFrame()

    # Merged cells are represented as a value followed by NaN.  Fill labels
    # horizontally, but preserve the original numeric cells and line values.
    labels = raw.select_dtypes(exclude=["number"]).columns
    if len(labels):
        raw.loc[:, labels] = raw.loc[:, labels].ffill(axis=1)
    return raw.reset_index(drop=True)


def find_table_header(frame: pd.DataFrame, required: tuple[str, ...] = ("description", "amount")) -> int | None:
    """Find a likely line-item header row by normalized cell names."""
    wanted = {item.casefold().strip() for item in required}
    for index, row in frame.iterrows():
        values = {str(value).casefold().strip() for value in row.tolist() if pd.notna(value)}
        if wanted.issubset(values):
            return int(index)
    return None


def normalize_invoice_rows(frame: pd.DataFrame, header_row: int) -> list[dict[str, Any]]:
    """Convert rows below a detected header into JSON-safe line-item records."""
    headers = [str(value).strip().casefold() if pd.notna(value) else f"column_{i}" for i, value in enumerate(frame.iloc[header_row])]
    rows: list[dict[str, Any]] = []
    for values in frame.iloc[header_row + 1 :].itertuples(index=False, name=None):
        if all(pd.isna(value) for value in values):
            continue
        item = {headers[i]: (None if pd.isna(value) else value) for i, value in enumerate(values)}
        if any(value is not None for value in item.values()):
            rows.append(item)
    return rows
