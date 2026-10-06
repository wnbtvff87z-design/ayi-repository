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
        for migration in migrations:
            statements = migration.read_text(encoding='utf-8').split(';')
            for statement in statements:
                if statement.strip():
                    conn.execute(statement)


if __name__ == '__main__':
    main()
