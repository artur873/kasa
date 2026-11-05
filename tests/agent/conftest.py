"""Fixtures of the agent tests: the MCP server on the clicked pipeline database and the test client."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from kasa.core.agent.client import AgentClient
from kasa.core.agent.server import KasaAgent, build_server, create_agent
from tests.metrics.helpers import clicked_database


@pytest.fixture(scope="session")
def agent_url() -> str:
    return clicked_database()


@pytest.fixture(scope="module")
def agent(agent_url: str) -> KasaAgent:
    return create_agent("odm", database_url=agent_url, client="pytest", model="test-model")


@pytest.fixture(scope="module")
def client(agent: KasaAgent) -> AgentClient:
    return AgentClient(build_server(agent))


@pytest.fixture
def owner(agent_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(agent_url, autocommit=True) as connection:
        yield connection
