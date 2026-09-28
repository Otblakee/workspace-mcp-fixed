"""find_and_replace_doc short find_text needs confirm (tier 3, item 7).

A find_text shorter than 3 characters matches all over a document, so it is
refused unless confirm=True. Three characters or more runs as before.

The Docs service is a MagicMock; nothing touches the network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.utils import UserInputError
from gdocs import docs_tools

USER = "oliver@otbgroup.co.uk"


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


find_and_replace_doc = _unwrap(docs_tools.find_and_replace_doc)


def _service(changed: int = 4) -> MagicMock:
    service = MagicMock()
    service.documents.return_value.batchUpdate.return_value.execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": changed}}]
    }
    return service


def _batch_calls(service: MagicMock):
    return service.documents.return_value.batchUpdate.call_args_list


class TestShortFindTextNeedsConfirm:
    @pytest.mark.parametrize("find_text", ["a", "ab", "", " "])
    @pytest.mark.asyncio
    async def test_short_find_text_refused_without_confirm(self, find_text):
        service = _service()

        with pytest.raises(UserInputError) as excinfo:
            await find_and_replace_doc(
                service, USER, document_id="doc1", find_text=find_text, replace_text="x"
            )

        message = str(excinfo.value)
        assert "Refused" in message
        assert repr(find_text) in message
        assert "shorter than 3 characters" in message
        assert "confirm=True" in message
        assert _batch_calls(service) == []

    @pytest.mark.asyncio
    async def test_short_find_text_with_confirm_proceeds(self):
        service = _service(changed=12)

        result = await find_and_replace_doc(
            service, USER, "doc1", find_text="ab", replace_text="xy", confirm=True
        )

        assert "Replaced 12 occurrence(s) of 'ab' with 'xy'" in result
        (call,) = _batch_calls(service)
        request = call.kwargs["body"]["requests"][0]["replaceAllText"]
        assert request["containsText"]["text"] == "ab"
        assert request["replaceText"] == "xy"

    @pytest.mark.asyncio
    async def test_three_character_find_text_needs_no_confirm(self):
        service = _service(changed=1)

        result = await find_and_replace_doc(
            service, USER, "doc1", find_text="abc", replace_text="xyz"
        )

        assert "Replaced 1 occurrence(s)" in result
        assert len(_batch_calls(service)) == 1

    def test_threshold_constant(self):
        assert docs_tools.MIN_FIND_TEXT_CHARS == 3
