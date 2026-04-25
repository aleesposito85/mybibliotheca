"""Shared pytest fixtures for graph-touching tests.

Spins up an isolated KuzuDB per test session in a tempdir, applies the
production schema lazily, and seeds a small deterministic graph.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Iterator, Tuple

import pytest

# Pytest puts conftest's directory on sys.path automatically; the seed file
# sits next to this conftest and is imported as a sibling module (no
# `tests.` prefix because the tests/ dir has no __init__.py).
from _kuzu_seed import seed_graph


@pytest.fixture(scope="session")
def kuzu_tempdir() -> Iterator[str]:
    tmp = tempfile.mkdtemp(prefix="bibliotheca_kuzu_test_")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def kuzu_seeded(kuzu_tempdir: str) -> Iterator[Tuple[object, dict]]:
    """Session-scoped seeded KuzuDB connection.

    Returns (connection, named_ids).
    """
    os.environ["KUZU_DB_PATH"] = os.path.join(kuzu_tempdir, "kuzu")
    os.environ["DATA_DIR"] = kuzu_tempdir

    # Reset and reload the singleton manager so it picks up the test paths.
    from app.utils.safe_kuzu_manager import reset_safe_kuzu_manager, get_safe_kuzu_manager
    reset_safe_kuzu_manager()
    mgr = get_safe_kuzu_manager()
    with mgr.get_connection(operation="test_seed") as conn:
        ids = seed_graph(conn)
        yield conn, ids
