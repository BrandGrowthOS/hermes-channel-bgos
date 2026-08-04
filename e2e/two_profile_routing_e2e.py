"""End-to-end proof: multi-profile Hermes on ONE machine routes correctly.

Reproduces and proves the fix for the 2026-08-04 Achilles/Shadow bug
(message sent to Shadow was answered by Achilles) against a REAL local BGOS
backend (docker postgres + `npm run start:dev`), never production.

Three phases:

  Phase 1 - two daemons, one machine (the supported per-profile topology):
    two scratch HERMES_HOMEs, each with its own pairing written by the same
    secrets contract `hermes-pair-bgos` uses. Daemon A binds Achilles
    (route `default`), daemon B binds Shadow (route `shadow`). A scripted
    deterministic "brain" replies with its own identity through the
    adapter's real send() path. Asserts: message to A answered by A,
    message to B answered by B, interleaved, exactly ONE reply each.

  Phase 2 - daemon restart must not steal identity: daemon B is killed and
    restarted; both agents are messaged again and must still answer as
    themselves, once each.

  Phase 3 - KC's exact shape: ONE daemon, ONE pairing, TWO routes
    (default:Achilles + shadow:Shadow). The daemon runs with a simulated
    Hermes gateway layer (fake `hermes_cli.profiles` exposing a `shadow`
    profile plus a stand-in gateway MessageEvent), and replies with the
    `source.profile` the adapter stamped. Asserts: the Shadow assistant's
    turns carry profile `shadow`, Achilles' carry the active profile, and
    each message gets exactly one reply. Without the fix the stamp is
    always absent and Shadow's turn runs as the active (Achilles) profile;
    this phase fails on the pre-fix adapter.

Usage:
    python e2e/two_profile_routing_e2e.py \
        --base-url http://127.0.0.1:8099 --api-key <test user X-API-Key>

Requires: the local backend running against a THROWAWAY database and a test
user whose api_key is set. Never point this at api.brandgrowthos.ai.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON = REPO_ROOT / "e2e" / "daemon_stub.py"

def _req(method: str, url: str, api_key: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc


class Backend:
    def __init__(self, base_url: str, api_key: str, user_id: str):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.user_id = user_id

    def create_pairing(self, device_label: str, catalog: list[dict]) -> dict:
        code = _req(
            "POST", f"{self.base}/api/v1/integrations/pair-codes", self.key,
            {"direction": "bgos_initiated"},
        )["code"]
        exchanged = _req(
            "POST", f"{self.base}/api/v1/integrations/pair-exchange", None,
            {
                "code": code,
                "deviceLabel": device_label,
                "integration": "hermes",
                "agentCatalog": catalog,
            },
        )
        return exchanged

    def bind_agents(self, pairing_id: int, routes: list[tuple[str, str]]) -> list[int]:
        resp = _req(
            "POST",
            f"{self.base}/api/v1/integrations/pairings/{pairing_id}/assistants",
            self.key,
            {"agents": [{"agent_route": r, "name": n} for r, n in routes]},
        )
        ids = resp.get("assistant_ids") or resp.get("created") or resp
        if isinstance(ids, dict):
            ids = ids.get("assistant_ids", [])
        return [int(i) for i in ids]

    def create_chat(self, assistant_id: int, name: str) -> int:
        resp = _req(
            "POST", f"{self.base}/api/v1/chats", self.key,
            {"assistantId": assistant_id, "title": name},
        )
        return int(resp["id"])

    def send_user_message(self, chat_id: int, assistant_id: int, text: str) -> None:
        _req(
            "POST", f"{self.base}/api/v1/send-message", self.key,
            {
                "chatId": chat_id,
                "assistantId": assistant_id,
                "text": text,
                "sender": "user",
            },
        )

    def messages(self, chat_id: int) -> list[dict]:
        resp = _req(
            "GET",
            f"{self.base}/api/v1/chats/{chat_id}/messages?userId={self.user_id}",
            self.key,
        )
        if isinstance(resp, dict):
            resp = resp.get("messages", resp.get("data", []))
        # The endpoint wraps each row as {"message": {...}, "files": [...]}.
        out = []
        for row in resp:
            out.append(row.get("message", row) if isinstance(row, dict) else row)
        out.sort(key=lambda m: m.get("id", 0))
        return out

    def wait_for_assistant_replies(
        self, chat_id: int, after_len: int, count: int = 1, timeout: float = 20.0,
    ) -> list[dict]:
        """Wait for `count` assistant messages beyond `after_len`, then a
        settle beat to catch late duplicates."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msgs = self.messages(chat_id)
            fresh = [
                m for m in msgs[after_len:]
                if (m.get("sender") or m.get("senderType")) == "assistant"
            ]
            if len(fresh) >= count:
                time.sleep(1.5)  # settle: catch duplicate replies arriving late
                msgs = self.messages(chat_id)
                return [
                    m for m in msgs[after_len:]
                    if (m.get("sender") or m.get("senderType")) == "assistant"
                ]
            time.sleep(0.5)
        raise AssertionError(
            f"timed out waiting for assistant reply in chat {chat_id}"
        )


def write_secrets(home: Path, token: str, pairing_id: int, base_url: str) -> None:
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    (home / "secrets" / "bgos.json").write_text(json.dumps({
        "pairing_token": token,
        "pairing_id": pairing_id,
        "base_url": base_url,
    }))


def start_daemon(
    home: Path, base_url: str, identity: str, *, gateway_sim: bool = False,
    log_path: Path,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_CHANNEL_BGOS_FORCE_MOCK_HERMES"] = "1"
    env["BGOS_E2E_IDENTITY"] = identity
    env["BGOS_E2E_GATEWAY_SIM"] = "1" if gateway_sim else ""
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + str(REPO_ROOT)
    env.pop("BGOS_API_KEY", None)
    env.pop("BGOS_BACKEND_URL", None)
    log = open(log_path, "ab")
    return subprocess.Popen(
        [sys.executable, str(DAEMON)], env=env, stdout=log, stderr=log,
    )


def stop_daemon(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def expect(msgs: list[dict], token: str, chat_id: int, label: str) -> None:
    assert len(msgs) == 1, (
        f"{label}: expected exactly ONE assistant reply in chat {chat_id}, "
        f"got {len(msgs)}: {[m.get('text', '')[:80] for m in msgs]}"
    )
    text = msgs[0].get("text") or msgs[0].get("message") or ""
    assert token in text, (
        f"{label}: reply in chat {chat_id} does not carry {token!r}: {text[:200]!r}"
    )
    print(f"  PASS {label}: {text[:110]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--workdir", default=None)
    args = ap.parse_args()

    host = urllib.parse.urlsplit(args.base_url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(
            f"refusing to run against {args.base_url!r}: this script is "
            "local-stack only (allowed hosts: 127.0.0.1, localhost, ::1)"
        )

    be = Backend(args.base_url, args.api_key, args.user_id)
    work = Path(args.workdir or (REPO_ROOT / "e2e" / ".work")).resolve()
    work.mkdir(parents=True, exist_ok=True)

    print("== Phase 0: pairings, assistants, chats ==")
    pair_a = be.create_pairing(
        "e2e-achilles-box", [{"agent_route": "default", "name": "Achilles"}],
    )
    pair_b = be.create_pairing(
        "e2e-shadow-box", [{"agent_route": "shadow", "name": "Shadow"}],
    )
    ach_ids = be.bind_agents(int(pair_a["pairing_id"]), [("default", "Achilles")])
    sha_ids = be.bind_agents(int(pair_b["pairing_id"]), [("shadow", "Shadow")])
    achilles, shadow = ach_ids[0], sha_ids[0]
    chat_a = be.create_chat(achilles, "e2e Achilles chat")
    chat_b = be.create_chat(shadow, "e2e Shadow chat")
    print(f"  achilles={achilles} chat={chat_a}; shadow={shadow} chat={chat_b}")

    home_a = work / "homeA"
    home_b = work / "homeB"
    write_secrets(home_a, pair_a["pairing_token"], int(pair_a["pairing_id"]), args.base_url)
    write_secrets(home_b, pair_b["pairing_token"], int(pair_b["pairing_id"]), args.base_url)

    print("== Phase 1: two daemons, interleaved routing ==")
    proc_a = start_daemon(home_a, args.base_url, "Achilles", log_path=work / "daemonA.log")
    proc_b = start_daemon(home_b, args.base_url, "Shadow", log_path=work / "daemonB.log")
    try:
        time.sleep(5)  # both adapters connect + join rooms

        base_a = len(be.messages(chat_a))
        base_b = len(be.messages(chat_b))

        be.send_user_message(chat_b, shadow, "Who are you?")
        expect(
            be.wait_for_assistant_replies(chat_b, base_b + 1),
            "I am Shadow", chat_b, "message to Shadow answered by Shadow",
        )

        base_a = len(be.messages(chat_a))
        be.send_user_message(chat_a, achilles, "Who are you?")
        expect(
            be.wait_for_assistant_replies(chat_a, base_a + 1),
            "I am Achilles", chat_a, "message to Achilles answered by Achilles",
        )

        base_b = len(be.messages(chat_b))
        be.send_user_message(chat_b, shadow, "And now?")
        expect(
            be.wait_for_assistant_replies(chat_b, base_b + 1),
            "I am Shadow", chat_b, "interleaved: Shadow again",
        )

        print("== Phase 2: restart daemon B, identities must hold ==")
        stop_daemon(proc_b)
        proc_b = start_daemon(
            home_b, args.base_url, "Shadow", log_path=work / "daemonB.log",
        )
        time.sleep(5)

        base_b = len(be.messages(chat_b))
        be.send_user_message(chat_b, shadow, "Still you after restart?")
        expect(
            be.wait_for_assistant_replies(chat_b, base_b + 1),
            "I am Shadow", chat_b, "post-restart: Shadow still Shadow",
        )

        base_a = len(be.messages(chat_a))
        be.send_user_message(chat_a, achilles, "And you?")
        expect(
            be.wait_for_assistant_replies(chat_a, base_a + 1),
            "I am Achilles", chat_a, "post-restart: Achilles untouched",
        )
    finally:
        stop_daemon(proc_a)
        stop_daemon(proc_b)

    print("== Phase 3: one daemon, one pairing, two routes (KC's shape) ==")
    pair_c = be.create_pairing(
        "e2e-multi-route-box",
        [
            {"agent_route": "default", "name": "Achilles"},
            {"agent_route": "shadow", "name": "Shadow"},
        ],
    )
    both = be.bind_agents(int(pair_c["pairing_id"]), [("default", "Achilles"), ("shadow", "Shadow")])
    achilles2, shadow2 = both[0], both[1]
    chat_a2 = be.create_chat(achilles2, "e2e multi-route Achilles")
    chat_b2 = be.create_chat(shadow2, "e2e multi-route Shadow")
    home_c = work / "homeC"
    write_secrets(home_c, pair_c["pairing_token"], int(pair_c["pairing_id"]), args.base_url)

    proc_c = start_daemon(
        home_c, args.base_url, "gateway", gateway_sim=True,
        log_path=work / "daemonC.log",
    )
    try:
        time.sleep(5)

        base = len(be.messages(chat_b2))
        be.send_user_message(chat_b2, shadow2, "Who serves you?")
        expect(
            be.wait_for_assistant_replies(chat_b2, base + 1),
            "served-by-profile=shadow", chat_b2,
            "Shadow's turn runs under Hermes profile 'shadow'",
        )

        base = len(be.messages(chat_a2))
        be.send_user_message(chat_a2, achilles2, "Who serves you?")
        expect(
            be.wait_for_assistant_replies(chat_a2, base + 1),
            "served-by-profile=active", chat_a2,
            "Achilles' turn runs under the active (default) profile",
        )

        base = len(be.messages(chat_b2))
        be.send_user_message(chat_b2, shadow2, "Again?")
        expect(
            be.wait_for_assistant_replies(chat_b2, base + 1),
            "served-by-profile=shadow", chat_b2,
            "interleaved multi-route: Shadow stays shadow-profiled",
        )
    finally:
        stop_daemon(proc_c)

    print("\nALL PHASES PASSED: multi-profile Hermes routing on one machine is correct.")


if __name__ == "__main__":
    main()
