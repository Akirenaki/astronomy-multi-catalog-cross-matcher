"""SQLAlchemy ORM models for cached astronomical object resolutions."""

import json
from datetime import datetime
from typing import Any, List

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models in this project."""
    pass


class ObjectRecord(Base):
    """Represents one resolved or unresolved object lookup in the cache database."""
    __tablename__ = "objects"

    # Core identity and metadata for the queried astronomical object.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    simbad_main_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    query_text: Mapped[str] = mapped_column(String, nullable=False)
    ra_deg: Mapped[float | None] = mapped_column(nullable=True)
    dec_deg: Mapped[float | None] = mapped_column(nullable=True)
    otype: Mapped[str | None] = mapped_column(String, nullable=True)
    spectral_type: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_state: Mapped[str] = mapped_column(String, nullable=False)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Only populated when resolution_state == 'AMBIGUOUS'. This preserves the candidate list for UI rendering.
    candidates_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Serialized (JSON) alias-chain trail, e.g. ["51 pegasi", "51 Peg", "51 Peg b"], for the
    # "resolved via: ... -> ... -> ..." UI the spec calls for on RESOLVED/PARTIAL pages.
    resolved_via_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Child rows associated with this object, automatically removed when the parent row is deleted.
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
        """Deserialize the stored candidate list back into Python objects for the web layer."""
        if not self.candidates_json:
            return []
        try:
            return json.loads(self.candidates_json)
        except (TypeError, ValueError):
            return []

    @property
    def resolved_via(self) -> list[str]:
        """Deserialize the stored alias-chain trail (empty list if none was recorded)."""
        if not self.resolved_via_json:
            return []
        try:
            return json.loads(self.resolved_via_json)
        except (TypeError, ValueError):
            return []

    def to_dict(self) -> dict:
        """Create a JSON-friendly dictionary for API responses."""
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
            "resolved_via": self.resolved_via,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "identifiers": [identifier.to_dict() for identifier in self.identifiers],
            "planets": [planet.to_dict() for planet in self.planets],
        }


class IdentifierRecord(Base):
    """Stores a SIMBAD alias or identifier associated with an object record."""
    __tablename__ = "identifiers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"), nullable=False)
    catalog: Mapped[str] = mapped_column(String, nullable=False)
    identifier: Mapped[str] = mapped_column(String, nullable=False)
    matched_exoplanet_archive: Mapped[bool] = mapped_column(default=False)

    object: Mapped[ObjectRecord] = relationship(back_populates="identifiers")

    __table_args__ = (UniqueConstraint("object_id", "catalog", "identifier", name="uq_identifier"),)

    def to_dict(self) -> dict:
        """Serialize a single identifier row for API output."""
        return {
            "id": self.id,
            "catalog": self.catalog,
            "identifier": self.identifier,
            "matched_exoplanet_archive": self.matched_exoplanet_archive,
        }


class PlanetRecord(Base):
    """Stores planet information linked to a resolved object."""
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
        """Serialize a planet row for API output."""
        return {
            "id": self.id,
            "pl_name": self.pl_name,
            "pl_letter": self.pl_letter,
            "orbital_period_days": self.orbital_period_days,
            "planet_radius_earth": self.planet_radius_earth,
            "discovery_year": self.discovery_year,
            "discovery_method": self.discovery_method,
        }
