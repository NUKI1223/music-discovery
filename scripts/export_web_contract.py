"""Export the agreed API contract without importing application state."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from discovery.web import schemas as s

OPERATIONS = [
    ("get", "/api/releases/{release_id}/artwork", None, s.Artwork),
    ("post", "/api/auth/register", s.Registration, s.Session),
    ("post", "/api/auth/login", s.Credentials, s.Session),
    ("get", "/api/auth/session", None, s.Session),
    ("post", "/api/auth/logout", None, s.Ok),
    ("post", "/api/auth/password", s.PasswordChange, s.Ok),
    ("get", "/api/profile", None, s.Profile),
    ("put", "/api/profile", s.Profile, s.Profile),
    ("post", "/api/profile/starter", None, s.Profile),
    ("get", "/api/catalog/genres", None, list[s.Lookup]),
    ("get", "/api/catalog/artists", None, list[s.Lookup]),
    ("get", "/api/chats", None, list[s.ChatSummary]),
    ("post", "/api/chats", s.ChatCreate, s.ChatDetail),
    ("get", "/api/chats/{chat_id}", None, s.ChatDetail),
    ("delete", "/api/chats/{chat_id}", None, s.Ok),
    ("patch", "/api/chats/{chat_id}/filters", s.Filters, s.ChatDetail),
    ("put", "/api/chats/{chat_id}/memory", s.MemoryUpdate, s.ChatMemory),
    ("post", "/api/chats/{chat_id}/messages", s.MessageRequest, s.Message),
    ("get", "/api/collection", None, list[s.ReleaseCard]),
    ("delete", "/api/collection", None, s.Ok),
    ("delete", "/api/collection/{release_id}", None, s.Ok),
    ("post", "/api/feedback", s.FeedbackRequest, s.Ok),
]


def contract():
    schemas = {}

    def schema(model):
        value = TypeAdapter(model).json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(value.pop("$defs", {}))
        if isinstance(model, type) and issubclass(model, s.Model):
            schemas[model.__name__] = value
            return {"$ref": f"#/components/schemas/{model.__name__}"}
        return value

    paths = {}
    for method, path, request, response in OPERATIONS:
        operation = {
            "operationId": method + path.replace("/", "_").replace("{", "").replace("}", ""),
            "responses": {"200": {"description": "Success", "content": {
                "application/json": {"schema": schema(response)}}}},
        }
        operation["security"] = ([] if path in {"/api/auth/register", "/api/auth/login"}
                                 else [{"SessionCookie": []}])
        for status in (400, 401, 403, 404, 409, 413, 422, 429):
            operation["responses"][str(status)] = {"description": "Request rejected", "content": {
                "application/json": {"schema": schema(s.APIError)}}}
        params = []
        if path == "/api/profile/starter":
            params.append({"name": "preview", "in": "query", "required": False,
                           "description": "Return starter taste without persisting; the browser saves it explicitly.",
                           "schema": {"type": "boolean", "default": False}})
        if method == "delete" and path.startswith("/api/collection"):
            params.append({"name": "forget", "in": "query", "required": False,
                           "description": "Also revoke album feedback and allow future recommendations; otherwise hide and exclude.",
                           "schema": {"type": "boolean", "default": False}})
        if "{release_id}" in path:
            params.append({"name": "release_id", "in": "path", "required": True, "schema": {"type": "string"}})
        if "{chat_id}" in path:
            params.append({"name": "chat_id", "in": "path", "required": True,
                           "schema": {"type": "string"}})
        if path == "/api/chats/{chat_id}" and method == "get":
            params.extend([
                {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 2, "maximum": 50, "default": 20}},
                {"name": "before", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                {"name": "after", "in": "query", "schema": {"type": "integer", "minimum": 1}},
            ])
        if path.startswith("/api/catalog/"):
            params.append({"name": "q", "in": "query", "required": False,
                           "schema": {"type": "string", "default": ""}})
        if path == "/api/profile" and method == "get":
            params.append({"name": "genre_scope_id", "in": "query", "required": False,
                           "schema": {"type": "string"}})
        if path == "/api/catalog/genres":
            params.append({"name": "genre_scope_id", "in": "query", "required": False,
                           "schema": {"type": "string"}})
        if path == "/api/catalog/artists":
            params.append({"name": "ids", "in": "query", "required": False,
                           "description": "Comma-separated IDs, at most 50.",
                           "schema": {"type": "string"}})
        if method not in {"get"}:
            params.append({"name": "Origin", "in": "header", "required": True,
                           "schema": {"type": "string"}})
            if path not in {"/api/auth/register", "/api/auth/login"}:
                params.append({"name": "X-CSRF-Token", "in": "header", "required": True,
                               "schema": {"type": "string"}})
        if params:
            operation["parameters"] = params
        if request:
            operation["requestBody"] = {"required": True, "content": {
                "application/json": {"schema": schema(request)}}}
        paths.setdefault(path, {})[method] = operation
    return {"openapi": "3.1.0", "info": {"title": "Music Discovery", "version": "1.0.0"},
            "paths": paths, "components": {"schemas": schemas, "securitySchemes": {
                "SessionCookie": {"type": "apiKey", "in": "cookie", "name": "discovery_session"}}}}


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "docs/web-openapi.json"
    target.write_text(json.dumps(contract(), indent=2, ensure_ascii=False) + "\n")
