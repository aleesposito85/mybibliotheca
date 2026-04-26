"""Shared pytest fixtures for graph-touching tests.

Spins up an isolated KuzuDB per test session in a tempdir, applies the
production schema, and seeds a small deterministic graph. Tests that don't
need the graph can ignore these fixtures entirely.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Iterator, Tuple

import pytest

from _kuzu_seed import seed_graph


@pytest.fixture(scope="session")
def kuzu_tempdir() -> Iterator[str]:
    """Session-scoped temp dir for KuzuDB files."""
    tmp = tempfile.mkdtemp(prefix="bibliotheca_kuzu_test_")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def kuzu_seeded(kuzu_tempdir: str) -> Iterator[Tuple[object, dict]]:
    """Session-scoped seeded KuzuDB connection.

    Returns (connection, named_ids). The connection survives the whole
    session because schema setup is heavy and the seed graph is read-only
    in tests.
    """
    # Point the safe_kuzu_manager at our temp dir BEFORE importing it.
    os.environ["KUZU_DB_PATH"] = os.path.join(kuzu_tempdir, "kuzu")
    os.environ["DATA_DIR"] = kuzu_tempdir

    # Import here so env vars take effect first.
    from app.utils.safe_kuzu_manager import get_safe_kuzu_manager, reset_safe_kuzu_manager

    # Reset any previously cached singleton so it picks up the new KUZU_DB_PATH.
    reset_safe_kuzu_manager()

    mgr = get_safe_kuzu_manager()
    with mgr.get_connection(operation="test_seed") as conn:
        ids = seed_graph(conn)
        yield conn, ids
