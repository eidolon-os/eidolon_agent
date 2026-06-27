"""Custom persona-template DTOs shared by persistence adapters."""

from __future__ import annotations

from datetime import datetime


class CustomTemplateError(Exception):
    """Base error for custom-template persistence."""


class CustomTemplateNotFound(CustomTemplateError):
    pass


class CustomTemplateAlreadyExists(CustomTemplateError):
    pass


class CustomTemplateInUse(CustomTemplateError):
    def __init__(self, template_id: str, in_use_count: int) -> None:
        super().__init__(
            f"template {template_id!r} is in use by {in_use_count} persona "
            "instance(s); delete or migrate those first"
        )
        self.template_id = template_id
        self.in_use_count = in_use_count


class CustomTemplateView:
    """Lightweight read DTO returned by custom-template stores."""

    __slots__ = (
        "archetype",
        "created_at",
        "display_name",
        "revision",
        "template_id",
        "tenant_id",
        "updated_at",
        "yaml_body",
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


__all__ = [
    "CustomTemplateAlreadyExists",
    "CustomTemplateError",
    "CustomTemplateInUse",
    "CustomTemplateNotFound",
    "CustomTemplateView",
]
