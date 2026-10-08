"""
Outbound Communication Safety Guard.

NON-NEGOTIABLE: AI must never trigger outbound communication from Odoo.
This module enforces that rule at the code level — the first of three layers
(code block → human approval → Odoo permissions).

See Architecture Document Section 5.4 for full specification.
"""

import logging
from typing import Any

from src.mcp.odoo.utils.safety_blocked_methods import (
    BLOCKED_METHODS,
    BLOCKED_MODELS,
    INTERNAL_NOTE_SUBTYPE_ID,
    SAFE_MESSAGE_TYPES,
)

logger = logging.getLogger(__name__)

class SafetyViolation(Exception):
    """Raised when an operation would trigger outbound communication."""

    def __init__(self, operation: str, reason: str):
        self.operation = operation
        self.reason = reason
        super().__init__(f"BLOCKED: {operation} — {reason}")


class SafetyGuard:
    """
    Validates all Odoo write operations before execution.

    Usage:
        guard = SafetyGuard()

        # Before any write operation:
        guard.validate_write(model, method, kwargs)

        # Before message_post:
        guard.validate_message_post(kwargs)
    """

    def validate_write(self, model: str, method: str, kwargs: dict[str, Any] | None = None) -> None:
        """
        Validate that a write operation is safe (no outbound communication).

        Args:
            model: Odoo model name (e.g. 'crm.lead', 'mail.mail')
            method: Method being called (e.g. 'write', 'message_post')
            kwargs: Keyword arguments being passed to the method

        Raises:
            SafetyViolation: If the operation would trigger outbound communication
        """
        kwargs = kwargs or {}

        # Block entire models that are inherently outbound
        if model in BLOCKED_MODELS:
            raise SafetyViolation(
                f"{model}.{method}",
                f"Model '{model}' is blocked because it triggers outbound communication. "
                f"See Architecture Section 5.4."
            )

        # Block specific methods that send emails/SMS
        if method in BLOCKED_METHODS:
            raise SafetyViolation(
                f"{model}.{method}",
                f"Method '{method}' is blocked because it triggers outbound communication. "
                f"Use internal alternatives (e.g. message_post with message_type='note')."
            )

        # Special handling for message_post — only 'note' is allowed
        if method == "message_post":
            self.validate_message_post(kwargs)

        # Special handling for calendar events with attendees
        if model == "calendar.event" and method in ("create", "write"):
            self._validate_calendar_event(kwargs)

    def validate_message_post(self, kwargs: dict[str, Any]) -> None:
        """
        Validate that message_post uses internal-only message type.

        Supports both Odoo 18 (message_type='note') and Odoo 19
        (message_type='comment' + subtype_id=2).

        Args:
            kwargs: Arguments to message_post

        Raises:
            SafetyViolation: If message_type would send external notifications
        """
        message_type = kwargs.get("message_type", "comment")  # Odoo defaults to 'comment'!
        subtype_id = kwargs.get("subtype_id")
        subtype_xmlid = kwargs.get("subtype_xmlid", "")

        # Odoo 18 path: message_type='note' is always safe
        if message_type in SAFE_MESSAGE_TYPES:
            # Still validate subtype_xmlid if provided
            if subtype_xmlid and subtype_xmlid != "mail.mt_note":
                raise SafetyViolation(
                    f"message_post(subtype_xmlid='{subtype_xmlid}')",
                    "Only 'mail.mt_note' subtype is allowed with message_type='note'. "
                    "Other subtypes may trigger notifications to followers."
                )
        # Odoo 19 path: message_type='comment' + subtype_id=2 (internal Note)
        elif message_type == "comment":
            if subtype_id == INTERNAL_NOTE_SUBTYPE_ID:
                pass  # Safe: internal note via Odoo 19 mechanism
            elif subtype_xmlid == "mail.mt_note":
                pass  # Safe: internal note via xmlid reference
            else:
                raise SafetyViolation(
                    "message_post(message_type='comment')",
                    f"message_type='comment' sends emails to followers/external contacts. "
                    f"For internal notes, use message_type='comment' with subtype_id={INTERNAL_NOTE_SUBTYPE_ID} "
                    f"(Odoo 19) or message_type='note' (Odoo 18)."
                )
        else:
            raise SafetyViolation(
                f"message_post(message_type='{message_type}')",
                f"message_type='{message_type}' is not allowed. "
                f"Use message_type='comment' with subtype_id={INTERNAL_NOTE_SUBTYPE_ID} for internal notes."
            )

        # Block partner_ids in message_post (would notify those partners)
        if kwargs.get("partner_ids"):
            raise SafetyViolation(
                "message_post(partner_ids=...)",
                "Specifying partner_ids in message_post sends notifications to those partners. "
                "Remove partner_ids for internal-only notes."
            )

    def _validate_calendar_event(self, kwargs: dict[str, Any]) -> None:
        """
        Validate calendar event operations.

        Calendar events WITHOUT attendees = safe (personal reminders).
        Calendar events WITH attendees = outbound (sends email invitations).

        Events with attendees require a separate express approval step,
        distinct from the standard write-back approval.
        """
        partner_ids = kwargs.get("partner_ids", [])
        attendee_ids = kwargs.get("attendee_ids", [])

        if partner_ids or attendee_ids:
            raise SafetyViolation(
                "calendar.event with attendees",
                "Calendar events with attendees send email invitations to all participants. "
                "This requires express approval. Create the event without attendees first, "
                "then request explicit permission to add attendees and send invitations."
            )

    def validate_model_access(self, model: str, method: str) -> None:
        """
        Quick check: is this model+method combination safe at all?
        Call this before even preparing the data.

        Args:
            model: Odoo model name
            method: Method name

        Raises:
            SafetyViolation: If the combination is inherently unsafe
        """
        if model in BLOCKED_MODELS:
            raise SafetyViolation(
                f"{model}.{method}",
                f"Model '{model}' is entirely blocked for AI access."
            )
        if method in BLOCKED_METHODS:
            raise SafetyViolation(
                f"{model}.{method}",
                f"Method '{method}' is blocked for AI access."
            )
