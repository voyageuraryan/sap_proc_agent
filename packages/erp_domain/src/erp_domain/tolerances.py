"""Which tolerance applies to which vendor.

Kept in its own module so config.py can import it without a cycle, and so the
"what is the tolerance here" question has exactly one answer in the codebase.
"""

from decimal import Decimal

from pydantic import BaseModel, field_validator


class ToleranceSet(BaseModel):
    price_pct: Decimal
    quantity_pct: Decimal


class ToleranceBook(BaseModel):
    default: ToleranceSet
    by_vendor: dict[str, ToleranceSet] = {}

    @field_validator("by_vendor")
    @classmethod
    def _keys_are_sap_vendor_ids(cls, v: dict) -> dict:
        # Unquoted YAML keys parse as ints, the lookup then misses silently and
        # every vendor gets the default tolerance -- plausible data, wrong labels.
        for key in v:
            if not (isinstance(key, str) and len(key) == 10 and key.isdigit()):
                raise ValueError(
                    f"by_vendor key {key!r} must be a 10-digit string "
                    f'-- quote it in the YAML: "{key}":'
                )
        return v

    def for_vendor(self, vendor_id: str) -> ToleranceSet:
        return self.by_vendor.get(vendor_id, self.default)
