-- Separate database for the integration tests (see TEST_DATABASE_URL).
-- The pgvector extension itself is created by the Alembic migration.
CREATE DATABASE reels_test OWNER reels;
