"""Record schemas, keyed by Bronze source name."""

from __future__ import annotations

from src.validation.base import RecordModel
from src.validation.schemas.catalog import Product, Vendor
from src.validation.schemas.customers import Customer
from src.validation.schemas.invoices import Invoice, InvoiceLine
from src.validation.schemas.sales import Transaction
from src.validation.schemas.support import CallTranscript, ProductReview, SupportTicket

SCHEMAS: dict[str, type[RecordModel]] = {
    "products": Product,
    "customers": Customer,
    "transactions": Transaction,
    "vendors": Vendor,
    "invoices": Invoice,
    "invoice_lines": InvoiceLine,
    "support_tickets": SupportTicket,
    "product_reviews": ProductReview,
    "call_transcripts": CallTranscript,
}

__all__ = ["SCHEMAS"]
