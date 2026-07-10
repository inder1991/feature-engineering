"""Metadata-service connectors: additional READERS into the unchanged ingest pipeline.

A connector is a third translator (after the CSV and Excel readers) from an external metadata
source into ``list[CanonicalRow]``; every import still funnels through ``ingest_upload`` — same
validation, large-change brake, quarantine, fact assertion, drift watermark, and graph build.
Connectors add NO new write path into the catalog (binding spec: 2026-07-09 OpenMetadata
connector).
"""
