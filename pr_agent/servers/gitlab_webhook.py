import copy
import json

import uvicorn
from fastapi import APIRouter, FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.secret_providers import get_secret_provider

setup_logger(fmt=LoggingFormat.JSON)
router = APIRouter()

secret_provider = get_secret_provider() if get_settings().get("CONFIG.SECRET_PROVIDER") else None


def handle_request(background_tasks: BackgroundTasks, url: str, body: str, log_context: dict):
    log_context["action"] = body
    log_context["event"] = "pull_request" if body == "/review" else "comment"
    log_context["api_url"] = url
    with get_logger().contextualize(**log_context):
        background_tasks.add_task(PRAgent().handle_request, url, body)


@router.post("/webhook")
async def gitlab_webhook(background_tasks: BackgroundTasks, request: Request):
    log_context = {"server_type": "gitlab_app"}
    if request.headers.get("X-Gitlab-Token") and secret_provider:
        request_token = request.headers.get("X-Gitlab-Token")
        secret = secret_provider.get_secret(request_token)
        try:
            secret_dict = json.loads(secret)
            gitlab_token = secret_dict["gitlab_token"]
            log_context["sender"] = secret_dict["id"]
            context["settings"] = copy.deepcopy(global_settings)
            context["settings"].gitlab.personal_access_token = gitlab_token
        except Exception as e:
            get_logger().error(f"Failed to validate secret {request_token}: {e}")
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content=jsonable_encoder({"message": "unauthorized"}))
    elif get_settings().get("GITLAB.SHARED_SECRET"):
        secret = get_settings().get("GITLAB.SHARED_SECRET")
        if not request.headers.get("X-Gitlab-Token") == secret:
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content=jsonable_encoder({"message": "unauthorized"}))
    else:
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content=jsonable_encoder({"message": "unauthorized"}))
    gitlab_token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", None)
    if not gitlab_token:
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content=jsonable_encoder({"message": "unauthorized"}))
    data = await request.json()
    get_logger().info(json.dumps(data))
    if data.get('object_kind') == 'merge_request' and data['object_attributes'].get('action') in ['open', 'reopen']:
        get_logger().info(f"A merge request has been opened: {data['object_attributes'].get('title')}")
        url = data['object_attributes'].get('url')
        handle_request(background_tasks, url, "/review")
    elif data.get('object_kind') == 'note' and data['event_type'] == 'note':
        if 'merge_request' in data:
            mr = data['merge_request']
            url = mr.get('url')
            body = data.get('object_attributes', {}).get('note')
            handle_request(background_tasks, url, body)
    return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "success"}))


@router.get("/")
async def root():
    return {"status": "ok"}

def start():
    gitlab_url = get_settings().get("GITLAB.URL", None)
    if not gitlab_url:
        raise ValueError("GITLAB.URL is not set")
    get_settings().config.git_provider = "gitlab"
    middleware = [Middleware(RawContextMiddleware)]
    app = FastAPI(middleware=middleware)
    app.include_router(router)

    uvicorn.run(app, host="0.0.0.0", port=3000)


if __name__ == '__main__':
    start()
