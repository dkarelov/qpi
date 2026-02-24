from __future__ import annotations

import os

import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from tests.utils import reset_public_schema, run_upgrade


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; database integration tests are skipped")
    return url


@pytest.fixture
def migrated_database(test_database_url: str) -> str:
    reset_public_schema(test_database_url)
    run_upgrade(test_database_url, "head")
    return test_database_url


@pytest_asyncio.fixture
async def db_pool(migrated_database: str):
    pool = AsyncConnectionPool(
        conninfo=migrated_database,
        min_size=1,
        max_size=4,
        open=False,
        kwargs={"autocommit": False, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    try:
        yield pool
    finally:
        await pool.close()
