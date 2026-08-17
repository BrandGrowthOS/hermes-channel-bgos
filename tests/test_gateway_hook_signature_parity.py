"""Signature-parity guard: every BGOSAdapter hook the Hermes gateway calls
MUST accept the kwargs the gateway passes at its call site.

Why this exists (the compounding-habit lock). The gateway invokes the adapter's
send_* hooks by duck-typed keyword call. When the gateway grows a new kwarg at a
call site (e.g. it started passing allow_permanent / allow_session / smart_denied
to send_exec_approval), a plugin hook whose signature does not accept it raises
`TypeError: got an unexpected keyword argument ...`. The gateway swallows that in
a try/except and DOWNGRADES to a degraded path -- for approvals, the plain-text
"/approve" fallback, so the four-button bubble silently vanished (regression
2026-08). The same class already bit send_document (file_path) and
send_multiple_images (metadata) earlier. Per-hook behavioral tests catch one hook
at a time; this guard catches the whole class at once and fails the moment ANY
implemented hook stops binding a kwarg the gateway sends.

THE CONTRACT below is the set of kwargs the gateway passes to each adapter hook,
extracted from the real gateway source. To regenerate after a Hermes upgrade, run
this AST walk against the live gateway (path is machine-local, not shipped):

    import ast, collections
    src = open("~/.hermes/hermes-agent/gateway/run.py").read()
    tree = ast.parse(src)
    hooks = collections.defaultdict(set)
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id.endswith("adapter")
                and n.func.attr.startswith("send")):
            for kw in n.keywords:
                hooks[n.func.attr].add(kw.arg if kw.arg else "**")

A "**" entry means the gateway spreads a dict at that call site, so the plugin
hook MUST carry a **kwargs catch-all to stay forward-compatible.
"""
from __future__ import annotations

import inspect

from hermes_channel_bgos.bgos_adapter import BGOSAdapter


# The kwargs the Hermes gateway passes to each adapter hook. Source of truth:
# ~/.hermes/hermes-agent/gateway/run.py (see module docstring for extraction).
GATEWAY_HOOK_KWARGS: dict[str, list[str]] = {
    "send": ["chat_id", "content", "metadata", "reply_to"],
    "send_clarify": [
        "chat_id", "choices", "clarify_id", "metadata", "question", "session_key",
    ],
    "send_document": ["chat_id", "file_path", "metadata"],
    "send_exec_approval": [
        "allow_permanent", "allow_session", "chat_id", "command",
        "description", "metadata", "session_key", "smart_denied",
    ],
    "send_image": ["caption", "chat_id", "image_url", "metadata"],
    "send_image_file": ["caption", "chat_id", "image_path", "metadata"],
    "send_multiple_images": ["chat_id", "images", "metadata"],
    "send_private_notice": ["metadata"],
    "send_slash_confirm": [
        "chat_id", "confirm_id", "message", "metadata", "session_key", "title",
    ],
    "send_typing": ["metadata"],
    "send_update_prompt": [
        "chat_id", "default", "metadata", "prompt", "session_key",
    ],
    "send_video": ["chat_id", "metadata", "video_path"],
    "send_voice": ["**", "audio_path", "chat_id", "metadata"],
}

# Hooks the plugin intentionally does NOT implement. The gateway guards each of
# these so absence degrades gracefully rather than crashing, so it is fine for
# BGOSAdapter to omit them:
#   send_clarify        -- only invoked if the agent calls the clarify tool; BGOS
#                          exposes its own richer ask_user_input modal + inline
#                          option buttons, so it does not wire the Hermes primitive.
#   send_private_notice -- called only when notice delivery is configured
#                          "private" AND inside a try/except that falls back to
#                          adapter.send(); BGOS is DM-only so this never fires.
# If one of these ever IS implemented, it must still bind its gateway kwargs, so
# the guard below checks binding whenever the method is present.
EXPECTED_UNIMPLEMENTED = {"send_clarify", "send_private_notice"}


def _has_var_keyword(sig: inspect.Signature) -> bool:
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def _binds_kwarg(sig: inspect.Signature, name: str) -> bool:
    p = sig.parameters.get(name)
    if p is not None and p.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    ):
        return True
    return _has_var_keyword(sig)


def test_every_implemented_gateway_hook_binds_its_kwargs():
    """No implemented adapter hook may reject a kwarg the gateway passes.

    This is the regression that guards the whole signature-drift class. It fails
    if send_exec_approval loses allow_permanent again, if send_document loses
    file_path, if send_multiple_images loses metadata, etc.
    """
    failures: list[str] = []
    for hook, kwargs in GATEWAY_HOOK_KWARGS.items():
        method = getattr(BGOSAdapter, hook, None)
        if method is None:
            if hook in EXPECTED_UNIMPLEMENTED:
                continue
            failures.append(f"{hook}: MISSING (gateway calls it, plugin lacks it)")
            continue
        sig = inspect.signature(method)
        for kw in kwargs:
            if kw == "**":
                if not _has_var_keyword(sig):
                    failures.append(
                        f"{hook}: gateway spreads **kwargs here but the hook has "
                        f"no **kwargs catch-all"
                    )
                continue
            if not _binds_kwarg(sig, kw):
                failures.append(f"{hook}: rejects kwarg '{kw}'")
    assert not failures, "gateway/plugin signature drift:\n  " + "\n  ".join(failures)


def test_unimplemented_hooks_still_bind_if_they_get_implemented():
    """If a currently-optional hook is later added, it must accept the gateway's
    kwargs from day one -- otherwise it would ship the same TypeError-fallback
    bug the moment the gateway starts calling it."""
    failures: list[str] = []
    for hook in EXPECTED_UNIMPLEMENTED:
        method = getattr(BGOSAdapter, hook, None)
        if method is None:
            continue  # still optional -- fine
        sig = inspect.signature(method)
        for kw in GATEWAY_HOOK_KWARGS[hook]:
            if kw == "**":
                if not _has_var_keyword(sig):
                    failures.append(f"{hook}: missing **kwargs catch-all")
                continue
            if not _binds_kwarg(sig, kw):
                failures.append(f"{hook}: rejects kwarg '{kw}'")
    assert not failures, "newly-implemented hook drifts:\n  " + "\n  ".join(failures)
