from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

StrictString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class TrustTier(StrEnum):
    BLOCKED = "BLOCKED"
    RESTRICTED = "RESTRICTED"
    STANDARD = "STANDARD"
    ELEVATED = "ELEVATED"
    HIGHEST = "HIGHEST"


class TransferAction(ApiModel):
    type: Literal["transfer"]
    amount: float = Field(ge=0)
    currency: StrictString
    to_account_id: StrictString = Field(alias="toAccountId")
    memo: str | None = Field(default=None, max_length=500)


class AddPayeeAction(ApiModel):
    type: Literal["addPayee"]
    account_id: StrictString = Field(alias="accountId")
    routing_number: StrictString = Field(alias="routingNumber")


class ElevatedLoginAction(ApiModel):
    type: Literal["elevatedLogin"]


class PolicyChangeAction(ApiModel):
    type: Literal["policyChange"]
    policy_id: StrictString = Field(alias="policyId")
    new_value: Any = Field(alias="newValue")


SensitiveAction = Annotated[
    Union[TransferAction, AddPayeeAction, ElevatedLoginAction, PolicyChangeAction],
    Field(discriminator="type"),
]


class InstrumentationReport(ApiModel):
    platform: Literal["android", "ios"]
    debugger_attached: bool = Field(alias="debuggerAttached")
    emulator: bool
    rooted_or_jailbroken: bool = Field(alias="rootedOrJailbroken")
    hook_framework_detected: bool = Field(alias="hookFrameworkDetected")
    debuggable_package: bool = Field(alias="debuggablePackage")
    untrusted_installer: bool = Field(alias="untrustedInstaller")
    suspicious_env_vars: list[str] = Field(alias="suspiciousEnvVars", max_length=100)
    device_model: str | None = Field(default=None, alias="deviceModel", max_length=255)
    sdk_int: int | None = Field(default=None, alias="sdkInt", ge=1, le=10_000)
    signals: dict[str, Any]
    collected_at_ms: int = Field(alias="collectedAtMs", ge=0)


class HardwareKeyAttestation(ApiModel):
    alias: StrictString
    certificate_chain_base64: list[str] = Field(
        alias="certificateChainBase64", min_length=1, max_length=8
    )


class AndroidAttestationSuccess(ApiModel):
    ok: Literal[True]
    platform: Literal["android"]
    token: Annotated[str, StringConstraints(min_length=20, max_length=100_000)]
    provider: Literal["playIntegrity"]
    hardware_key_attestation: HardwareKeyAttestation | None = Field(
        default=None, alias="hardwareKeyAttestation"
    )


class IosAttestationSuccess(ApiModel):
    ok: Literal[True]
    platform: Literal["ios"]
    assertion: Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
    key_id: StrictString = Field(alias="keyId")
    attestation: str | None = Field(default=None, max_length=200_000)
    provider: Literal["appAttest"]


class AttestationFailure(ApiModel):
    ok: Literal[False]
    code: Literal[
        "DEVICE_NOT_SUPPORTED",
        "INTEGRITY_SERVICE_UNAVAILABLE",
        "NETWORK",
        "KEY_GENERATION_FAILED",
        "ASSERTION_FAILED",
        "NONCE_REJECTED",
        "UNKNOWN",
    ]
    retryable: bool
    details: str | None = Field(default=None, max_length=2_000)


BaseAttestationResult = Union[
    AndroidAttestationSuccess,
    IosAttestationSuccess,
    AttestationFailure,
]


class NonceRequest(ApiModel):
    client_ts: int = Field(alias="clientTs")


class NonceResponse(ApiModel):
    nonce: str
    server_issued_at: int = Field(alias="serverIssuedAt")


class VerifyRequest(ApiModel):
    action: SensitiveAction
    canonical_json: Annotated[str, StringConstraints(max_length=4096)] = Field(alias="canonicalJson")
    nonce: Annotated[str, StringConstraints(min_length=20, max_length=200)]
    attestation: BaseAttestationResult
    instrumentation_report: InstrumentationReport = Field(alias="instrumentationReport")


class VerifySuccess(ApiModel):
    ok: Literal[True] = True
    access_token: str = Field(alias="accessToken")
    tier: TrustTier
    expires_at: int = Field(alias="expiresAt")
    classifier_trace: dict[str, Any] | None = Field(default=None, alias="classifierTrace")


class DevTokenRequest(ApiModel):
    user_id: StrictString = Field(alias="userId")


class DevTokenResponse(ApiModel):
    access_token: str = Field(alias="accessToken")
    token_type: Literal["bearer"] = Field(default="bearer", alias="tokenType")
