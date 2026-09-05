from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.deps import get_current_user, get_current_user_for_streaming
from app.db.session import get_session
from app.main import app


LONG_LIVED_ROUTES = (
    ("GET", "/api/auth/access-events"),
    ("GET", "/api/channels/catalog-events"),
    ("GET", "/api/channels/{channel_id}/events"),
    ("GET", "/api/stream/{episode_id}"),
)


def _route(method: str, path: str) -> APIRoute:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
    raise AssertionError(f"Route not found: {method} {path}")


def _walk_dependencies(dependant):
    for dependency in dependant.dependencies:
        yield dependency
        yield from _walk_dependencies(dependency)


def test_long_lived_responses_use_function_scoped_database_sessions():
    """SSE/video requests must not reserve QueuePool slots for their whole lifetime."""
    for method, path in LONG_LIVED_ROUTES:
        route = _route(method, path)
        dependencies = list(_walk_dependencies(route.dependant))
        session_dependencies = [dep for dep in dependencies if dep.call is get_session]
        dependency_calls = {dep.call for dep in dependencies}

        assert session_dependencies, f"{method} {path} has no database dependency"
        assert all(dep.scope == "function" for dep in session_dependencies), (
            f"{method} {path} contains a request-scoped database dependency"
        )
        assert get_current_user_for_streaming in dependency_calls
        assert get_current_user not in dependency_calls


def test_function_scoped_yield_dependency_is_shared_and_closes_before_stream_body():
    """Lock in the FastAPI lifecycle/cache guarantees the pool fix relies on."""
    mini_app = FastAPI()
    state = {"closed": False, "opens": 0}

    def resource():
        state["opens"] += 1
        try:
            yield state
        finally:
            state["closed"] = True

    def nested_resource(resource_state=Depends(resource, scope="function")):
        return resource_state

    @mini_app.get("/")
    def stream(
        nested_state=Depends(nested_resource),
        resource_state=Depends(resource, scope="function"),
    ):
        # The authentication dependency and endpoint DB dependency use the same
        # callable/scope, so FastAPI should cache one resource for the preflight.
        assert nested_state is resource_state

        async def body():
            assert resource_state["closed"] is True
            yield b"ok"

        return StreamingResponse(body())

    with TestClient(mini_app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.content == b"ok"
    assert state["opens"] == 1
    assert state["closed"] is True
