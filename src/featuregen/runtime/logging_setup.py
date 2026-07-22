"""Process-wide logging configuration for the API / worker entrypoints.

The app previously configured no logging at all, so Python defaulted to the WARNING root level
with no handler formatting — every ``logger.info(...)`` in the pipeline was silently dropped and
operators saw only uvicorn's access line. This module installs one formatted stream handler and
sets the ``featuregen`` logger to a configurable level.

Call ``configure_logging()`` from the real process entrypoints ONLY (``create_app_from_env`` and
the worker/CLI ``main``), never from ``create_app`` — the pytest suite drives the ASGI app through
``create_app`` directly and must keep pytest's own log capture untouched.
"""
from __future__ import annotations

import logging
import os

_CONFIGURED = False

# Level applied to the top-level ``featuregen`` logger. Override per-deployment with
# FEATUREGEN_LOG_LEVEL=DEBUG for full step-by-step tracing, WARNING to quiet down.
_DEFAULT_LEVEL = "INFO"
_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> None:
    """Install a formatted root stream handler + set the ``featuregen`` logger level. Idempotent —
    safe to call more than once (only the first call takes effect). ``force=True`` replaces any
    pre-existing root handlers so the format is consistent; uvicorn's ``uvicorn.access`` logger keeps
    its own handler (propagate=False), so access logs are unaffected."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.environ.get("FEATUREGEN_LOG_LEVEL", _DEFAULT_LEVEL).upper()
    logging.basicConfig(level=level, format=_FORMAT, datefmt=_DATEFMT, force=True)
    # The pipeline loggers live under the ``featuregen`` namespace; set the whole tree explicitly so
    # the level applies regardless of the root level uvicorn may have left behind.
    logging.getLogger("featuregen").setLevel(level)
    _CONFIGURED = True
    logging.getLogger("featuregen.runtime").info(
        "logging configured (level=%s) — set FEATUREGEN_LOG_LEVEL=DEBUG for full tracing", level)
