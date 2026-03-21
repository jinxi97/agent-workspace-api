import hashlib
import os
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

engine: AsyncEngine | None = None
async_session_factory: sessionmaker | None = None
_connector = None  # Cloud SQL Connector instance (if used)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(Text, unique=True, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class AuthIdentity(Base):
    __tablename__ = "auth_identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_sub", name="uq_provider_sub"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    provider = Column(Text, nullable=False)  # "google", "github", "email", etc.
    provider_sub = Column(Text, nullable=False)  # provider's unique user ID
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    key_hash = Column(Text, unique=True, nullable=False)
    key_masked = Column(Text, nullable=False)  # e.g. "sk-...XScA"
    name = Column(Text, nullable=False, default="default")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        unique=True,
        nullable=False,
    )
    claim_name = Column(Text, nullable=False)
    template_name = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


async def _create_cloud_sql_engine() -> AsyncEngine:
    """Create an engine using Cloud SQL Python Connector."""
    global _connector
    from google.cloud.sql.connector import create_async_connector

    _connector = await create_async_connector()

    instance_connection_name = os.environ["CLOUD_SQL_CONNECTION_NAME"]
    db_user = os.environ["DB_USER"]
    db_pass = os.environ["DB_PASS"]
    db_name = os.environ["DB_NAME"]

    return create_async_engine(
        "postgresql+asyncpg://",
        async_creator=lambda: _connector.connect_async(
            instance_connection_name,
            "asyncpg",
            user=db_user,
            password=db_pass,
            db=db_name,
        ),
    )


def _create_direct_engine(database_url: str) -> AsyncEngine:
    """Create an engine using a direct connection URL (for local dev / tests)."""
    return create_async_engine(database_url)


async def init_db(database_url: str = "") -> None:
    """Initialize the database engine and create tables.

    If database_url is provided, connects directly (local dev / tests).
    Otherwise, uses Cloud SQL Python Connector via env vars.
    """
    global engine, async_session_factory
    if database_url:
        engine = _create_direct_engine(database_url)
    else:
        engine = await _create_cloud_sql_engine()
    async_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    global engine, async_session_factory, _connector
    if engine:
        await engine.dispose()
    if _connector:
        await _connector.close_async()
    engine = None
    async_session_factory = None
    _connector = None


def get_session() -> AsyncSession:
    if async_session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db first.")
    return async_session_factory()


async def get_or_create_user(provider: str, provider_sub: str, email: str) -> User:
    """Find a user by auth identity, or create a new user + identity."""
    async with get_session() as session:
        # Look up existing identity
        result = await session.execute(
            select(AuthIdentity).where(
                AuthIdentity.provider == provider,
                AuthIdentity.provider_sub == provider_sub,
            )
        )
        identity = result.scalar_one_or_none()
        if identity:
            user_result = await session.execute(
                select(User).where(User.id == identity.user_id)
            )
            return user_result.scalar_one()

        # No identity found — create user + identity
        user = User(email=email)
        session.add(user)
        await session.flush()  # get user.id before creating identity

        identity = AuthIdentity(
            user_id=user.id,
            provider=provider,
            provider_sub=provider_sub,
        )
        session.add(identity)
        await session.commit()
        await session.refresh(user)
        return user


def _hash_api_key(raw_key: str) -> str:
    """SHA-256 hash of the raw key for storage."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _mask_api_key(raw_key: str) -> str:
    """Return masked version like 'sk-...XScA'."""
    return f"{raw_key[:3]}...{raw_key[-4:]}"


async def create_api_key(user_id: uuid.UUID, name: str = "default") -> tuple[ApiKey, str]:
    """Generate a new API key for the given user.

    Returns (api_key_record, raw_key). The raw key is only available at
    creation time — only the hash is persisted.
    """
    raw_key = f"sk-{secrets.token_urlsafe(32)}"
    key_hash = _hash_api_key(raw_key)
    key_masked = _mask_api_key(raw_key)
    async with get_session() as session:
        api_key = ApiKey(
            user_id=user_id,
            key_hash=key_hash,
            key_masked=key_masked,
            name=name,
        )
        session.add(api_key)
        await session.commit()
        await session.refresh(api_key)
        return api_key, raw_key


async def list_api_keys(user_id: uuid.UUID) -> list[ApiKey]:
    """Return all API keys for the given user."""
    async with get_session() as session:
        result = await session.execute(
            select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at)
        )
        return list(result.scalars().all())


async def verify_api_key(raw_key: str) -> ApiKey | None:
    """Look up an API key by its hash. Returns the record or None."""
    key_hash = _hash_api_key(raw_key)
    async with get_session() as session:
        result = await session.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash)
        )
        return result.scalar_one_or_none()


async def create_workspace_record(
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    claim_name: str,
    template_name: str,
) -> Workspace:
    async with get_session() as session:
        workspace = Workspace(
            id=workspace_id,
            user_id=user_id,
            claim_name=claim_name,
            template_name=template_name,
        )
        session.add(workspace)
        await session.commit()
        await session.refresh(workspace)
        return workspace


async def get_workspace_by_user_id(user_id: uuid.UUID) -> Workspace | None:
    async with get_session() as session:
        result = await session.execute(
            select(Workspace).where(Workspace.user_id == user_id)
        )
        return result.scalar_one_or_none()


async def get_user_id_for_workspace(workspace_id: str) -> uuid.UUID | None:
    async with get_session() as session:
        result = await session.execute(
            select(Workspace.user_id).where(Workspace.id == uuid.UUID(workspace_id))
        )
        return result.scalar_one_or_none()
