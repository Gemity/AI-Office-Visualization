from typing import Annotated

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field

from office_api import idempotency
from office_api.auth import OwnerMembership, Session
from office_api.errors import ApiError
from office_api.idempotency import WriteResponse
from office_api.runtime import NoOrchestrator, active_orchestrator_id, add_message, owner_chat

router = APIRouter(prefix="/projects/{project_id}/orchestrator", tags=["orchestrator"])


class OwnerMessage(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class OwnerMessageAccepted(BaseModel):
    message_id: str
    conversation_id: str
    run_id: str = Field(description="Orchestrator turn handling this message.")


@router.post("/messages", status_code=202, response_model=WriteResponse[OwnerMessageAccepted])
async def send_owner_message(
    project_id: str,
    body: OwnerMessage,
    request: Request,
    member: OwnerMembership,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    """Owner → Orchestrator (BR-02). The reply arrives asynchronously as a
    ``message.sent`` event in the owner_chat conversation."""
    runtime = request.app.state.runtime
    orchestrator_id = await active_orchestrator_id(session, project_id)
    if orchestrator_id is None:
        raise ApiError(409, "no_orchestrator", "Assign an orchestrator agent first")

    async def handler() -> OwnerMessageAccepted:
        conv_id = await owner_chat(session, project_id, orchestrator_id)
        message = await add_message(
            session,
            runtime.store,
            project_id=project_id,
            conversation_id=conv_id,
            sender_id=member.principal_id,
            text=body.content,
            recipient_ids=[orchestrator_id],
        )
        try:
            run = await runtime.create_turn(
                session, project_id, {"kind": "owner_message", "message_id": message.id}
            )
        except NoOrchestrator:
            raise ApiError(409, "no_orchestrator", "Assign an orchestrator agent first") from None
        return OwnerMessageAccepted(message_id=message.id, conversation_id=conv_id, run_id=run.id)

    response, data = await idempotency.write(
        request,
        session,
        member.principal_id,
        idempotency_key,
        body,
        handler,
        status_code=202,
        op_status="accepted",
    )
    if data is not None:
        runtime.start_turn(data.run_id)
    return response
