import os
import uuid

import pytest
import pytest_asyncio

from utils.db import (
    Base,
    close_db,
    create_workspace_record,
    get_or_create_user,
    get_session,
    get_user_id_for_workspace,
    get_workspaces_by_user_id,
    init_db,
)

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/workspace_api_test",
)


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initialize the DB and clean up tables before each test."""
    await init_db(TEST_DATABASE_URL)
    # Clean tables in correct order (FKs first)
    async with get_session() as session:
        await session.execute(Base.metadata.tables["workspaces"].delete())
        await session.execute(Base.metadata.tables["auth_identities"].delete())
        await session.execute(Base.metadata.tables["users"].delete())
        await session.commit()
    yield
    await close_db()


class TestGetOrCreateUser:
    @pytest.mark.asyncio
    async def test_new_user(self):
        user = await get_or_create_user("google", "google-sub-1", "alice@example.com")

        assert user.email == "alice@example.com"
        assert user.id is not None
        assert user.created_at is not None

    @pytest.mark.asyncio
    async def test_existing_user_returns_same_id(self):
        user1 = await get_or_create_user("google", "google-sub-2", "bob@example.com")
        user2 = await get_or_create_user("google", "google-sub-2", "bob@example.com")

        assert user1.id == user2.id


class TestWorkspaceRecord:
    @pytest.mark.asyncio
    async def test_create_workspace(self):
        user = await get_or_create_user("google", "google-sub-3", "carol@example.com")
        ws_id = uuid.uuid4()
        ws = await create_workspace_record(
            user_id=user.id,
            workspace_id=ws_id,
            claim_name="sandbox-claim-abc123",
            template_name="python-runtime-template",
        )

        assert ws.id == ws_id
        assert ws.user_id == user.id
        assert ws.claim_name == "sandbox-claim-abc123"
        assert ws.template_name == "python-runtime-template"

    @pytest.mark.asyncio
    async def test_get_workspaces_by_user_id(self):
        user = await get_or_create_user("google", "google-sub-4", "dave@example.com")
        ws_id = uuid.uuid4()
        await create_workspace_record(
            user_id=user.id,
            workspace_id=ws_id,
            claim_name="claim-xyz",
            template_name="test-template",
        )

        found = await get_workspaces_by_user_id(user.id)
        assert found is not None
        assert found.id == ws_id

    @pytest.mark.asyncio
    async def test_get_workspaces_by_user_id_none(self):
        result = await get_workspaces_by_user_id(uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_get_user_id_for_workspace(self):
        user = await get_or_create_user("google", "google-sub-5", "eve@example.com")
        ws_id = uuid.uuid4()
        await create_workspace_record(
            user_id=user.id,
            workspace_id=ws_id,
            claim_name="claim-999",
            template_name="test-template",
        )

        owner_id = await get_user_id_for_workspace(str(ws_id))
        assert owner_id == user.id
