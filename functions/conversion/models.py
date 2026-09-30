"""Data models for unit and currency conversion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConversionCategory(str, Enum):
    LENGTH = "Length & Distance"
    WEIGHT = "Weight & Mass"
    TEMPERATURE = "Temperature"
    SPEED = "Speed"
    TIME = "Time"
    VOLUME = "Volume"
    AREA = "Area"
    DATA = "Digital Storage"
    CURRENCY = "Currency"


@dataclass(slots=True)
class ConversionResult:
    """The result of converting a value between units or currencies."""

    from_value: float
    from_unit: str
    from_symbol: str
    to_value: float
    to_unit: str
    to_symbol: str
    category: ConversionCategory
    rate_info: str | None = None
    rate_timestamp: str | None = None

    @property
    def display_text(self) -> str:
        """Formatted string representation, e.g. '10 USD → ₹832.40 INR'."""
        if self.category == ConversionCategory.CURRENCY:
            # Clear currency display
            from_str = f"{_format_num(self.from_value)} {self.from_unit.upper()}"
            to_sym = f"{self.to_symbol}" if self.to_symbol else ""
            to_str = f"{to_sym}{_format_num(self.to_value)} {self.to_unit.upper()}"
            return f"{from_str} → {to_str}"
        elif self.category == ConversionCategory.TEMPERATURE:
            from_str = f"{_format_num(self.from_value)} {self.from_symbol or self.from_unit}"
            to_str = f"{_format_num(self.to_value)} {self.to_symbol or self.to_unit}"
            return f"{from_str} → {to_str}"
        else:
            from_str = f"{_format_num(self.from_value)} {self.from_unit}"
            to_str = f"{_format_num(self.to_value)} {self.to_unit}"
            return f"{from_str} → {to_str}"


def _format_num(val: float) -> str:
    """Clean number formatting without trailing zero clutter."""
    if abs(val) >= 1_000_000:
        return f"{val:,.2f}"
    if abs(val) >= 1_000:
        return f"{val:,.2f}".rstrip("0").rstrip(".")
    if abs(val) < 0.0001 and val != 0:
        return f"{val:.6g}"
    if val.is_integer():
        return f"{int(val):,}"
    # Up to 4 decimal places, stripping trailing zeros
    res = f"{val:.4f}".rstrip("0").rstrip(".")
    return res
