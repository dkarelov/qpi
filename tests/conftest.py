from __future__ import annotations

import os

import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from tests.utils import (
    assert_safe_test_database,
    parse_database_url,
    run_schema_apply,
    truncate_public_tables,
)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; database integration tests are skipped")
    assert_safe_test_database(url)
    return url


@pytest.fixture(scope="session")
def prepared_database(test_database_url: str) -> str:
    if os.getenv("QPI_SKIP_TEST_SCHEMA_APPLY") != "1":
        run_schema_apply(test_database_url)
    return test_database_url


@pytest.fixture
def isolated_database(prepared_database: str) -> str:
    truncate_public_tables(prepared_database)
    return prepared_database


@pytest.fixture(scope="session")
def migration_smoke_database_url(test_database_url: str) -> str:
    if os.getenv("RUN_MIGRATION_SMOKE") != "1":
        pytest.skip("RUN_MIGRATION_SMOKE is not set to 1; migration smoke is skipped")
    scratch_url = os.getenv("TEST_SCRATCH_DATABASE_URL", "").strip()
    if not scratch_url:
        parsed = parse_database_url(test_database_url)
        scratch_url = test_database_url.rsplit(parsed.dbname, 1)[0] + f"{parsed.dbname}_scratch"
    assert_safe_test_database(scratch_url, require_scratch_name=True)
    return scratch_url


@pytest_asyncio.fixture
async def db_pool(isolated_database: str):
    pool = AsyncConnectionPool(
        conninfo=isolated_database,
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
