"""Delivery D — the semantic-binding candidate store (immutable candidate sets + the mutable
compare-and-swap current-projection). D1 owns the schema (migration 1014) and the persistence
contract in :mod:`store_projection`; D2/D3/D4 build the shortlist / LLM / wiring on top."""
