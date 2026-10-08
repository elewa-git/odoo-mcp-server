from __future__ import annotations

import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest

from src.core.identity import Principal, PrincipalKind
from src.core.onboarding import (
    OnboardingContinuation,
    OnboardingFailureCode,
    OnboardingResult,
    OnboardingStatus,
    OnboardingUnavailableError,
    UnavailableOnboardingProvider,
)

SECRET_FIELD_MARKERS = ("api_key", "credential", "password", "secret", "token")
SECRET_VALUES = ("odoo-api-key-value", "super-secret-password", "oauth-bearer-token")


def _expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=5)


def test_continuation_has_a_json_safe_representation_without_secret_fields_or_values() -> None:
    continuation = OnboardingContinuation(
        onboarding_id="onboarding-1",
        url="https://onboarding.example/continue/onboarding-1",
        expires_at=_expires_at(),
    )

    serialized = json.dumps(continuation.to_public_dict())

    assert all(marker not in field.name for field in fields(continuation) for marker in SECRET_FIELD_MARKERS)
    assert all(value not in serialized for value in SECRET_VALUES)
    assert all(value not in repr(continuation) for value in SECRET_VALUES)


def test_result_has_a_json_safe_representation_without_secret_fields_or_values() -> None:
    result = OnboardingResult(
        onboarding_id="onboarding-1",
        status=OnboardingStatus.FAILED,
        failure_code=OnboardingFailureCode.VALIDATION_FAILED,
        completed_at=datetime.now(UTC),
    )

    serialized = json.dumps(result.to_public_dict())

    assert all(marker not in field.name for field in fields(result) for marker in SECRET_FIELD_MARKERS)
    assert all(value not in serialized for value in SECRET_VALUES)
    assert all(value not in repr(result) for value in SECRET_VALUES)


@pytest.mark.parametrize(
    "url",
    [
        "http://onboarding.example/continue/onboarding-1",
        "https://onboarding.example/continue?state=one-time-secret",
        "https://user:password@onboarding.example/continue",
        "https://onboarding.example/continue#one-time-secret",
    ],
)
def test_continuation_rejects_urls_that_could_expose_sensitive_state(url: str) -> None:
    with pytest.raises(ValueError):
        OnboardingContinuation(
            onboarding_id="onboarding-1",
            url=url,
            expires_at=_expires_at(),
        )


@pytest.mark.asyncio
async def test_unavailable_onboarding_provider_fails_closed() -> None:
    principal = Principal(subject="user-1", issuer="https://issuer.example", kind=PrincipalKind.REMOTE)
    provider = UnavailableOnboardingProvider()

    with pytest.raises(OnboardingUnavailableError):
        await provider.begin(principal)

    with pytest.raises(OnboardingUnavailableError):
        await provider.get_result(principal, "onboarding-1")
