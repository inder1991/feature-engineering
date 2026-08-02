"""Identifier issuer scope — the ISSUER axis beside the scheme (semantic plan Task 2).

`Concept.namespace` names an identifier SCHEME (cif, swift_bic, ...). Cross-catalog equality also
needs ISSUER context: two catalogs both carrying "cif" values prove nothing unless the same
institution issued them (functional rule 5 — scheme alone is never proof). This module supplies
that axis from two closed sources:

* a GLOBALLY-ISSUED scheme has one issuing authority everywhere (`GLOBAL_SCHEME_ISSUERS` — a BIC
  is SWIFT's value space in every catalog);
* every other scheme is CATALOG-SCOPED: its issuer is whatever institution the catalog's operator
  declared through the one-time `catalog_semantic_scope` configuration (migration 1045, routes
  `GET/PUT /data-sources/catalogs/{catalog_source}/semantic-scope`). No institution name is ever
  hardcoded in application logic.

An undeclared catalog resolves honestly to `(None, "unresolved")` — a missing issuer NEVER
appears as verified, and downstream it flags an ADVISORY candidate rather than blocking one
(AI-proposed stays usable).

**Not a third namespace surface** (review C Task 2): this module only PRODUCES the issuer axis;
the pairing verdict stays inside `bridge_grounding.assess_grounded_identifier_link`, which every
path (enumeration, per-refs reassessment, direct proposal) already flows through — so the issuer
truth table cannot be bypassed by the grounded path.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.contracts import DbConn
from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY

#: The closed set of globally-issued schemes: scheme -> its issuing authority. A scheme listed
#: here has ONE value space everywhere, so its issuer never depends on catalog configuration.
#: Closed and code-reviewed by design — extending it is a vocabulary decision, not configuration.
GLOBAL_SCHEME_ISSUERS: dict[str, str] = {
    "swift_bic": "swift",
    "swift_uetr": "swift",
    "lei": "gleif",
}

#: The bases a DECLARED catalog scope may carry (the `unresolved` basis is never declared — it is
#: the honest absence, produced only by resolution).
DECLARED_SCOPE_BASES: tuple[str, ...] = ("catalog_scope", "global_scheme")


@dataclass(frozen=True, slots=True)
class CatalogSemanticScopeV1:
    catalog_source: str
    issuer_scope: str
    basis: str
    declared_by: str


def read_catalog_semantic_scope(
    conn: DbConn, catalog_source: str
) -> CatalogSemanticScopeV1 | None:
    row = conn.execute(
        "SELECT catalog_source, issuer_scope, basis, declared_by "
        "FROM catalog_semantic_scope WHERE catalog_source = %s",
        (catalog_source.strip().lower(),)).fetchone()
    if row is None:
        return None
    return CatalogSemanticScopeV1(
        catalog_source=row[0], issuer_scope=row[1], basis=row[2], declared_by=row[3])


def declare_catalog_semantic_scope(
    conn: DbConn, *, catalog_source: str, issuer_scope: str, basis: str, declared_by: str
) -> None:
    """Declare (or replace) one catalog's issuer scope. A CONFIGURATION, not an append-only fact
    — mirroring `catalog_engine`; the declarer + timestamp are the audit trail."""
    if basis not in DECLARED_SCOPE_BASES:
        raise ValueError(
            f"semantic-scope basis must be one of {list(DECLARED_SCOPE_BASES)}, got {basis!r}")
    if not issuer_scope.strip():
        raise ValueError("issuer_scope must not be blank")
    conn.execute(
        "INSERT INTO catalog_semantic_scope (catalog_source, issuer_scope, basis, declared_by) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (catalog_source) DO UPDATE SET issuer_scope = EXCLUDED.issuer_scope, "
        "  basis = EXCLUDED.basis, declared_by = EXCLUDED.declared_by, declared_at = now()",
        (catalog_source.strip().lower(), issuer_scope.strip().lower(), basis, declared_by))


def resolve_identifier_issuer(
    conn: DbConn, catalog_source: str, concept_name: str | None
) -> tuple[str | None, str]:
    """`(issuer_scope, basis)` for one identifier column, or `(None, "unresolved")`.

    A globally-issued scheme resolves to its scheme authority without touching configuration; a
    catalog-scoped scheme resolves to the catalog's declared issuer scope; anything else —
    including a non-identifier concept and an undeclared catalog — is honestly unresolved."""
    registered = CONCEPT_REGISTRY.get(concept_name) if concept_name else None
    if registered is None or registered.group != "identifier" or not registered.namespace:
        return None, "unresolved"
    global_issuer = GLOBAL_SCHEME_ISSUERS.get(registered.namespace)
    if global_issuer is not None:
        return global_issuer, "global_scheme"
    scope = read_catalog_semantic_scope(conn, catalog_source)
    if scope is not None and scope.issuer_scope:
        return scope.issuer_scope, "catalog_scope"
    return None, "unresolved"
