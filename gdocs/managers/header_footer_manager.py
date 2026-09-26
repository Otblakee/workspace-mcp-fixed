"""
Header Footer Manager

This module provides high-level operations for managing headers and footers
in Google Docs, extracting complex logic from the main tools module.
"""

import logging
import asyncio
from typing import Any, Optional

from gdocs.docs_helpers import create_insert_text_segment_request

logger = logging.getLogger(__name__)


class HeaderFooterManager:
    """
    High-level manager for Google Docs header and footer operations.

    Handles complex header/footer operations including:
    - Finding and updating existing headers/footers
    - Content replacement with proper range calculation
    - Section type management
    """

    def __init__(self, service):
        """
        Initialize the header footer manager.

        Args:
            service: Google Docs API service instance
        """
        self.service = service

    async def update_header_footer_content(
        self,
        document_id: str,
        section_type: str,
        content: str,
        header_footer_type: str = "DEFAULT",
    ) -> tuple[bool, str]:
        """
        Updates header or footer content in a document.

        This method extracts the complex logic from update_doc_headers_footers tool function.

        Args:
            document_id: ID of the document to update
            section_type: Type of section ("header" or "footer")
            content: New content for the section
            header_footer_type: Type of header/footer ("DEFAULT", "FIRST_PAGE_ONLY", "EVEN_PAGE")

        Returns:
            Tuple of (success, message)
        """
        logger.info(f"Updating {section_type} in document {document_id}")

        # Validate section type
        if section_type not in ["header", "footer"]:
            return False, "section_type must be 'header' or 'footer'"

        # Validate header/footer type
        if header_footer_type not in ["DEFAULT", "FIRST_PAGE_ONLY", "EVEN_PAGE"]:
            return (
                False,
                "header_footer_type must be 'DEFAULT', 'FIRST_PAGE_ONLY', or 'EVEN_PAGE'",
            )

        try:
            # Get document structure
            doc = await self._get_document(document_id)

            # Find the target section
            target_section, section_id = await self._find_target_section(
                doc, section_type, header_footer_type
            )

            if not target_section:
                # Nothing to update yet. The Docs API can create a DEFAULT
                # header or footer (createHeader / createFooter); first page
                # and even page variants have no create request, so those
                # still need to be switched on in Google Docs first.
                if header_footer_type != "DEFAULT":
                    return (
                        False,
                        f"No {section_type} found in document and the Docs API can only "
                        f"create a DEFAULT {section_type}, not {header_footer_type}. "
                        f"Turn on the {header_footer_type} {section_type} in Google Docs "
                        f"first (File > Page setup or the header/footer options), then call again.",
                    )

                section_id = await self._create_section(document_id, section_type)
                await self._insert_into_new_section(document_id, section_id, content)
                return (
                    True,
                    f"Created {section_type} ({section_id}) and set its content in document {document_id}",
                )

            # Update the content
            success = await self._replace_section_content(
                document_id, target_section, section_id, content
            )

            if success:
                return True, f"Updated {section_type} content in document {document_id}"
            else:
                return (
                    False,
                    f"Could not find content structure in {section_type} to update",
                )

        except Exception as e:
            logger.error(f"Failed to update {section_type}: {str(e)}")
            return False, f"Failed to update {section_type}: {str(e)}"

    async def _create_section(self, document_id: str, section_type: str) -> str:
        """
        Create a DEFAULT header or footer and return its segment ID.

        Sends a single createHeader / createFooter request and reads the new
        ID from the batchUpdate response (replies[0].createHeader.headerId or
        replies[0].createFooter.footerId).

        Raises:
            KeyError: if the response carries no ID for the new section.
        """
        request_key = "createHeader" if section_type == "header" else "createFooter"
        id_key = "headerId" if section_type == "header" else "footerId"

        response = await asyncio.to_thread(
            self.service.documents()
            .batchUpdate(
                documentId=document_id,
                body={"requests": [{request_key: {"type": "DEFAULT"}}]},
            )
            .execute
        )

        replies = (response or {}).get("replies") or []
        section_id = None
        for reply in replies:
            if isinstance(reply, dict) and request_key in reply:
                section_id = reply[request_key].get(id_key)
                break
        if not section_id:
            raise KeyError(
                f"{request_key} succeeded but the response carried no {id_key}: {response!r}"
            )
        logger.info(f"Created {section_type} {section_id} in document {document_id}")
        return section_id

    async def _insert_into_new_section(
        self, document_id: str, section_id: str, content: str
    ) -> None:
        """Insert content at index 0 of a freshly created (empty) header or footer."""
        await asyncio.to_thread(
            self.service.documents()
            .batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        create_insert_text_segment_request(0, content, section_id)
                    ]
                },
            )
            .execute
        )

    async def _get_document(self, document_id: str) -> dict[str, Any]:
        """Get the full document data."""
        return await asyncio.to_thread(
            self.service.documents().get(documentId=document_id).execute
        )

    async def _find_target_section(
        self, doc: dict[str, Any], section_type: str, header_footer_type: str
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """
        Find the target header or footer section.

        The Header and Footer objects in ``doc["headers"]`` / ``doc["footers"]``
        carry no type, and every segment id is an opaque ``kix.`` string, so
        neither can say which one is the first-page or even-page variant. The
        only place that mapping lives is ``doc["documentStyle"]``:
        ``defaultHeaderId`` / ``firstPageHeaderId`` / ``evenPageHeaderId`` and
        the three footer equivalents. Read the id for the requested type
        there, then look it up in the headers / footers map.

        Args:
            doc: Document data
            section_type: "header" or "footer"
            header_footer_type: "DEFAULT", "FIRST_PAGE_ONLY" or "EVEN_PAGE"

        Returns:
            Tuple of (section_data, section_id), or (None, None) when the
            document has no header / footer of that type. There is no
            fallback to another type or to "the first one found": a
            missing id means the section does not exist, and the caller
            decides whether it can be created (DEFAULT) or must be switched
            on in Google Docs first (FIRST_PAGE_ONLY, EVEN_PAGE).
        """
        section_id = self._section_id_from_document_style(
            doc, section_type, header_footer_type
        )
        if not section_id:
            return None, None

        sections = doc.get("headers" if section_type == "header" else "footers") or {}
        section_data = sections.get(section_id)
        if section_data is None:
            # documentStyle names an id the map does not carry. Treat it as
            # missing rather than guessing at another section.
            logger.warning(
                f"documentStyle names {section_type} {section_id} for "
                f"{header_footer_type} but the document has no such {section_type}"
            )
            return None, None
        return section_data, section_id

    # documentStyle field per (section_type, header_footer_type).
    _DOCUMENT_STYLE_ID_FIELDS = {
        ("header", "DEFAULT"): "defaultHeaderId",
        ("header", "FIRST_PAGE_ONLY"): "firstPageHeaderId",
        ("header", "EVEN_PAGE"): "evenPageHeaderId",
        ("footer", "DEFAULT"): "defaultFooterId",
        ("footer", "FIRST_PAGE_ONLY"): "firstPageFooterId",
        ("footer", "EVEN_PAGE"): "evenPageFooterId",
    }

    @classmethod
    def _section_id_from_document_style(
        cls, doc: dict[str, Any], section_type: str, header_footer_type: str
    ) -> Optional[str]:
        """The segment id documentStyle records for this header/footer type."""
        field = cls._DOCUMENT_STYLE_ID_FIELDS.get((section_type, header_footer_type))
        if field is None:
            return None
        style = doc.get("documentStyle") or {}
        if not isinstance(style, dict):
            return None
        value = style.get(field)
        return value if isinstance(value, str) and value else None

    async def _replace_section_content(
        self,
        document_id: str,
        section: dict[str, Any],
        section_id: str,
        new_content: str,
    ) -> bool:
        """
        Replace the content in a header or footer section.

        Args:
            document_id: Document ID
            section: Section data containing content elements
            section_id: Segment ID of the header/footer (targets the requests
                at the section instead of the document body)
            new_content: New content to insert

        Returns:
            True if successful, False otherwise
        """
        content_elements = section.get("content", [])
        if not content_elements:
            return False

        # Find the first paragraph to replace content
        first_para = self._find_first_paragraph(content_elements)
        if not first_para:
            return False

        # Calculate content range
        start_index = first_para.get("startIndex", 0)
        end_index = first_para.get("endIndex", 0)

        # Build requests to replace content
        requests = []

        # Delete existing content if any (preserve paragraph structure)
        if end_index > start_index:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {
                            "segmentId": section_id,
                            "startIndex": start_index,
                            "endIndex": end_index - 1,  # Keep the paragraph end marker
                        }
                    }
                }
            )

        # Insert new content
        requests.append(
            create_insert_text_segment_request(start_index, new_content, section_id)
        )

        try:
            await asyncio.to_thread(
                self.service.documents()
                .batchUpdate(documentId=document_id, body={"requests": requests})
                .execute
            )
            return True

        except Exception as e:
            logger.error(f"Failed to replace section content: {str(e)}")
            return False

    def _find_first_paragraph(
        self, content_elements: list[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        """Find the first paragraph element in content."""
        for element in content_elements:
            if "paragraph" in element:
                return element
        return None

    async def get_header_footer_info(self, document_id: str) -> dict[str, Any]:
        """
        Get information about all headers and footers in the document.

        Args:
            document_id: Document ID

        Returns:
            Dictionary with header and footer information
        """
        try:
            doc = await self._get_document(document_id)

            headers_info = {}
            for header_id, header_data in doc.get("headers", {}).items():
                headers_info[header_id] = self._extract_section_info(header_data)

            footers_info = {}
            for footer_id, footer_data in doc.get("footers", {}).items():
                footers_info[footer_id] = self._extract_section_info(footer_data)

            return {
                "headers": headers_info,
                "footers": footers_info,
                "has_headers": bool(headers_info),
                "has_footers": bool(footers_info),
            }

        except Exception as e:
            logger.error(f"Failed to get header/footer info: {str(e)}")
            return {"error": str(e)}

    def _extract_section_info(self, section_data: dict[str, Any]) -> dict[str, Any]:
        """Extract useful information from a header/footer section."""
        content_elements = section_data.get("content", [])

        # Extract text content
        text_content = ""
        for element in content_elements:
            if "paragraph" in element:
                para = element["paragraph"]
                for para_element in para.get("elements", []):
                    if "textRun" in para_element:
                        text_content += para_element["textRun"].get("content", "")

        return {
            "content_preview": text_content[:100] if text_content else "(empty)",
            "element_count": len(content_elements),
            "start_index": content_elements[0].get("startIndex", 0)
            if content_elements
            else 0,
            "end_index": content_elements[-1].get("endIndex", 0)
            if content_elements
            else 0,
        }

    async def create_header_footer(
        self, document_id: str, section_type: str, header_footer_type: str = "DEFAULT"
    ) -> tuple[bool, str]:
        """
        Create a new header or footer section.

        Args:
            document_id: Document ID
            section_type: "header" or "footer"
            header_footer_type: Type of header/footer ("DEFAULT", "FIRST_PAGE", or "EVEN_PAGE")

        Returns:
            Tuple of (success, message)
        """
        if section_type not in ["header", "footer"]:
            return False, "section_type must be 'header' or 'footer'"

        # Map our type names to API type names
        type_mapping = {
            "DEFAULT": "DEFAULT",
            "FIRST_PAGE": "FIRST_PAGE",
            "EVEN_PAGE": "EVEN_PAGE",
            "FIRST_PAGE_ONLY": "FIRST_PAGE",  # Support legacy name
        }

        api_type = type_mapping.get(header_footer_type, header_footer_type)
        if api_type not in ["DEFAULT", "FIRST_PAGE", "EVEN_PAGE"]:
            return (
                False,
                "header_footer_type must be 'DEFAULT', 'FIRST_PAGE', or 'EVEN_PAGE'",
            )

        try:
            # Build the request
            request = {"type": api_type}

            # Create the appropriate request type
            if section_type == "header":
                batch_request = {"createHeader": request}
            else:
                batch_request = {"createFooter": request}

            # Execute the request
            await asyncio.to_thread(
                self.service.documents()
                .batchUpdate(documentId=document_id, body={"requests": [batch_request]})
                .execute
            )

            return True, f"Successfully created {section_type} with type {api_type}"

        except Exception as e:
            error_msg = str(e)
            if "already exists" in error_msg.lower():
                return (
                    False,
                    f"A {section_type} of type {api_type} already exists in the document",
                )
            return False, f"Failed to create {section_type}: {error_msg}"
