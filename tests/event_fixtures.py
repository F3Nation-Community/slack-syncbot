"""Full EventContext builders for target-post unit tests."""

from __future__ import annotations

from typing import Any

from handlers._common import EventContext


def make_event_context(**overrides: Any) -> EventContext:
    """Return a complete :class:`EventContext` with sensible defaults for tests."""
    ctx: EventContext = {
        "team_id": "T1",
        "channel_id": "C_SRC",
        "user_id": "U1",
        "msg_text": "",
        "mentioned_users": [],
        "thread_ts": None,
        "ts": "1.000000",
        "event_subtype": None,
        "reply_broadcast": False,
        "content_blocks": [],
        "block_file_ids": [],
    }
    ctx.update(overrides)  # type: ignore[typeddict-item]
    return ctx


FORWARD_PERMALINK = "https://workspace-a.slack.com/archives/C0GENERAL/p1793823790552479"


def forwarded_app_post_body(*, text: str = "", subtype: str | None = None) -> dict[str, Any]:
    """A forward of an app post, shaped like a captured Free-workspace event.

    Names and ids are replaced. The share carries the app's Block Kit as
    ``blocks``, an image block with Slack's output-only keys, a string ``ts``,
    ``author_subname`` for the shown name, and the ``Slack Conversation`` footer.
    """
    event: dict[str, Any] = {
        "type": "message",
        "user": "U0ADA",
        "ts": "1795670312.814729",
        "text": text,
        "team": "T0WORKSPACEA",
        "channel": "C0ANNOUNCE",
        "event_ts": "1795670312.814729",
        "channel_type": "channel",
        "attachments": [
            {
                "id": 1,
                "is_share": True,
                "is_msg_unfurl": True,
                "msg_subtype": "bot_message",
                "ts": "1793823790.552479",
                "channel_id": "C0GENERAL",
                "channel_team": "T0WORKSPACEA",
                "author_name": "App",
                "author_subname": "Ada Lovelace (via App)",
                "author_icon": "https://s3-us-west-2.amazonaws.com/slack-files2/bot_icons/2026-03-25/1_48.png",
                "author_link": "https://workspace-a.slack.com/services/B0APP",
                "from_url": FORWARD_PERMALINK,
                "color": "D0D0D0",
                "mrkdwn_in": ["text"],
                "footer": "Slack Conversation",
                "fallback": "[November 4th, 2026 2:23 PM] Ada Lovelace (via App): NOTES:  All good.",
                "text": "NOTES:  All good.",
                "blocks": [
                    {
                        "type": "section",
                        "block_id": "a1",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*Report! Morning*\n*WHERE*: <#C0GENERAL>\n*LEAD*: <@U0ADA>",
                            "verbatim": False,
                        },
                    },
                    {
                        "type": "rich_text",
                        "block_id": "a2",
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": [
                                    {"type": "text", "text": "NOTES", "style": {"bold": True}},
                                    {"type": "text", "text": ":\n\nAll good."},
                                ],
                            }
                        ],
                    },
                    {
                        "type": "image",
                        "block_id": "a3",
                        "image_url": "https://images.example.com/report.png",
                        "alt_text": "Report",
                        "image_width": 971,
                        "image_height": 1024,
                        "image_bytes": 1356671,
                        "fallback": "971x1024px image",
                        "is_animated": True,
                    },
                    {
                        "type": "actions",
                        "block_id": "a4",
                        "elements": [
                            {"type": "button", "text": {"type": "plain_text", "text": "New Report"}, "action_id": "new"}
                        ],
                    },
                ],
            }
        ],
    }
    if subtype:
        event["subtype"] = subtype
    return {"event_id": "EvFWD1", "team_id": "T0WORKSPACEA", "event": event}
