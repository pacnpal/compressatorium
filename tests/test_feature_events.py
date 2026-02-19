import pytest

from app.models import FeatureEventRequest
from app.routes import info as info_routes


@pytest.mark.asyncio
async def test_track_feature_event_counts():
    info_routes._feature_event_counts.clear()

    first = await info_routes.track_feature_event(
        FeatureEventRequest(event="conversion_preset_saved"),
    )
    second = await info_routes.track_feature_event(
        FeatureEventRequest(event="conversion_preset_saved"),
    )
    third = await info_routes.track_feature_event(
        FeatureEventRequest(event="conversion_preset_applied", value=3),
    )
    fourth = await info_routes.track_feature_event(
        FeatureEventRequest(event="auto_queue_folder_queued", value=5),
    )
    snapshot = await info_routes.list_feature_events()

    assert first["total"] == 1
    assert second["total"] == 2
    assert third["total"] == 3
    assert fourth["total"] == 5
    assert snapshot["events"]["conversion_preset_saved"] == 2
    assert snapshot["events"]["conversion_preset_applied"] == 3
    assert snapshot["events"]["auto_queue_folder_queued"] == 5


@pytest.mark.asyncio
async def test_track_feature_event_normalizes_invalid_value():
    info_routes._feature_event_counts.clear()
    result = await info_routes.track_feature_event(
        FeatureEventRequest(event="conversion_preset_saved", value=0),
    )
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_track_feature_event_rejects_unknown_event():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.track_feature_event(
            FeatureEventRequest(event="unknown_event"),
        )

    assert exc_info.value.status_code == 400
