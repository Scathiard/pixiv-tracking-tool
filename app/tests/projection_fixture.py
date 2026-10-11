"""Explicit projection setup for small, standalone synthetic ORM fixtures."""
from sqlalchemy.orm import Session

from src.core.projection_maintenance import install_projections, rebuild_projections


def prepare_synthetic_projection(engine, *, install=True):
    """Opt a test-owned database into the product read schema after seeding.

    Tests using ArchiveDatabase already receive transactional maintenance. Pure
    Base/Session fixtures deliberately perform their own explicit candidate
    backfill; this helper is never installed as a product read-time fallback.
    """
    with Session(engine) as session, session.begin():
        if install:
            install_projections(session.connection())
        rebuild_projections(session)
