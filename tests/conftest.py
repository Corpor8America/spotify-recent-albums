"""Pytest fixtures for future pytest-style tests.

The unittest suites here manage their own contexts via tests.support;
these fixtures wrap the same helpers.
"""

import pytest

import spotify_core as core
from tests.support import make_context, write_config


@pytest.fixture
def ctx(tmp_path):
    """A fresh AppContext on a temp dir, installed as the default."""
    from tests.support import ContextTestCase  # noqa: F401  (ensure env set)

    context = make_context(tmp_path)
    core.set_context(context)
    yield context
    core.set_context(None)


@pytest.fixture
def client(ctx):
    from app import create_app

    write_config(ctx)
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
