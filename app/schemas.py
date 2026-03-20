from pydantic import BaseModel, Field


class GoogleAuthRequest(BaseModel):
    id_token: str = Field(..., min_length=1)


class SnapshotTriggerRequest(BaseModel):
    workspace_id: str = Field(
        ...,
        min_length=1,
        description="Workspace ID returned by POST /workspaces",
    )


class SnapshotRestoreRequest(BaseModel):
    workspace_id: str = Field(
        ...,
        min_length=1,
        description="Workspace ID whose latest snapshot should be restored",
    )


class ExecuteRequest(BaseModel):
    workspace_id: str
    command: str


class CreateChatRequest(BaseModel):
    title: str | None = Field(default=None, description="Optional chat title")


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, description="Message content to send")


class AnswerRequest(BaseModel):
    answers: dict[str, str] = Field(..., description="Question text → selected option label")
