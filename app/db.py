from pathlib import Path

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
Pool = ConnectionPool[Connection[DictRow]]


def _configure(c: Connection[DictRow]) -> None:
    c.execute("set search_path = leadloom")


def connect(url: str, max_size: int = 4) -> Pool:
    """A small pool (Supabase's free session pooler allows few connections, and n8n
    shares them), with the schema applied once."""
    pool = Pool(
        url,
        connection_class=Connection[DictRow],
        min_size=1,
        max_size=max_size,
        timeout=15,
        configure=_configure,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=True,
    )
    with pool.connection() as c:
        c.execute(SCHEMA)
    return pool
