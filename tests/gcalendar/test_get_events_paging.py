"""get_events must say when the Calendar API paged the result.

``events.list`` returns at most ``maxResults`` items and a ``nextPageToken``
when more exist. The tool used to drop the token, so a range with more
events than ``max_results`` read as complete. The default page size is
unchanged; the tool now appends one line when the token is present.

The Calendar service is a MagicMock; nothing touches the network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gcalendar import calendar_tools

USER = "oliver@otbgroup.co.uk"
MORE_LINE = "More results available: raise max_results or narrow the range."


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


get_events = _unwrap(calendar_tools.get_events)


def _event(event_id: str) -> dict:
    return {
        "id": event_id,
        "summary": f"Event {event_id}",
        "start": {"dateTime": "2026-09-26T09:00:00Z"},
        "end": {"dateTime": "2026-09-26T10:00:00Z"},
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
    }


def _service(list_response: dict) -> MagicMock:
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = list_response
    return service


class TestGetEventsPaging:
    @pytest.mark.asyncio
    async def test_next_page_token_adds_the_more_results_line(self):
        service = _service(
            {"items": [_event("e1"), _event("e2")], "nextPageToken": "tok-2"}
        )

        result = await get_events(
            service=service,
            user_google_email=USER,
            max_results=2,
        )

        assert "Successfully retrieved 2 events" in result
        assert result.rstrip().endswith(MORE_LINE)
        assert result.count(MORE_LINE) == 1
        # Default page size passes through untouched.
        assert service.events.return_value.list.call_args.kwargs["maxResults"] == 2

    @pytest.mark.asyncio
    async def test_no_token_means_no_line(self):
        service = _service({"items": [_event("e1")]})

        result = await get_events(service=service, user_google_email=USER)

        assert "Successfully retrieved 1 events" in result
        assert "More results available" not in result
        assert service.events.return_value.list.call_args.kwargs["maxResults"] == 25

    @pytest.mark.asyncio
    async def test_detailed_listing_also_carries_the_line(self):
        service = _service({"items": [_event("e1")], "nextPageToken": "tok"})

        result = await get_events(
            service=service,
            user_google_email=USER,
            max_results=1,
            detailed=True,
        )

        assert "Attendee Details" in result
        assert result.rstrip().endswith(MORE_LINE)

    @pytest.mark.asyncio
    async def test_single_event_lookup_never_prints_the_line(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = _event("e9")

        result = await get_events(
            service=service,
            user_google_email=USER,
            event_id="e9",
        )

        assert "Successfully retrieved event" in result
        assert "More results available" not in result
        service.events.return_value.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_max_results_is_still_25(self):
        import inspect

        assert inspect.signature(get_events).parameters["max_results"].default == 25
