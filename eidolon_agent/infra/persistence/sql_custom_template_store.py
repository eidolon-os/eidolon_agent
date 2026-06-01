"""SQL-backed store for operator-authored custom persona templates.

Phase 29.D. Built-in templates remain on disk under
``settings.persona.templates_dir`` — they're deployment artifacts that
ship with the agent image. This store is the *operator-mutable*
counterpart: created/forked/edited/deleted via admin's Templates UI.

The store deals only in raw YAML text (not parsed PersonaTemplate
models). Parsing happens in the registry layer when an actual
``PersonaTemplate`` is needed (e.g. for rendering). This separation
lets the operator save a syntactically-invalid YAML and get back a
useful validation error from the registry layer, instead of the store
silently dropping the write.

API surface mirrors PersonaTemplateRegistry's read-side so the latter
can delegate get / list / exists to either source uniformly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eidolon_agent.infra.persistence.models import PersonaTemplateCustomRow


# --- exception types -------------------------------------------------------


class CustomTemplateError(Exception):
    """Base — wrappers above this layer (HTTP handlers / orchestrators)
    map subclasses to status codes."""


class CustomTemplateNotFound(CustomTemplateError):
    pass


class CustomTemplateAlreadyExists(CustomTemplateError):
    pass


class CustomTemplateInUse(CustomTemplateError):
    """Raised on DELETE when at least one persona_instance still
    references this template_id. Includes a count so the caller can
    show "3 agents are using this template" in the error message.
    """

    def __init__(self, template_id: str, in_use_count: int) -> None:
        super().__init__(
            f"template {template_id!r} is in use by {in_use_count} persona "
            f"instance(s); delete or migrate those first"
        )
        self.template_id = template_id
        self.in_use_count = in_use_count


# --- value-object view ------------------------------------------------------


class CustomTemplateView:
    """Lightweight read DTO returned by list / get. Kept dataclass-like
    (plain Python) so the HTTP layer can convert to JSON freely without
    coupling to SQLAlchemy row objects."""

    __slots__ = (
        "template_id",
        "tenant_id",
        "display_name",
        "archetype",
        "yaml_body",
        "revision",
        "created_at",
        "updated_at",
    )

    def __init__(
        self,
        *,
        template_id: str,
        tenant_id: str,
        display_name: str,
        archetype: str,
        yaml_body: str,
        revision: int,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        self.template_id = template_id
        self.tenant_id = tenant_id
        self.display_name = display_name
        self.archetype = archetype
        self.yaml_body = yaml_body
        self.revision = revision
        self.created_at = created_at
        self.updated_at = updated_at

    @classmethod
    def from_row(cls, row: PersonaTemplateCustomRow) -> "CustomTemplateView":
        return cls(
            template_id=row.template_id,
            tenant_id=row.tenant_id,
            display_name=row.display_name,
            archetype=row.archetype,
            yaml_body=row.yaml_body,
            revision=row.revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def to_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "archetype": self.archetype,
            "yaml_body": self.yaml_body,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


# --- store -----------------------------------------------------------------


class SqlCustomTemplateStore:
    """Async CRUD over the ``persona_templates_custom`` table.

    All methods open a short-lived transaction. We do not cache —
    builtin templates ARE cached (they're loaded at boot), but customs
    are operator-mutable and the read volume is low (admin UI list,
    occasional render at agent-create time).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ---- reads -----------------------------------------------------------

    async def get(self, template_id: str) -> CustomTemplateView | None:
        async with self._session_factory() as session:
            row = await session.get(PersonaTemplateCustomRow, template_id)
            return None if row is None else CustomTemplateView.from_row(row)

    async def exists(self, template_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(PersonaTemplateCustomRow, template_id)
            return row is not None

    async def list_all(
        self, *, tenant_id: str | None = None
    ) -> list[CustomTemplateView]:
        """List all custom templates. ``tenant_id`` filters when provided;
        ``None`` means "all tenants" (admin's catalog view)."""
        async with self._session_factory() as session:
            stmt = select(PersonaTemplateCustomRow).order_by(
                PersonaTemplateCustomRow.created_at
            )
            if tenant_id is not None:
                stmt = stmt.where(PersonaTemplateCustomRow.tenant_id == tenant_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [CustomTemplateView.from_row(r) for r in rows]

    # ---- writes ----------------------------------------------------------

    async def create(
        self,
        *,
        template_id: str,
        tenant_id: str,
        display_name: str,
        archetype: str,
        yaml_body: str,
    ) -> CustomTemplateView:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            row = PersonaTemplateCustomRow(
                template_id=template_id,
                tenant_id=tenant_id,
                display_name=display_name,
                archetype=archetype,
                yaml_body=yaml_body,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                # PK collision — template_id already taken. Surface as a
                # business exception so the HTTP layer can emit 409.
                raise CustomTemplateAlreadyExists(
                    f"template {template_id!r} already exists"
                ) from exc
            return CustomTemplateView.from_row(row)

    async def update(
        self,
        template_id: str,
        *,
        display_name: str | None = None,
        yaml_body: str | None = None,
    ) -> CustomTemplateView:
        """Partial update. ``revision`` bumps by 1 on every successful PUT
        (even if both fields are None — keeps the audit story clean:
        every PUT counts as an edit even if no-op).
        """
        async with self._session_factory() as session:
            row = await session.get(PersonaTemplateCustomRow, template_id)
            if row is None:
                raise CustomTemplateNotFound(
                    f"template {template_id!r} not found"
                )
            if display_name is not None:
                row.display_name = display_name
            if yaml_body is not None:
                row.yaml_body = yaml_body
            row.revision = row.revision + 1
            row.updated_at = datetime.now(timezone.utc)
            await session.commit()
            return CustomTemplateView.from_row(row)

    async def delete(self, template_id: str) -> None:
        """Hard delete. Refcount enforcement lives at the orchestrator
        layer — by the time we reach here, the caller has already
        confirmed no persona_instance references this template."""
        async with self._session_factory() as session:
            row = await session.get(PersonaTemplateCustomRow, template_id)
            if row is None:
                raise CustomTemplateNotFound(
                    f"template {template_id!r} not found"
                )
            await session.delete(row)
            await session.commit()

    # ---- refcount helper -------------------------------------------------

    async def count_referring_instances(self, template_id: str) -> int:
        """How many ``persona_instances`` rows still reference this id?

        Used by the orchestrator to refuse DELETE when the template is
        still in use. Kept on this store (rather than in a separate
        ``PersonaInstanceStore`` call) so the orchestrator gets one
        coherent answer from one place.
        """
        # Avoid importing PersonaInstanceRow at module load time — keeps
        # this file's dependency graph minimal. Import inline.
        from sqlalchemy import func

        from eidolon_agent.infra.persistence.models import PersonaInstanceRow

        async with self._session_factory() as session:
            stmt = select(func.count()).select_from(PersonaInstanceRow).where(
                PersonaInstanceRow.template_id == template_id
            )
            return int((await session.execute(stmt)).scalar_one())
