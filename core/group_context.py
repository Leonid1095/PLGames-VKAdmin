"""GroupContext — per-request context for multi-tenant handlers."""

from dataclasses import dataclass
from vkbottle import API


@dataclass
class GroupContext:
    """Holds group-specific data for a single request."""
    group_id: int
    api: API
    admin_vk_id: int
