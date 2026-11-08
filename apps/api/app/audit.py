from typing import Any

from sqlalchemy.orm import Session as DBSession

from app.models import AuditLog, User


def audit(
    db: DBSession,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: Any = None,
    **metadata: Any,
) -> None:
    """Add an audit row to the caller's transaction (committed with the change)."""
    db.add(
        AuditLog(
            actor_id=actor.id if actor else None,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            meta={k: (str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v)
                  for k, v in metadata.items()},
        )
    )
