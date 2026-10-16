"""Demo data: 1 society, 6 flats, admin + resident users (passwords from settings)."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Society
from app.repositories import FlatRepository, SocietyRepository, UserRepository
from app.services.auth import hash_password

log = structlog.get_logger(__name__)
SLUG = "green-valley"


async def seed_demo(session: AsyncSession, settings: Settings | None = None) -> Society:
    """Idempotent. Safe to call on every start."""
    settings = settings or get_settings()
    societies = SocietyRepository(session)
    society = await societies.get_by_slug(SLUG)
    if society:
        return society
    society = await societies.create(
        name="Green Valley Apartments", slug=SLUG, address="12 Lake Road, Bengaluru", total_flats=6
    )
    flats = FlatRepository(session)
    created = [
        await flats.create(
            society_id=society.id,
            unit_number=f"{floor}0{n}",
            floor=floor,
            residents=2 + n % 3,
            sensor_id=f"{SLUG}-{floor}0{n}",
        )
        for floor in (1, 2, 3)
        for n in (1, 2)
    ]
    users = UserRepository(session)
    rounds = settings.bcrypt_rounds
    await users.create(
        username="admin",
        name="Demo Admin",
        role="admin",
        society_id=society.id,
        hashed_password=hash_password(settings.demo_admin_password, rounds),
    )
    await users.create(
        username="resident",
        name="Demo Resident",
        role="resident",
        society_id=society.id,
        flat_id=created[0].id,
        hashed_password=hash_password(settings.demo_resident_password, rounds),
    )
    log.info("demo_seeded", society=SLUG, flats=len(created))
    return society
