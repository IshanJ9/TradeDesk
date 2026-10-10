"""Routes that act on the signed-in trader's desk: sign-in is required, and the handler gets that trader's Workspace.

The workspace comes from the session (via `require_user`), never from anything the client sends, so a route cannot
be pointed at another user's desk by editing a URL, a header or a body.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.deps import CurrentUser, require_user
from app.workspace import Workspace, WorkspaceUnavailable


async def bind_workspace(request: Request, actor: CurrentUser) -> Workspace:
    try:
        ws = await request.app.state.workspaces.get(actor)
    except WorkspaceUnavailable:
        raise HTTPException(503, "Your desk is unavailable right now (the broker could not be reached). Try again shortly.") from None
    request.state.ws = ws
    return ws


def desk_router(**kwargs) -> APIRouter:
    """An APIRouter whose every route needs a signed-in user and runs against that user's workspace."""
    return APIRouter(dependencies=[Depends(require_user), Depends(bind_workspace)], **kwargs)


def ws_of(request: Request) -> Workspace:
    """The caller's workspace (set by `bind_workspace` before the handler runs)."""
    return request.state.ws
