from __future__ import annotations

from collections.abc import Mapping

from featuregen.intake.banking_catalog import BankingDomainCatalog

# Process-global registered read-only catalog (mirrors SP-1's collaborator seams). Wired by P9 via
# register_intake_catalog(...); reset per test by the shared conftest's intake_catalog fixture.
_INTAKE_CATALOG: BankingDomainCatalog | None = None


class IntakeCatalogNotConfigured(RuntimeError):
    """Raised by current_intake_catalog when no catalog has been registered — fail-closed (§4.5(b))."""


def register_intake_catalog(catalog: BankingDomainCatalog) -> None:
    """R8/R10 — register the process-global, read-only BankingDomainCatalog the intake banking-boundary
    screens read (§4.5, Decision D8). Idempotent last-writer-wins; P9 wires the seeded catalog here."""
    global _INTAKE_CATALOG
    _INTAKE_CATALOG = catalog


def current_intake_catalog() -> BankingDomainCatalog:
    """R8/R10 — the registered catalog, or FAIL CLOSED if unset (§4.5(b)): never silently None, so the
    banking-boundary screen can never auto-pass against a missing catalog. P7 `_prohibited_intent_screen`
    reads this seam (NOT load_banking_catalog(conn))."""
    if _INTAKE_CATALOG is None:
        raise IntakeCatalogNotConfigured(
            "no intake catalog registered; call register_intake_catalog(...) first"
        )
    return _INTAKE_CATALOG


def _clear_intake_catalog() -> None:
    """Test-only reset of the process-global catalog seam (mirrors SP-1's collaborator seams)."""
    global _INTAKE_CATALOG
    _INTAKE_CATALOG = None


def load_banking_catalog_from_seed(seed: Mapping) -> BankingDomainCatalog:
    """R8 — build a read-only BankingDomainCatalog from an in-memory seed mapping (a thin wrapper over
    BankingDomainCatalog.from_seed; read-only, never grounding — Decision D8)."""
    return BankingDomainCatalog.from_seed(seed)
