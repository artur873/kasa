"""Documents channel: KSeF FA(3) invoices, JPK and office exports, internal
sheets (import wizard), PDFs from e-mails read by a model, and deduplication.

Every channel follows the same path: ``store_raw`` (L0) -> parse into
:class:`~kasa.core.documents.facts.InvoiceFacts` -> ``upsert_document`` (L1),
which links observations of the same invoice under one document.
"""
