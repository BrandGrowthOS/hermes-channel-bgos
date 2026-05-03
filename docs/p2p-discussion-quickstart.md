# P2P Discussion (Cross-channel agent-to-agent) — Hermes quickstart

This is the operator-facing runbook for upgrading a Hermes deployment to use the BGOS peer-to-peer (P2P) discussion feature. After applying these steps, every Hermes agent on a paired BGOS account can discover, message, and collaborate with the user's other BGOS assistants (Claude Code, OpenClaw, Gobot, n8n agents) live in the chat.

> **Source of truth:** `docs/bgos-agent-capabilities.md` §11.

## What changed in this release

Vendor pkg `hermes_channel_bgos` v0.5.0 adds:

1. **Six new methods on `BgosApi`** — `list_peers`, `peer_status`, `send_to_peer`, `complete_peer_thread`, `complete_side_thread`, `get_peer_inbox`. All carry the new `X-Caller-Assistant-Id` header in addition to the existing `X-BGOS-Pairing` token.
2. **Four new bridge-local slash commands** the user (or the agent via `/`) can run in any BGOS chat:
   - `/peers` — list discoverable peers with introduced ✓/✗ flags
   - `/peer-status <name|id>` — online state + open conversation
   - `/peer-send <name|id> <text> [--wait]` — send to peer
   - `/peer-complete [<summary>]` — close most recent open peer conversation
3. **Two agent-emitted marker blocks** for in-reply collaboration:
   - `[[BGOS_PEER_SEND name="..." text="..." wait="false" turn="expecting_reply"]]`
   - `[[BGOS_PEER_COMPLETE summary="..."]]`

The adapter extracts each marker, strips it from the user-visible reply, posts the cleaned reply, and then dispatches the parsed directive against the new BGOS peer endpoints. The user sees a `<SideConversationCard>` live-render each turn under the agent's reply.

## How to upgrade a deployment

```bash
# On the Hermes host
pip install --upgrade hermes-channel-bgos==0.5.0   # or `pip install -e .` from this repo
systemctl restart hermes                              # adapter picks up the new methods
```

There are **no fork-side changes required** for the slash commands and markers — they're entirely bridge-local. The only fork-side update is the recommended **`PLATFORM_HINTS` rewrite** so the agent's system prompt mentions the peer collaboration capability. See `hermes-fork-patch/FORK-NOTES.md` (2026-05-03 update) for the exact text to splice in.

## Verification (5 minutes)

1. **Pair the agent normally** with `hermes-pair-bgos <CODE> --device-label test`.
2. **Open the BGOS app** and start a chat with the Hermes-bound assistant.
3. **Type `/peers`** — expect a markdown table listing every other assistant on your account with an introduced ✗ marker.
4. **Open BGOS Settings → Agent Permissions** and enable the row from your Hermes assistant to one of the others (e.g. an n8n LLM or a Claude Code session).
5. **Type `/peers` again** — that row should now show ✓.
6. **Type `/peer-send <name> Hello peer!`** — expect:
   - A "Looping in peer..." reply from your Hermes assistant.
   - A `<SideConversationCard>` rendered under that reply.
   - The peer assistant receives the message tagged with `fromAgent` (i.e. cyan bubble, not a normal user message).
7. **Have the peer reply** (it must set `replyToId` to the inbound message id; this is automatic in v0.5.0+ adapters).
8. **Type `/peer-complete Done.`** — the card flips to completed-collapsed with the summary.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/peers` lists nothing | This account has only one assistant | Create a second one (n8n, Claude Code) and re-run |
| Every peer shows ✗ | No introductions enabled | Open BGOS Settings → Agent Permissions → toggle the row |
| `send_to_peer` returns `requires_introduction` | Same as above | Same fix |
| Peer never replies, `--wait` times out | Peer's adapter doesn't set `replyToId` (older version) | Upgrade peer adapter to the matching version |
| Card doesn't render in BGOS | Frontend version too old | BGOS desktop ≥ 1.19.0 / mobile build ≥ 2026-04-30 |

## Agent-facing usage examples

Tell your Hermes agents about the marker syntax via the `PLATFORM_HINTS` BGOS entry. Example reply that uses peer collaboration:

```markdown
I'll loop in Hades for the AWS bucket creation.

[[BGOS_PEER_SEND name="Hades" text="Please create bgos-dev-uploads in us-east-1 with public access blocked." wait="true"]]
```

After Hades replies, the agent's next turn closes the conversation:

```markdown
Hades confirmed the bucket is up. You can upload to s3://bgos-dev-uploads/ now.

[[BGOS_PEER_COMPLETE summary="Hades created bgos-dev-uploads in us-east-1, public access blocked"]]
```
