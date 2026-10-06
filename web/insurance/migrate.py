"""Apply the additive insurance case/outbox migration using a migration-only DSN."""
from pathlib import Path
import os

import psycopg


def main():
    uri = os.getenv('INSURANCE_MIGRATION_DATABASE_URL', '').strip()
    if not uri:
        raise RuntimeError('INSURANCE_MIGRATION_DATABASE_URL is not configured')
    migrations = sorted((Path(__file__).with_name('migrations')).glob('*.sql'))
    with psycopg.connect(uri, connect_timeout=5) as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS insurance_schema_migrations '
            '(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())'
        )
        for migration in migrations:
            version = migration.name
            with conn.transaction():
                applied = conn.execute(
                    'SELECT 1 FROM insurance_schema_migrations WHERE version=%s',
                    (version,),
                ).fetchone()
                if applied:
                    continue
                conn.execute(migration.read_text(encoding='utf-8'))
                conn.execute(
                    'INSERT INTO insurance_schema_migrations(version) VALUES(%s)',
                    (version,),
                )


if __name__ == '__main__':
    main()
