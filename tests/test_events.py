"""Tests for the registry event filters."""

from custom_components.light_buddy.manager import _entity_event_filter


def test_entity_event_filter() -> None:
    """Only changes that can move a light between rooms trigger a refresh."""
    assert _entity_event_filter({"action": "create", "entity_id": "light.a"})
    assert not _entity_event_filter({"action": "create", "entity_id": "switch.a"})
    assert _entity_event_filter(
        {"action": "update", "entity_id": "light.a", "changes": {"area_id": None}}
    )
    assert _entity_event_filter(
        {
            "action": "update",
            "entity_id": "light.b",
            "changes": {},
            "old_entity_id": "light.a",
        }
    )
    assert not _entity_event_filter(
        {"action": "update", "entity_id": "light.a", "changes": {"icon": None}}
    )
