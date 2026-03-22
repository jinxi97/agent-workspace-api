from pydantic import BaseModel, Field


class GoogleAuthRequest(BaseModel):
    id_token: str = Field(..., min_length=1)


class CreateApiKeyRequest(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=64, description="A friendly name for the API key")


class SnapshotTriggerRequest(BaseModel):
    claim_name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    pod_name: str = Field(..., min_length=1)


class SnapshotRestoreRequest(BaseModel):
    claim_name: str = Field(
        ...,
        min_length=1,
        description="Claim name whose latest snapshot should be restored",
    )


class ExecuteRequest(BaseModel):
    claim_name: str
    namespace: str
    pod_name: str
    command: str


class CreateChatRequest(BaseModel):
    title: str | None = Field(default=None, description="Optional chat title")


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, description="Message content to send")


class AnswerRequest(BaseModel):
    answers: dict[str, str] = Field(..., description="Question text → selected option label")
