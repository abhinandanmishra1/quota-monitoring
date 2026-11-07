class DatabaseUnavailableError(Exception):
    """Raised when PostgreSQL is unreachable or the pool is missing (fail closed)."""
