"""Role-based access control for the API.

Permissions are the PRIMITIVE; roles are bundles of them; routes check PERMISSIONS (never role
strings). So a role can be renamed, or a sixth persona added, without touching a single route, and
"who can do X?" is a one-line lookup in ROLE_PERMISSIONS.

Functional roles (this file) answer "what OPERATIONS may you perform". They are a SEPARATE axis from
the data-sensitivity roles pii_reader / restricted_reader (see read_scope), which answer "which
sensitive COLUMNS may you see". A user legitimately holds one of each, e.g. {feature_engineer, pii_reader}.
"""
from __future__ import annotations

from collections.abc import Iterable

# ---- Capabilities (the stable primitive routes depend on) ---------------------------------------
CATALOG_READ = "catalog:read"          # browse the data catalogue: search, join edges, join paths
CATALOG_WRITE = "catalog:write"        # publish/curate the data catalogue: upload, quarantine, entity tags
FEATURE_READ = "feature:read"          # browse the feature + hypothesis catalogue (registry, Feature 360)
FEATURE_GENERATE = "feature:generate"  # run the feature-generation workflow + govern contracts
IAM_MANAGE = "iam:manage"              # administer users / groups / roles
# Confirm/reject discovered joins on the governance queue. NOTE: the route gate (require_confirmer)
# does NOT rely on this yet — it checks the raw `platform-admin` role claim directly, matching the
# overlay's dual-owner confirm. This constant exists for future reconciliation of that gate into the
# permission model.
GOVERNANCE_CONFIRM = "governance:confirm"
# Read SAFE LLM-call audit summaries (which task/dispatch touched a ref, versions, outcome, times).
# RESTRICTED: raw/redacted LLM inputs, raw outputs and repair bodies are NEVER exposed by this — they
# stay in the audit store. Granted only to platform_admin + an explicitly provisioned audit role, so a
# catalog_viewer / data_owner / feature_engineer cannot read the LLM audit trail.
AUDIT_READ = "audit:read"

ALL_PERMISSIONS = frozenset({CATALOG_READ, CATALOG_WRITE, FEATURE_READ, FEATURE_GENERATE, IAM_MANAGE,
                             GOVERNANCE_CONFIRM, AUDIT_READ})

# ---- Roles (bundles) ----------------------------------------------------------------------------
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    # read-only: browse the data catalogue AND the feature/hypothesis catalogue
    "catalog_viewer": frozenset({CATALOG_READ, FEATURE_READ}),
    # publishes/curates the data catalogue (upload + quarantine + entity tags); does NOT build features
    "data_owner": frozenset({CATALOG_READ, CATALOG_WRITE}),
    # builds features: runs the generation workflow + governs contracts; cannot upload
    "feature_engineer": frozenset({CATALOG_READ, FEATURE_READ, FEATURE_GENERATE}),
    # identity administrator: manages access only (separation of duties from data + feature work)
    "access_admin": frozenset({IAM_MANAGE}),
    # explicitly provisioned audit reader: may read SAFE LLM-call audit summaries and nothing else.
    # Deliberately NOT bundled into catalog_viewer / data_owner / feature_engineer.
    "audit_reader": frozenset({AUDIT_READ}),
    # superuser
    "platform_admin": ALL_PERMISSIONS,
}


def permissions_for(roles: Iterable[str]) -> set[str]:
    """Union of the permissions granted by these roles (unknown roles grant nothing)."""
    perms: set[str] = set()
    for r in roles:
        perms |= ROLE_PERMISSIONS.get(r, frozenset())
    return perms


def has_permission(roles: Iterable[str], permission: str) -> bool:
    return permission in permissions_for(roles)


def roles_granting(permission: str) -> list[str]:
    """The role names that confer `permission` (e.g. iam:manage -> [access_admin, platform_admin]).
    Used by the last-admin lockout guards so they track the CAPABILITY, not one hard-coded role name."""
    return sorted(r for r, perms in ROLE_PERMISSIONS.items() if permission in perms)
