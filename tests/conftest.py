"""Shared test setup: cheap password hashing for every test."""

import pytest

from app.auth import passwords


@pytest.fixture(autouse=True, scope="session")
def _cheap_password_hashing():
    passwords.configure(time_cost=1, memory_cost=8, parallelism=1)  # tests, not production: speed only
