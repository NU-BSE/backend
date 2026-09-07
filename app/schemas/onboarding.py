from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import CamelModel

OnboardingStatus = Literal[
    "not_started",
    "auth_completed",
    "intent_completed",
    "connections_completed",
    "ai_mode_completed",
    "first_task_started",
    "first_task_completed",
    "feedback_completed",
    "subscription_completed",
    "completed",
]

OnboardingIntent = Literal[
    "android_settings",
    "messages",
    "email",
    "calendar",
    "drive",
]

AiMode = Literal["local", "cloud"]

FeedbackResult = Literal["yes", "partly", "no"]

FeedbackAlternative = Literal[
    "google",
    "chatgpt_or_gemini",
    "android_settings",
    "manual_app_action",
    "ask_someone",
    "would_not_do",
    "other",
]

FirstTaskOutcome = Literal["completed", "failed", "abandoned"]

LocalAiStatus = Literal[
    "local_available",
    "local_not_supported",
    "local_download_required",
    "local_ready",
]


class FirstTaskInfo(CamelModel):
    conversation_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    suggested_task_id: str | None = None
    original_prompt: str | None = None
    tool_used: bool | None = None
    tool_names: list[str] = Field(default_factory=list)
    outcome: FirstTaskOutcome | None = None


class FeedbackInfo(CamelModel):
    result: FeedbackResult | None = None
    expectation_text: str | None = None
    alternative: FeedbackAlternative | None = None
    alternative_text: str | None = None


class AiModeLocalOptions(CamelModel):
    status: LocalAiStatus
    profiles: list[str] = Field(default_factory=list)
    download_allowed: bool


class AiModeCloudOptions(CamelModel):
    status: Literal["cloud_ready"] = "cloud_ready"
    requires_subscription: bool


class AiModeOptions(CamelModel):
    local: AiModeLocalOptions
    cloud: AiModeCloudOptions


class OnboardingStateResponse(CamelModel):
    version: int
    status: OnboardingStatus
    intents: list[str] = Field(default_factory=list)
    custom_intent: str | None = None
    ai_mode: AiMode | None = None
    first_task: FirstTaskInfo = Field(default_factory=FirstTaskInfo)
    feedback: FeedbackInfo = Field(default_factory=FeedbackInfo)
    ai_mode_options: AiModeOptions
    # Whether this account must buy anything to use the product past the first
    # task. The app shows the paywall only after first-task feedback, not
    # before — the backend says *if* a paywall is due, the client decides when.
    subscription_required: bool
    # True once feedback is in and this account still has to pay: the moment
    # the paywall is actually shown.
    paywall_due: bool
    created_at: datetime
    updated_at: datetime


class UpdateIntentsRequest(CamelModel):
    # Plain strings: the route validates against VALID_INTENTS and answers with
    # a machine-readable INVALID_INTENT, rather than pydantic's generic
    # VALIDATION_ERROR.
    intents: list[str] = Field(default_factory=list)
    custom_intent: str | None = Field(default=None, max_length=2000)


class UpdateAiModeRequest(CamelModel):
    mode: AiMode


class FirstTaskStartRequest(CamelModel):
    suggested_task_id: str | None = Field(default=None, max_length=200)
    original_prompt: str | None = Field(default=None, max_length=5000)
    conversation_id: str | None = Field(default=None, max_length=200)


class FirstTaskStartResponse(CamelModel):
    conversation_id: str
    state: OnboardingStateResponse


class FirstTaskCompleteRequest(CamelModel):
    conversation_id: str | None = Field(default=None, max_length=200)
    outcome: FirstTaskOutcome = "completed"
    tool_used: bool = False
    tool_names: list[str] = Field(default_factory=list)


class OnboardingFeedbackRequest(CamelModel):
    result: FeedbackResult
    expectation_text: str | None = Field(default=None, max_length=5000)
    alternative: FeedbackAlternative | None = None
    alternative_text: str | None = Field(default=None, max_length=5000)