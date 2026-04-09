from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import json
from typing import Any

from .actions import ActionController
from .collector import BandwidthCollector
from .models import ActionResult


@dataclass(slots=True)
class AppState:
    collector: BandwidthCollector
    actions: ActionController
    history: deque[ActionResult] = field(default_factory=lambda: deque(maxlen=12))

    def status_payload(self) -> dict[str, Any]:
        snapshot = self.collector.snapshot()
        return {
            "snapshot": snapshot.to_dict(),
            "recipes": [recipe.to_dict() for recipe in self.actions.list_recipes()],
            "history": [item.to_dict() for item in self.history],
        }

    def record(self, result: ActionResult) -> ActionResult:
        self.history.appendleft(result)
        return result


def serve(host: str, port: int, collector: BandwidthCollector, actions: ActionController) -> None:
    state = AppState(collector=collector, actions=actions)
    handler = _handler_factory(state)
    with ThreadingHTTPServer((host, port), handler) as server:
        print(f"Low Bandwidth listening on http://{host}:{port}")
        server.serve_forever()


def _handler_factory(state: AppState) -> type[BaseHTTPRequestHandler]:
    class RequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                self._serve_asset("index.html", "text/html; charset=utf-8")
                return
            if self.path == "/app.js":
                self._serve_asset("app.js", "application/javascript; charset=utf-8")
                return
            if self.path == "/styles.css":
                self._serve_asset("styles.css", "text/css; charset=utf-8")
                return
            if self.path == "/api/status":
                self._write_json(HTTPStatus.OK, state.status_payload())
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            body = self._read_json_body()
            if body is None:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
                return

            if self.path == "/api/process-action":
                pid = body.get("pid")
                action = body.get("action")
                if not isinstance(pid, int) or not isinstance(action, str):
                    self._write_json(HTTPStatus.BAD_REQUEST, {"error": "pid and action are required"})
                    return
                result = state.record(state.actions.execute_process_action(pid, action))
                self._write_json(HTTPStatus.OK, result.to_dict())
                return

            if self.path == "/api/recipe-action":
                recipe_id = body.get("recipe_id")
                if not isinstance(recipe_id, str):
                    self._write_json(HTTPStatus.BAD_REQUEST, {"error": "recipe_id is required"})
                    return
                result = state.record(state.actions.execute_recipe(recipe_id))
                self._write_json(HTTPStatus.OK, result.to_dict())
                return

            self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def log_message(self, format_: str, *args: object) -> None:
            del format_, args

        def _serve_asset(self, name: str, content_type: str) -> None:
            payload = resources.files("src.static").joinpath(name).read_text(encoding="utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))

        def _read_json_body(self) -> dict[str, Any] | None:
            raw_length = self.headers.get("Content-Length", "0")
            if not raw_length.isdigit():
                return None
            length = int(raw_length)
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return None

        def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return RequestHandler
