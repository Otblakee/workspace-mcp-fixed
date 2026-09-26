"""Request-shape tests for the People API fixes found by the 2026-09-26 live
battery.

Three defects:

A. ``update_contact_group`` sent no etag, so ``contactGroups.update`` failed
   with HTTP 400 "Fingerprint is missing".
B. ``batch_update_contacts`` sent ``contacts`` as a list, so
   ``people.batchUpdateContacts`` failed with HTTP 400 "Cannot bind a list to
   map for field 'contacts'". The API wants a map keyed by resource name, each
   Person carrying its current etag, plus ``updateMask`` and ``readMask``.
C. A job title was rendered under the "Organization:" label in tool results.

Unit-scoped: the Google service is a MagicMock and the tools are exercised
through the ``_unwrap`` pattern used across this suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gcontacts import contacts_tools  # noqa: E402
from gcontacts.contacts_tools import (  # noqa: E402
    CONTACT_GROUP_FIELDS,
    DEFAULT_PERSON_FIELDS,
)


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


update_contact_group = _unwrap(contacts_tools.update_contact_group)
batch_update_contacts = _unwrap(contacts_tools.batch_update_contacts)
update_contact = _unwrap(contacts_tools.update_contact)
create_contact = _unwrap(contacts_tools.create_contact)
batch_create_contacts = _unwrap(contacts_tools.batch_create_contacts)

USER = "u@example.com"


def _http_error(status: int, message: str = "boom") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = message
    body = ('{"error": {"message": "%s"}}' % message).encode("utf-8")
    return HttpError(resp, body)


# ---------------------------------------------------------------------------
# Defect A: update_contact_group must send the current etag
# ---------------------------------------------------------------------------


class TestUpdateContactGroupEtag:
    def _service(self, get_result=None):
        service = MagicMock()
        groups = service.contactGroups.return_value
        groups.get.return_value.execute.return_value = (
            {"resourceName": "contactGroups/g1", "etag": "grp-etag-1", "name": "Old"}
            if get_result is None
            else get_result
        )
        groups.update.return_value.execute.return_value = {
            "resourceName": "contactGroups/g1",
            "name": "New name",
        }
        return service

    @pytest.mark.asyncio
    async def test_update_body_carries_etag_and_name(self):
        service = self._service()

        result = await update_contact_group(
            service=service, user_google_email=USER, group_id="g1", name="New name"
        )

        groups = service.contactGroups.return_value
        groups.get.assert_called_once()
        assert groups.get.call_args.kwargs["resourceName"] == "contactGroups/g1"

        groups.update.assert_called_once()
        kwargs = groups.update.call_args.kwargs
        assert kwargs["resourceName"] == "contactGroups/g1"
        body = kwargs["body"]
        assert body["contactGroup"] == {"etag": "grp-etag-1", "name": "New name"}
        assert body["updateGroupFields"] == "name"
        assert body["readGroupFields"] == CONTACT_GROUP_FIELDS
        assert "Name: New name" in result

    @pytest.mark.asyncio
    async def test_full_resource_name_is_not_double_prefixed(self):
        service = self._service()

        await update_contact_group(
            service=service,
            user_google_email=USER,
            group_id="contactGroups/g1",
            name="New name",
        )

        groups = service.contactGroups.return_value
        assert groups.get.call_args.kwargs["resourceName"] == "contactGroups/g1"
        assert groups.update.call_args.kwargs["resourceName"] == "contactGroups/g1"

    @pytest.mark.asyncio
    async def test_get_failure_surfaces_clear_message_and_skips_update(self):
        service = self._service()
        groups = service.contactGroups.return_value
        groups.get.return_value.execute.side_effect = _http_error(500, "backend")

        with pytest.raises(Exception) as excinfo:
            await update_contact_group(
                service=service, user_google_email=USER, group_id="g1", name="X"
            )

        assert "Could not fetch contact group g1" in str(excinfo.value)
        groups.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_404_reports_group_not_found(self):
        service = self._service()
        groups = service.contactGroups.return_value
        groups.get.return_value.execute.side_effect = _http_error(404, "gone")

        with pytest.raises(Exception) as excinfo:
            await update_contact_group(
                service=service, user_google_email=USER, group_id="g1", name="X"
            )

        assert "Contact group not found: g1" in str(excinfo.value)
        groups.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_etag_refuses_to_update(self):
        service = self._service(get_result={"resourceName": "contactGroups/g1"})

        with pytest.raises(Exception) as excinfo:
            await update_contact_group(
                service=service, user_google_email=USER, group_id="g1", name="X"
            )

        assert "no etag" in str(excinfo.value)
        service.contactGroups.return_value.update.assert_not_called()


# ---------------------------------------------------------------------------
# Defect B: batch_update_contacts must send a map keyed by resourceName
# ---------------------------------------------------------------------------


def _people_service(batch_get_responses, update_result=None):
    service = MagicMock()
    people = service.people.return_value
    people.getBatchGet.return_value.execute.return_value = {
        "responses": batch_get_responses
    }
    people.batchUpdateContacts.return_value.execute.return_value = {
        "updateResult": update_result or {}
    }
    return service


class TestBatchUpdateContactsRequestShape:
    @pytest.mark.asyncio
    async def test_contacts_is_a_map_keyed_by_resource_name_with_etags(self):
        service = _people_service(
            [
                {"person": {"resourceName": "people/c1", "etag": "etag-1"}},
                {"person": {"resourceName": "people/c2", "etag": "etag-2"}},
            ]
        )

        await batch_update_contacts(
            service=service,
            user_google_email=USER,
            updates=[
                {"contact_id": "c1", "email": "a@example.com"},
                {"contact_id": "people/c2", "email": "b@example.com"},
            ],
        )

        people = service.people.return_value
        people.batchUpdateContacts.assert_called_once()
        body = people.batchUpdateContacts.call_args.kwargs["body"]

        contacts = body["contacts"]
        assert isinstance(contacts, dict), "contacts must be a map, not a list"
        assert set(contacts) == {"people/c1", "people/c2"}
        assert contacts["people/c1"] == {
            "etag": "etag-1",
            "emailAddresses": [{"value": "a@example.com"}],
        }
        assert contacts["people/c2"]["etag"] == "etag-2"
        # The old list shape wrapped each body in {"person": ...}.
        for person in contacts.values():
            assert "person" not in person

        assert body["updateMask"] == "emailAddresses"
        assert body["readMask"] == DEFAULT_PERSON_FIELDS

    @pytest.mark.asyncio
    async def test_batch_get_requests_the_fields_being_updated(self):
        service = _people_service(
            [{"person": {"resourceName": "people/c1", "etag": "etag-1"}}]
        )

        await batch_update_contacts(
            service=service,
            user_google_email=USER,
            updates=[{"contact_id": "c1", "job_title": "CTO"}],
        )

        people = service.people.return_value
        get_kwargs = people.getBatchGet.call_args.kwargs
        assert get_kwargs["resourceNames"] == ["people/c1"]
        requested = set(get_kwargs["personFields"].split(","))
        assert {"names", "emailAddresses", "phoneNumbers", "organizations"} <= (
            requested
        )
        assert "metadata" in requested

    @pytest.mark.asyncio
    async def test_organization_and_title_land_in_the_right_fields(self):
        service = _people_service(
            [{"person": {"resourceName": "people/c1", "etag": "etag-1"}}]
        )

        await batch_update_contacts(
            service=service,
            user_google_email=USER,
            updates=[{"contact_id": "c1", "organization": "Acme", "job_title": "CTO"}],
        )

        body = service.people.return_value.batchUpdateContacts.call_args.kwargs["body"]
        assert body["contacts"]["people/c1"]["organizations"] == [
            {"name": "Acme", "title": "CTO"}
        ]
        assert body["updateMask"] == "organizations"

    @pytest.mark.asyncio
    async def test_per_contact_failure_in_update_result_is_reported(self):
        service = _people_service(
            [
                {"person": {"resourceName": "people/c1", "etag": "etag-1"}},
                {"person": {"resourceName": "people/c2", "etag": "etag-2"}},
            ],
            update_result={
                "people/c1": {
                    "person": {
                        "resourceName": "people/c1",
                        "names": [{"displayName": "Alice"}],
                    },
                    "status": {"code": 0},
                },
                "people/c2": {
                    "status": {"code": 5, "message": "Requested entity was not found."}
                },
            },
        )

        result = await batch_update_contacts(
            service=service,
            user_google_email=USER,
            updates=[
                {"contact_id": "c1", "given_name": "Alice"},
                {"contact_id": "c2", "given_name": "Bob"},
            ],
        )

        assert "Updated 1 contacts" in result
        assert "Name: Alice" in result
        assert "Not updated (1):" in result
        assert "people/c2: Requested entity was not found." in result

    @pytest.mark.asyncio
    async def test_contact_missing_from_batch_get_is_reported_not_sent(self):
        service = _people_service(
            [
                {"person": {"resourceName": "people/c1", "etag": "etag-1"}},
                {
                    "requestedResourceName": "people/c9",
                    "status": {"code": 5, "message": "not found"},
                },
            ]
        )

        result = await batch_update_contacts(
            service=service,
            user_google_email=USER,
            updates=[
                {"contact_id": "c1", "email": "a@example.com"},
                {"contact_id": "c9", "email": "z@example.com"},
            ],
        )

        body = service.people.return_value.batchUpdateContacts.call_args.kwargs["body"]
        assert set(body["contacts"]) == {"people/c1"}
        assert "Not updated (1):" in result
        assert "people/c9: not found" in result

    @pytest.mark.asyncio
    async def test_all_contacts_unresolvable_raises(self):
        service = _people_service([])

        with pytest.raises(Exception) as excinfo:
            await batch_update_contacts(
                service=service,
                user_google_email=USER,
                updates=[{"contact_id": "c1", "email": "a@example.com"}],
            )

        assert "No valid update data provided" in str(excinfo.value)
        service.people.return_value.batchUpdateContacts.assert_not_called()


# ---------------------------------------------------------------------------
# Defect C: job title vs organization in request bodies and result text
# ---------------------------------------------------------------------------


def _echo_person(body, resource_name="people/c1"):
    """Return what People would: the body with a resourceName, etag stripped."""
    person = {k: v for k, v in body.items() if k != "etag"}
    person["resourceName"] = resource_name
    return person


class TestJobTitleAndOrganizationMapping:
    @pytest.mark.asyncio
    async def test_update_contact_job_title_only(self):
        service = MagicMock()
        people = service.people.return_value
        people.get.return_value.execute.return_value = {
            "resourceName": "people/c1",
            "etag": "etag-1",
        }
        people.updateContact.return_value.execute.side_effect = lambda: _echo_person(
            people.updateContact.call_args.kwargs["body"]
        )

        result = await update_contact(
            service=service,
            user_google_email=USER,
            contact_id="c1",
            job_title="MCP test fixture",
        )

        kwargs = people.updateContact.call_args.kwargs
        assert kwargs["body"]["organizations"] == [{"title": "MCP test fixture"}]
        assert kwargs["body"]["etag"] == "etag-1"
        assert kwargs["updatePersonFields"] == "organizations"

        assert "Job title: MCP test fixture" in result
        assert "Organization: MCP test fixture" not in result
        assert "Organization:" not in result

    @pytest.mark.asyncio
    async def test_update_contact_organization_and_title(self):
        service = MagicMock()
        people = service.people.return_value
        people.get.return_value.execute.return_value = {
            "resourceName": "people/c1",
            "etag": "etag-1",
        }
        people.updateContact.return_value.execute.side_effect = lambda: _echo_person(
            people.updateContact.call_args.kwargs["body"]
        )

        result = await update_contact(
            service=service,
            user_google_email=USER,
            contact_id="c1",
            organization="Acme Ltd",
            job_title="Engineer",
        )

        body = people.updateContact.call_args.kwargs["body"]
        assert body["organizations"] == [{"name": "Acme Ltd", "title": "Engineer"}]
        assert "Organization: Acme Ltd" in result
        assert "Job title: Engineer" in result

    @pytest.mark.asyncio
    async def test_create_contact_mapping_and_rendering(self):
        service = MagicMock()
        people = service.people.return_value
        people.createContact.return_value.execute.side_effect = lambda: _echo_person(
            people.createContact.call_args.kwargs["body"]
        )

        result = await create_contact(
            service=service,
            user_google_email=USER,
            given_name="Jane",
            organization="Acme Ltd",
            job_title="Engineer",
        )

        body = people.createContact.call_args.kwargs["body"]
        assert body["organizations"] == [{"name": "Acme Ltd", "title": "Engineer"}]
        assert "Organization: Acme Ltd" in result
        assert "Job title: Engineer" in result

    @pytest.mark.asyncio
    async def test_create_contact_job_title_only_never_labelled_organization(self):
        service = MagicMock()
        people = service.people.return_value
        people.createContact.return_value.execute.side_effect = lambda: _echo_person(
            people.createContact.call_args.kwargs["body"]
        )

        result = await create_contact(
            service=service,
            user_google_email=USER,
            given_name="Jane",
            job_title="Engineer",
        )

        body = people.createContact.call_args.kwargs["body"]
        assert body["organizations"] == [{"title": "Engineer"}]
        assert "Job title: Engineer" in result
        assert "Organization:" not in result

    @pytest.mark.asyncio
    async def test_batch_create_contacts_mapping(self):
        service = MagicMock()
        people = service.people.return_value

        def _created():
            body = people.batchCreateContacts.call_args.kwargs["body"]
            return {
                "createdPeople": [
                    {"person": _echo_person(c["contactPerson"], f"people/c{i}")}
                    for i, c in enumerate(body["contacts"])
                ]
            }

        people.batchCreateContacts.return_value.execute.side_effect = _created

        result = await batch_create_contacts(
            service=service,
            user_google_email=USER,
            contacts=[
                {"given_name": "A", "organization": "Acme", "job_title": "CTO"},
                {"given_name": "B", "job_title": "Analyst"},
            ],
        )

        body = people.batchCreateContacts.call_args.kwargs["body"]
        orgs = [c["contactPerson"]["organizations"] for c in body["contacts"]]
        assert orgs == [[{"name": "Acme", "title": "CTO"}], [{"title": "Analyst"}]]
        assert "Organization: Acme" in result
        assert "Job title: CTO" in result
        assert "Job title: Analyst" in result
        assert "Organization: Analyst" not in result
