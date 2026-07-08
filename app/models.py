import json
from datetime import datetime
from typing import Any, List

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ObjectRecord(Base):
    __tablename__ = "objects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    simbad_main_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    query_text: Mapped[str] = mapped_column(String, nullable=False)
    ra_deg: Mapped[float | None] = mapped_column(nullable=True)
    dec_deg: Mapped[float | None] = mapped_column(nullable=True)
    otype: Mapped[str | None] = mapped_column(String, nullable=True)
    spectral_type: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_state: Mapped[str] = mapped_column(String, nullable=False)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Only populated when resolution_state == 'AMBIGUOUS'. Holds the raw list of
    # candidate dicts (main_id, ra, dec, otype, sp_type) returned by SIMBAD so the
    # disambiguation list can be rendered/re-rendered without re-querying SIMBAD.
    candidates_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    identifiers: Mapped[List["IdentifierRecord"]] = relationship(back_populates="object", cascade="all, delete-orphan")
    planets: Mapped[List["PlanetRecord"]] = relationship(back_populates="object", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "resolution_state IN ('RESOLVED','AMBIGUOUS','PARTIAL','UNRESOLVED')",
            name="ck_resolution_state",
        ),
    )

    @property
    def candidates(self) -> list[dict[str, Any]]:
        if not self.candidates_json:
            return []
        try:
            return json.loads(self.candidates_json)
        except (TypeError, ValueError):
            return []

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "simbad_main_id": self.simbad_main_id,
            "query_text": self.query_text,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "otype": self.otype,
            "spectral_type": self.spectral_type,
            "resolution_state": self.resolution_state,
            "ai_summary": self.ai_summary,
            "candidates": self.candidates,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "identifiers": [identifier.to_dict() for identifier in self.identifiers],
            "planets": [planet.to_dict() for planet in self.planets],
        }


class IdentifierRecord(Base):
    __tablename__ = "identifiers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), nullable=False)
    catalog: Mapped[str] = mapped_column(String, nullable=False)
    identifier: Mapped[str] = mapped_column(String, nullable=False)
    matched_exoplanet_archive: Mapped[bool] = mapped_column(default=False)

    object: Mapped[ObjectRecord] = relationship(back_populates="identifiers")

    __table_args__ = (UniqueConstraint("object_id", "catalog", "identifier", name="uq_identifier"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "catalog": self.catalog,
            "identifier": self.identifier,
            "matched_exoplanet_archive": self.matched_exoplanet_archive,
        }


class PlanetRecord(Base):
    __tablename__ = "planets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), nullable=False)
    pl_name: Mapped[str] = mapped_column(String, nullable=False)
    pl_letter: Mapped[str | None] = mapped_column(String, nullable=True)
    orbital_period_days: Mapped[float | None] = mapped_column(nullable=True)
    planet_radius_earth: Mapped[float | None] = mapped_column(nullable=True)
    discovery_year: Mapped[int | None] = mapped_column(nullable=True)
    discovery_method: Mapped[str | None] = mapped_column(String, nullable=True)

    object: Mapped[ObjectRecord] = relationship(back_populates="planets")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pl_name": self.pl_name,
            "pl_letter": self.pl_letter,
            "orbital_period_days": self.orbital_period_days,
            "planet_radius_earth": self.planet_radius_earth,
            "discovery_year": self.discovery_year,
            "discovery_method": self.discovery_method,
        }
