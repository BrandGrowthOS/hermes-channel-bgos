"""Tests for the Hermes-plugin registration hooks."""
from __future__ import annotations

import base64
import json

import pytest

from hermes_channel_bgos.plugin import env_enablement, resolve_pairing


def test_resolve_pairing_from_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    monkeypatch.delenv("BGOS_BACKEND_URL", raising=False)
    secrets = tmp_path / "secrets" / "bgos.json"
    secrets.parent.mkdir(parents=True)
    secrets.write_text(json.dumps({"pairing_token": "tok", "base_url": "http://x"}))
    token, base_url = resolve_pairing()
    assert token == "tok"
    assert base_url == "http://x"


def test_resolve_pairing_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "envtok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://env")
    token, base_url = resolve_pairing()
    assert token == "envtok"
    assert base_url == "http://env"


def test_resolve_pairing_none_when_unpaired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    token, base_url = resolve_pairing()
    assert token is None
    assert base_url == "https://api.brandgrowthos.ai"  # prod default


def test_env_enablement_seeds_home_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_HOME_CHANNEL", "830")
    monkeypatch.setenv("BGOS_HOME_CHANNEL_NAME", "Ops")
    seed = env_enablement()
    assert seed is not None
    assert seed["home_channel"] == {"chat_id": "830", "name": "Ops"}


def test_env_enablement_none_when_unpaired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    assert env_enablement() is None


async def test_standalone_send_posts_via_bgos_api(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://x")

    from hermes_channel_bgos import plugin as plugin_mod

    captured = {}

    class FakeApi:
        def __init__(self, config):
            captured["base_url"] = config.base_url
            captured["token"] = config.pairing_token

        async def post_message(self, *, chat_id, text, **kw):
            captured["chat_id"] = chat_id
            captured["text"] = text
            captured["kw"] = kw
            return {"id": 4321}

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(plugin_mod, "BgosApi", FakeApi)

    result = await plugin_mod.standalone_send(None, "830", "scheduled hello")
    assert result["success"] is True
    assert result["message_id"] == 4321
    assert captured["chat_id"] == 830          # coerced to int
    assert captured["text"] == "scheduled hello"
    assert captured["kw"]["files"] is None
    assert captured["token"] == "tok"
    assert captured["closed"] is True


async def test_standalone_send_extracts_media_marker_and_attaches_document(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://x")

    media_cache = tmp_path / "media_cache"
    media_cache.mkdir()
    doc = media_cache / "agent-upload-key.txt"
    doc.write_text("secret-placeholder", encoding="utf-8")

    from hermes_channel_bgos import plugin as plugin_mod

    captured = {}

    class FakeApi:
        def __init__(self, config):
            captured["token"] = config.pairing_token

        async def get_chat(self, chat_id):
            captured["get_chat"] = chat_id
            return {"assistantId": 7}

        async def post_send_message(self, **kw):
            captured["post_send_message"] = kw
            return {"message": {"id": 9876}}

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(plugin_mod, "BgosApi", FakeApi)

    result = await plugin_mod.standalone_send(
        None,
        "830",
        f"Here is the file.\nMEDIA:{doc}\n",
    )

    assert result["success"] is True
    assert result["message_id"] == 9876
    body = captured["post_send_message"]
    assert body["chat_id"] == 830
    assert body["assistant_id"] == 7
    assert body["text"] == "Here is the file."
    assert body["has_attachment"] is True
    files = body["files"]
    assert len(files) == 1
    assert files[0]["fileName"] == "agent-upload-key.txt"
    assert files[0]["fileMimeType"] == "text/plain"
    assert files[0]["isDocument"] is True
    assert files[0]["isImage"] is False
    data_uri = files[0]["fileData"]
    assert data_uri.startswith("data:text/plain;base64,")
    decoded = base64.b64decode(data_uri.split(",", 1)[1]).decode("utf-8")
    assert decoded == "secret-placeholder"
    assert captured["closed"] is True


async def test_standalone_send_accepts_explicit_media_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://x")

    media_cache = tmp_path / "media_cache"
    media_cache.mkdir(exist_ok=True)
    doc = media_cache / "report.txt"
    doc.write_text("hello", encoding="utf-8")

    from hermes_channel_bgos import plugin as plugin_mod

    captured = {}

    class FakeApi:
        def __init__(self, config):
            pass

        async def get_chat(self, chat_id):
            return {"assistantId": 7}

        async def post_send_message(self, **kw):
            captured.update(kw)
            return {"message": {"id": 9877}}

        async def close(self):
            pass

    monkeypatch.setattr(plugin_mod, "BgosApi", FakeApi)

    result = await plugin_mod.standalone_send(
        None,
        "830",
        "report attached",
        media_files=[str(doc)],
    )

    assert result["success"] is True
    assert captured["text"] == "report attached"
    assert captured["files"][0]["fileName"] == "report.txt"


async def test_standalone_send_rejects_media_outside_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://x")

    outside = tmp_path / "outside.txt"
    outside.write_text("do-not-send", encoding="utf-8")

    from hermes_channel_bgos import plugin as plugin_mod

    class FakeApi:
        def __init__(self, config):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(plugin_mod, "BgosApi", FakeApi)

    result = await plugin_mod.standalone_send(
        None,
        "830",
        f"MEDIA:{outside}",
    )

    assert result == {"error": "bgos standalone send: no attachable media files"}


async def test_standalone_send_force_document_overrides_media_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://x")

    media_cache = tmp_path / "media_cache"
    media_cache.mkdir(exist_ok=True)
    png = media_cache / "image.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)

    from hermes_channel_bgos import plugin as plugin_mod

    captured = {}

    class FakeApi:
        def __init__(self, config):
            pass

        async def get_chat(self, chat_id):
            return {"assistantId": 7}

        async def post_send_message(self, **kw):
            captured.update(kw)
            return {"message": {"id": 9878}}

        async def close(self):
            pass

    monkeypatch.setattr(plugin_mod, "BgosApi", FakeApi)

    result = await plugin_mod.standalone_send(
        None,
        "830",
        "image as doc",
        media_files=[str(png)],
        force_document=True,
    )

    assert result["success"] is True
    file_entry = captured["files"][0]
    assert file_entry["fileMimeType"] == "image/png"
    assert file_entry["isDocument"] is True
    assert file_entry["isImage"] is False
    assert file_entry["isVideo"] is False
    assert file_entry["isAudio"] is False


async def test_standalone_send_errors_when_unpaired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    from hermes_channel_bgos import plugin as plugin_mod
    result = await plugin_mod.standalone_send(None, "830", "hi")
    assert "error" in result


def test_plugin_yaml_is_valid_and_declares_platform():
    import pathlib

    import yaml
    repo = pathlib.Path(__file__).resolve().parent.parent
    data = yaml.safe_load((repo / "plugins/platforms/bgos/plugin.yaml").read_text())
    assert data["kind"] == "platform"
    assert data["name"]
    req = {e["name"] for e in data.get("requires_env", [])}
    opt = {e["name"] for e in data.get("optional_env", [])}
    assert "BGOS_AGENTS" in (req | opt)
    assert "BGOS_ALLOW_ALL_USERS" in (req | opt)


def test_register_wires_all_hooks():
    import importlib.util
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "bgos_plugin_adapter", repo / "plugins/platforms/bgos/adapter.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    captured = {}

    class FakeCtx:
        def register_platform(self, **kwargs):
            captured.update(kwargs)

    mod.register(FakeCtx())
    assert captured["name"] == "bgos"
    assert captured["cron_deliver_env_var"] == "BGOS_HOME_CHANNEL"
    assert captured["allow_all_env"] == "BGOS_ALLOW_ALL_USERS"
    assert captured["allowed_users_env"] == "BGOS_ALLOWED_USERS"
    assert callable(captured["adapter_factory"])
    assert callable(captured["env_enablement_fn"])
    assert callable(captured["standalone_sender_fn"])
    assert captured["platform_hint"]
