"""Fly Sprites workspace API. No OCI emulation or implicit mutation retries."""

from __future__ import annotations

import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from agent_capacity.workspace_contract import Capabilities, WorkspaceError

MAX_RESPONSE = 250 * 1024 * 1024


class SpriteError(WorkspaceError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SpriteError("provider redirect refused", code)


def segment(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid provider identity segment")
    return quote(value, safe="")


def completed_stream(data: bytes) -> list[dict]:
    events = [json.loads(line) for line in data.splitlines() if line.strip()]
    if not events or any(event.get("type") in ("error", "timeout") for event in events):
        raise SpriteError("provider operation did not complete")
    if events[-1].get("type") != "complete":
        raise SpriteError("provider stream ended without completion evidence")
    return events


class SpritesProvider:
    name = "sprites"
    capabilities = Capabilities(
        True, True, True, True, False, True, ("keep", "sleep", "checkpoint", "delete")
    )
    endpoint = "https://api.sprites.dev/v1"

    def __init__(self, values: dict | None = None):
        self.values = values or {}

    def identity(self, values: dict) -> dict:
        return {
            "organization": values.get("organization", ""),
            "endpoint": self.endpoint,
        }

    def resources(self) -> dict:
        return {"cpu": 8, "memory": "provider-managed", "storage_gb": 100}

    def task_root(self, job_id: str) -> str:
        return "/home/sprite/.elsewhere/tasks/" + segment(job_id)

    def ready(self) -> tuple[bool, str]:
        if not self.values.get("enabled") or not self.values.get("organization"):
            return (
                False,
                "enabled workspace provider and explicit organization are required",
            )
        return True, "configured; credentials and live identity checked on execution"

    def token(self) -> str:
        token = os.environ.get("SPRITES_TOKEN") or os.environ.get("SPRITE_TOKEN")
        if not token:
            raise SpriteError(
                "Sprites credentials are unavailable; set SPRITES_TOKEN securely"
            )
        return token

    def request(
        self, method: str, path: str, body: bytes | dict | None = None
    ) -> bytes:
        headers = {"Authorization": "Bearer " + self.token()}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif body is not None:
            headers["Content-Type"] = "application/octet-stream"
        for attempt in range(3 if method == "GET" else 1):
            try:
                with build_opener(NoRedirect()).open(
                    Request(
                        self.endpoint + path, data=body, headers=headers, method=method
                    ),
                    timeout=45,
                ) as response:
                    data = response.read(MAX_RESPONSE + 1)
                    if len(data) > MAX_RESPONSE:
                        raise SpriteError("provider response exceeds safety limit")
                    return data
            except HTTPError as error:
                if (
                    method == "GET"
                    and error.code in (429, 502, 503, 504)
                    and attempt < 2
                ):
                    time.sleep(attempt + 1)
                    continue
                raise SpriteError(
                    f"Sprites {method} request failed (HTTP {error.code})", error.code
                ) from None
            except (URLError, TimeoutError, OSError):
                if method == "GET" and attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                raise SpriteError(
                    "Sprites request interrupted; mutation outcome may be uncertain"
                ) from None
        raise SpriteError("provider request exhausted retries")

    def get(self, name: str) -> dict | None:
        try:
            value = json.loads(self.request("GET", "/sprites/" + segment(name)))
        except SpriteError as error:
            if error.status == 404:
                return None
            raise
        if (
            value.get("name") != name
            or value.get("organization") != self.values["organization"]
            or not value.get("id")
        ):
            raise SpriteError("Sprite identity differs from the approved destination")
        return value

    def list(self) -> list[dict]:
        items, cursor = [], None
        for _ in range(100):
            query = "?" + urlencode({"continuation_token": cursor}) if cursor else ""
            result = json.loads(self.request("GET", "/sprites" + query))
            if result.get("name") != self.values["organization"]:
                raise SpriteError(
                    "Sprites token organization differs from configuration"
                )
            items.extend(result["sprites"])
            if not result.get("has_more"):
                return items
            new_cursor = result.get("next_continuation_token")
            if not new_cursor or new_cursor == cursor:
                raise SpriteError("invalid Sprite pagination")
            cursor = new_cursor
        raise SpriteError("Sprite inventory exceeded pagination limit")

    def create(self, name: str) -> dict:
        segment(name)
        self.list()  # Prove token organization before creation, including empty accounts.
        response = json.loads(
            self.request(
                "POST", "/sprites", {"name": name, "url_settings": {"auth": "sprite"}}
            )
        )
        if (
            response.get("name") != name
            or response.get("organization") != self.values["organization"]
            or not response.get("id")
        ):
            raise SpriteError("created Sprite identity is uncertain")
        return response

    def delete(self, name: str) -> None:
        try:
            self.request("DELETE", "/sprites/" + segment(name))
        except SpriteError as error:
            if error.status != 404:
                raise

    def read(self, name: str, path: str) -> bytes:
        return self.request(
            "GET",
            "/sprites/"
            + segment(name)
            + "/fs/read?"
            + urlencode({"path": path, "workingDir": "/"}),
        )

    def write(self, name: str, path: str, data: bytes) -> None:
        self.request(
            "PUT",
            "/sprites/"
            + segment(name)
            + "/fs/write?"
            + urlencode(
                {"path": path, "workingDir": "/", "mkdir": "true", "mode": "0600"}
            ),
            data,
        )

    def policy(self, name: str) -> dict:
        return json.loads(
            self.request("GET", "/sprites/" + segment(name) + "/policy/network")
        )

    def privileges(self, name: str) -> dict:
        value = json.loads(self.request("GET", "/sprites/" + segment(name) + "/policy/privileges"))
        if not isinstance(value, dict):
            raise SpriteError("unrecognized privilege policy")
        return value

    def sessions(self, name: str) -> list[dict]:
        value = json.loads(self.request("GET", "/sprites/" + segment(name) + "/exec"))
        if isinstance(value, dict):
            value = value.get("sessions")
        if not isinstance(value, list) or any(not isinstance(x, dict) or not x.get("id") for x in value):
            raise SpriteError("unrecognized session inventory")
        return value

    def observe_session(self, name: str, session: str) -> dict:
        """Reconnect for bounded diagnostics; only the result bundle proves task outcome."""
        from websocket import WebSocketTimeoutException, create_connection

        evidence = {"stdout": "", "stderr": "", "session_exit_code": None}
        connection = None
        try:
            connection = create_connection(
                self.endpoint.replace("https://", "wss://") + "/sprites/" + segment(name) + "/exec/" + segment(session),
                header={"Authorization": "Bearer " + self.token()}, timeout=2, redirect_limit=0,
            )
            for _ in range(64):
                message = connection.recv()
                if not message:
                    break
                if isinstance(message, bytes):
                    if message[0] in (1, 2):
                        key = "stdout" if message[0] == 1 else "stderr"
                        evidence[key] = (evidence[key] + message[1:].decode(errors="replace"))[-12000:]
                    elif message[0] == 3 and len(message) == 2:
                        evidence["session_exit_code"] = message[1]
                        break
                else:
                    event = json.loads(message)
                    if event.get("type") == "exit":
                        evidence["session_exit_code"] = event.get("exit_code")
                        break
        except WebSocketTimeoutException:
            pass
        except Exception:
            raise SpriteError("session reconnection failed; original task remains unresolved") from None
        finally:
            if connection:
                connection.close()
        return evidence

    def set_policy(self, name: str, policy: dict) -> None:
        self.request("POST", "/sprites/" + segment(name) + "/policy/network", policy)
        if self.policy(name) != policy:
            raise SpriteError("provider policy readback differs from approval")

    def checkpoint(self, name: str, comment: str) -> str:
        before = self.checkpoints(name)
        completed_stream(
            self.request(
                "POST",
                "/sprites/" + segment(name) + "/checkpoint",
                {"comment": comment},
            )
        )
        new = [
            x
            for x in self.checkpoints(name)
            if x["id"] not in {y["id"] for y in before} and x.get("comment") == comment
        ]
        if len(new) != 1 or new[0].get("health"):
            raise SpriteError("checkpoint identity is ambiguous")
        return new[0]["id"]

    def checkpoints(self, name: str) -> list[dict]:
        value = json.loads(
            self.request("GET", "/sprites/" + segment(name) + "/checkpoints")
        )
        if not isinstance(value, list):
            raise SpriteError("invalid checkpoint inventory")
        return value

    def restore(self, name: str, checkpoint: str) -> None:
        completed_stream(
            self.request(
                "POST",
                "/sprites/"
                + segment(name)
                + "/checkpoints/"
                + segment(checkpoint)
                + "/restore",
            )
        )

    def fork(self, *args, **kwargs):
        raise SpriteError(
            "native checkpoint fork is not supported by the verified Sprites API"
        )

    def verify_connectors(self, expected: list[dict]) -> None:
        # Organization inventory is approved explicitly; no token is projected.
        inventory = json.loads(self.request("GET", "/oauth/connections"))
        if isinstance(inventory, dict) and set(inventory) == {"connections"}:
            inventory = inventory["connections"]
        if not isinstance(inventory, list):
            raise SpriteError("connector inventory schema requires live verification")
        actual = []
        for item in inventory:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("access_policy"), dict)
                or not item.get("id")
                or not item.get("provider")
            ):
                raise SpriteError("connector policy cannot be verified")
            actual.append(
                {key: item[key] for key in ("id", "provider", "access_policy")}
            )
        if sorted(actual, key=lambda x: x["id"]) != sorted(
            expected, key=lambda x: x["id"]
        ):
            raise SpriteError(
                "connector identities or access policies differ from approval"
            )

    def activity_commands(self, job_id: str, runtime: int) -> dict:
        common = [
            "curl",
            "-fsS",
            "--max-time",
            "10",
            "--unix-socket",
            "/.sprite/api.sock",
        ]
        return {
            "start": [
                *common,
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "http://sprite/v1/tasks",
                "-d",
                json.dumps({"name": "elsewhere-" + job_id, "expire": runtime + 30}),
            ],
            "stop": [
                *common,
                "-X",
                "DELETE",
                "http://sprite/v1/tasks/elsewhere-" + job_id,
            ],
        }

    def start(self, name: str, argv: list[str], runtime: int) -> str:
        from websocket import create_connection

        query = urlencode(
            [
                *(("cmd", item) for item in argv),
                ("tty", "false"),
                ("max_run_after_disconnect", f"{runtime}s"),
            ]
        )
        address = (
            self.endpoint.replace("https://", "wss://")
            + "/sprites/"
            + segment(name)
            + "/exec?"
            + query
        )
        try:
            connection = create_connection(
                address,
                header={"Authorization": "Bearer " + self.token()},
                timeout=30,
                redirect_limit=0,
            )
            try:
                session = None
                ready_output = b""
                for _ in range(64):
                    message = connection.recv()
                    if isinstance(message, str):
                        event = json.loads(message)
                        if event.get("type") == "session_info" and event.get(
                            "session_id"
                        ):
                            session = segment(str(event["session_id"]))
                    elif isinstance(message, bytes) and message[:1] == b"\x01":
                        ready_output = (ready_output + message[1:])[-4096:]
                    if session and b"ELSEWHERE_WORKSPACE_READY\n" in ready_output:
                        return session
                    if not message:
                        break
            finally:
                connection.close()
        except Exception:
            raise SpriteError(
                "session submission interrupted; do not retry without reconciliation"
            ) from None
        raise SpriteError(
            "no stable session identity received; submission is uncertain"
        )

    def cancel(self, name: str, session: str) -> None:
        completed_stream(
            self.request(
                "POST",
                "/sprites/"
                + segment(name)
                + "/exec/"
                + segment(session)
                + "/kill?signal=SIGTERM&timeout=10s",
            )
        )
