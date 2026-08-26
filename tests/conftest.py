"""Pytest configuration: default DB backend for unit tests (no live DB required)."""

import os

# In-memory SQLite so importing `app` (which calls initialize_database) works without MySQL.
# CI has no .env; production-required Slack vars must be present unless LOCAL_DEVELOPMENT=true.
os.environ.setdefault("LOCAL_DEVELOPMENT", "true")
os.environ.setdefault("DATABASE_BACKEND", "sqlite")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")
os.environ.setdefault("SLACK_CLIENT_ID", "111.222")
os.environ.setdefault("SLACK_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("SLACK_BOT_SCOPES", "chat:write")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "test-encryption-key-16")
