"""Public metadata for a personal API key; never contains its digest or value."""

from dataclasses import dataclass


@dataclass(frozen=True)
class UserApiKey:
    id: str
    user_id: str
    name: str
    prefix: str
    scopes: tuple[str, ...]
    created_at: int
    expires_at: int | None
    last_used_at: int | None
    regenerated_at: int | None
