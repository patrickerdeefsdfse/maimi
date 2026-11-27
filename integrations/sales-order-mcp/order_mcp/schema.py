from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

class Evidence(StrictModel):
    source: str
    quote: str
    confidence: float = Field(ge=0, le=1)

class Line(StrictModel):
    source_line: str | None = None
    customer_item: str | None = None
    item_code: str | None = None
    description: str | None = None
    specification: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    price_basis: Literal["inclusive", "exclusive", "unknown"] = "unknown"
    tax_rate: Decimal | None = None
    amount: Decimal | None = None
    amount_basis: Literal["inclusive", "exclusive", "unknown"] = "unknown"
    request_date: str | None = None
    remark: str | None = None

class Order(StrictModel):
    business_entity: str | None = None
    customer_name: str | None = None
    account_number: str | None = None
    customer_po: str | None = None
    order_type: str | None = None
    price_list_name: str | None = None
    currency: str | None = None
    order_date: str | None = None
    total_amount: Decimal | None = None
    total_basis: Literal["inclusive", "exclusive", "unknown"] = "unknown"
    salesrep_name: str | None = None
    head_remark: str | None = None
    lines: list[Line] = Field(default_factory=list, max_length=500)
    # Paths are e.g. customer_po, lines.0.customer_item, lines.0.quantity.
    evidence: dict[str, Evidence] = Field(default_factory=dict)

class Extraction(StrictModel):
    orders: list[Order] = Field(min_length=1, max_length=30)
    warnings: list[str] = Field(default_factory=list)

class Issue(StrictModel):
    path: str
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
