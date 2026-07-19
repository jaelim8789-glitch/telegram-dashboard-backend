import asyncio
from logging.config import fileConfig

from sqlalchemy import inspect, pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.config import settings
from app.database import Base
from app.models.campaign import Campaign  # noqa: F401 - not exported from app.models; registers on Base.metadata
from app.models import (  # noqa: F401 - ensures models are registered on Base.metadata
    Account,
    APIKey,
    AutoReplyLog,
    AutoReplyRule,
    Broadcast,
    Folder,
    FollowUpRule,
    GroupJoinLog,
    GroupSearchResult,
    Lead,
    MessageLog,
    MessageTemplate,
    PaymentRecord,
    PhoneVerification,
    ReplyMacro,
    ReplyMacroLog,
    TeamMember,
    Tenant,
    UsageRecord,
    User,
)
from app.ai.models import (  # noqa: F401 - ensures AI Platform models are registered on Base.metadata
    AiApiCallLog,
    AiApiProviderConfig,
    AiEventLog,
    AiEventSubscription,
    AiPluginRegistration,
    AiScheduleDefinition,
    AiScheduleExecution,
    AiTask,
    AiTaskLog,
    AiToolDefinition,
    AiToolExecutionLog,
    AiWorkflowDefinition,
    AiWorkflowExecution,
    AiWorkflowStep,
)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in "offline" mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _ensure_wide_version_column(connection: Connection) -> None:
    """Some historic merge-migration revision IDs (e.g.
    ``merge_folders_and_reply_macro_heads``) are longer than Alembic's default
    ``alembic_version.version_num VARCHAR(32)``, which raises
    StringDataRightTruncationError on a from-scratch upgrade. Widen (or
    pre-create) the column so the full migration chain can replay on a fresh
    database, matching what already exists on long-lived deployments.
    """
    if connection.dialect.name != "postgresql":
        return
    if "alembic_version" not in inspect(connection).get_table_names():
        connection.execute(
            text(
                "CREATE TABLE alembic_version ("
                "version_num VARCHAR(255) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
    else:
        connection.execute(text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"))
    # Commit this DDL on its own so it isn't folded into (and doesn't change the
    # ownership/commit semantics of) the transaction Alembic is about to open via
    # context.begin_transaction() below.
    connection.commit()


def do_run_migrations(connection: Connection) -> None:
    _ensure_wide_version_column(connection)
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in "online" mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
