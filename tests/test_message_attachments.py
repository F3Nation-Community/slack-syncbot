"""Forwards, app attachments, and link previews: built, posted, edited, and rewritten."""

from __future__ import annotations

import copy
import os
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_HOST", "localhost")
os.environ.setdefault("DATABASE_USER", "root")
os.environ.setdefault("DATABASE_PASSWORD", "test")
os.environ.setdefault("DATABASE_SCHEMA", "syncbot")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")

from slack_sdk.errors import SlackApiError  # noqa: E402

from federation.deliver import build_remote_envelope  # noqa: E402
from handlers.message import _build_file_context, respond_to_message_event  # noqa: E402
from helpers.message_blocks import (  # noqa: E402
    NOTE_COPY_DIFFERS,
    append_link_lines,
    build_attachments_for_sync,
    build_content_blocks_for_sync,
    rewrite_attachments,
    rewrite_content_blocks,
    text_from_attachments,
)
from helpers.slack_api import post_message  # noqa: E402
from helpers.slack_write import slack_write_create, slack_write_edit  # noqa: E402
from helpers.user_map import resolve_channel_references, rewrite_envelope_channel_refs  # noqa: E402
from tests.event_fixtures import FORWARD_PERMALINK, forwarded_app_post_body  # noqa: E402


def _share(**overrides):
    attachment = copy.deepcopy(forwarded_app_post_body()["event"]["attachments"][0])
    attachment.update(overrides)
    return attachment


def _card():
    return build_attachments_for_sync([_share()])[0]


class TestShareCard:
    def test_card_from_the_captured_forward(self):
        cards = build_attachments_for_sync([_share()])

        assert len(cards) == 1
        card = cards[0]
        assert set(card) == {"color", "fallback", "blocks"}
        assert card["color"] == "D0D0D0"
        assert card["fallback"].startswith("[November 4th, 2026 2:23 PM] Ada Lovelace (via App)")
        header, channel_line, *body = card["blocks"]
        assert header["type"] == "context"
        assert header["elements"][0] == {
            "type": "image",
            "image_url": "https://s3-us-west-2.amazonaws.com/slack-files2/bot_icons/2026-03-25/1_48.png",
            "alt_text": "Ada Lovelace (via App)",
        }
        assert header["elements"][1]["text"] == (
            f"*<{FORWARD_PERMALINK}|Ada Lovelace (via App)>*  <!date^1793823790^{{date_short_pretty}}|Nov 4, 2026>"
        )
        assert channel_line == {"type": "context", "elements": [{"type": "mrkdwn", "text": "Posted in <#C0GENERAL>"}]}
        assert [block["type"] for block in body] == ["section", "rich_text", "image"]
        assert body[2] == {"type": "image", "image_url": "https://images.example.com/report.png", "alt_text": "Report"}

    def test_a_persons_forward_from_a_dm_uses_author_name_and_has_no_channel_line(self):
        share = _share(author_name="Ada Lovelace", channel_id="D0DIRECT", text="hello there", blocks=[])
        share.pop("author_subname")

        header, body = build_attachments_for_sync([share])[0]["blocks"]

        assert "|Ada Lovelace>*" in header["elements"][1]["text"]
        assert body == {"type": "section", "text": {"type": "mrkdwn", "text": "hello there"}}

    def test_message_blocks_are_the_body_when_the_share_has_no_blocks(self):
        rich = {
            "type": "rich_text",
            "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "hi"}]}],
        }
        share = _share(text="", blocks=[], message_blocks=[{"message": {"blocks": [rich]}}])

        blocks = build_attachments_for_sync([share])[0]["blocks"]

        assert blocks[-1] == rich

    def test_broadcasts_in_a_share_do_not_ping(self):
        share = _share(
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": "<!here> read this"}},
                {
                    "type": "rich_text",
                    "elements": [
                        {"type": "rich_text_section", "elements": [{"type": "broadcast", "range": "channel"}]}
                    ],
                },
            ],
            fallback="<!everyone> read this",
        )

        card = build_attachments_for_sync([share])[0]

        assert card["blocks"][2]["text"]["text"] == "@here read this"
        assert card["blocks"][3]["elements"][0]["elements"][0] == {"type": "text", "text": "@channel"}
        assert card["fallback"] == "@everyone read this"

    def test_card_words_for_text_only_posts(self):
        words = text_from_attachments([_card()])

        assert "Ada Lovelace (via App)" in words
        assert "Posted in <#C0GENERAL>" in words
        assert "*Report! Morning*" in words
        assert "All good." in words
        assert "Slack Conversation" not in words


class TestOtherAttachments:
    def test_image_blocks_keep_only_input_keys(self):
        raw = _share()["blocks"][2]

        assert build_content_blocks_for_sync([raw]) == [
            {"type": "image", "image_url": "https://images.example.com/report.png", "alt_text": "Report"}
        ]

    def test_app_attachment_keeps_documented_fields_and_an_integer_ts(self):
        raw = {
            "pretext": "Deploy",
            "title": "Build 42",
            "title_link": "https://ci.example.com/42",
            "text": "<!here> deployed",
            "fields": [{"title": "Env", "value": "prod", "short": True}],
            "footer": "CI",
            "footer_icon": "https://files.slack.com/files-pri/T1-F1/icon.png",
            "author_icon": "https://ci.example.com/icon.png",
            "color": "good",
            "ts": "1733497200.423469",
            "actions": [{"type": "button", "text": "Retry"}],
            "callback_id": "retry",
        }

        built = build_attachments_for_sync([raw])[0]

        assert built["ts"] == 1733497200
        assert built["text"] == "<!here> deployed"
        assert built["fields"] == [{"title": "Env", "value": "prod", "short": True}]
        assert built["author_icon"] == "https://ci.example.com/icon.png"
        assert "footer_icon" not in built
        assert "actions" not in built
        assert "callback_id" not in built
        assert "ts" not in build_attachments_for_sync([{"text": "x", "ts": "soon"}])[0]

    def test_link_preview_shows_its_service_and_is_not_an_image(self):
        preview = {
            "original_url": "https://example.com/post",
            "from_url": "https://example.com/post",
            "service_name": "Example",
            "service_icon": "https://example.com/favicon.png",
            "title": "A post",
            "title_link": "https://example.com/post",
            "text": "About the post",
            "thumb_url": "https://example.com/thumb.png",
        }
        body = {"event": {"type": "message", "subtype": "message_changed", "message": {"attachments": [preview]}}}

        built = build_attachments_for_sync([preview])[0]
        photo_blocks, _files = _build_file_context(body, MagicMock(), MagicMock())

        assert built["author_name"] == "Example"
        assert built["author_icon"] == "https://example.com/favicon.png"
        assert built["thumb_url"] == "https://example.com/thumb.png"
        assert photo_blocks == []

    def test_giphy_stays_an_image_block(self):
        gif = {"image_url": "https://media.giphy.com/media/x/giphy.gif", "fallback": "cat.gif"}
        body = {"event": {"type": "message", "attachments": [gif]}}

        photo_blocks, _files = _build_file_context(body, MagicMock(), MagicMock())

        assert build_attachments_for_sync([gif]) == []
        assert photo_blocks[0]["image_url"] == "https://media.giphy.com/media/x/giphy.gif"


def _handler_patches(stack: ExitStack, **extra):
    patches = {
        "handlers.message._is_own_bot_message": False,
        "handlers.message.helpers.has_channel_sync": True,
        "handlers.message.helpers.origin_publishes_anywhere": True,
        "handlers.message.helpers.iter_publish_targets": [object()],
        "handlers.message.helpers.post_meta_exists_for_channel_ts": False,
        "handlers.message.helpers.check_pending_event": False,
        "handlers.message.helpers.parse_mentioned_users": [],
    }
    patches.update(extra)
    mocks = {}
    for target, value in patches.items():
        mocks[target] = stack.enter_context(patch(target, return_value=value))
    stack.enter_context(patch("handlers.message.run_claimed", side_effect=lambda _body, fn: fn()))
    mocks["new_post"] = stack.enter_context(patch("handlers.message._handle_new_post"))
    mocks["edit"] = stack.enter_context(patch("handlers.message._handle_message_edit"))
    mocks["notify"] = stack.enter_context(patch("handlers.message.helpers.notify_source_user_error"))
    mocks["warn"] = stack.enter_context(patch("handlers.message.log_warning"))
    return mocks


def _run(body, **extra):
    with ExitStack() as stack:
        mocks = _handler_patches(stack, **{"handlers.message._build_file_context": ([], []), **extra})
        respond_to_message_event(body, MagicMock(), MagicMock(), {})
    return mocks


def _link_preview_body(text, blocks=None, *, subtype=None):
    """A message holding a link to another message, with Slack's preview of it."""
    body = forwarded_app_post_body(text=text, subtype=subtype)
    event = body["event"]
    preview = event["attachments"][0]
    preview.pop("is_share")
    preview["original_url"] = FORWARD_PERMALINK
    if blocks is not None:
        event["blocks"] = blocks
    if subtype == "message_changed":
        body["event"] = {"type": "message", "subtype": subtype, "channel": event["channel"], "message": event}
    return body


def _mention_block(url, label):
    element = {"type": "message_mention", "url": url, "text": label, "channel_id": "C0GENERAL"}
    if label != url:
        element = {"type": "link", "url": url, "text": label}
    return {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [element]}]}


class TestForwardHandler:
    def test_the_comment_then_the_link_then_the_card(self):
        for comment, expected in (
            ("", f"<{FORWARD_PERMALINK}>"),
            ("please read", f"please read\n<{FORWARD_PERMALINK}>"),
        ):
            mocks = _run(forwarded_app_post_body(text=comment))

            mocks["new_post"].assert_called_once()
            ctx = mocks["new_post"].call_args.args[3]
            assert ctx["msg_text"] == expected
            assert ctx["attachments"][0]["blocks"][0]["type"] == "context"
            mention_text = mocks["handlers.message.helpers.parse_mentioned_users"].call_args.args[0]
            assert "<@U0ADA>" in mention_text
            mocks["notify"].assert_not_called()

    def test_a_rich_text_comment_gets_the_link_after_it(self):
        comment = {
            "type": "rich_text",
            "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "please read"}]}],
        }
        body = forwarded_app_post_body(text="please read")
        body["event"]["blocks"] = [comment]

        ctx = _run(body)["new_post"].call_args.args[3]

        assert ctx["content_blocks"][0] == comment
        assert ctx["content_blocks"][-1]["elements"][0]["elements"] == [{"type": "link", "url": FORWARD_PERMALINK}]

    def test_an_edited_forward_keeps_the_link_and_the_card(self):
        body = forwarded_app_post_body(text="please read")
        event = body["event"]
        body["event"] = {"type": "message", "subtype": "message_changed", "channel": event["channel"], "message": event}

        ctx = _run(body)["edit"].call_args.args[2]

        assert ctx["msg_text"] == f"please read\n<{FORWARD_PERMALINK}>"
        assert ctx["attachments"][0]["blocks"][0]["type"] == "context"

    def test_a_bare_message_link_gets_the_same_card_as_a_forward(self):
        text = f"see <{FORWARD_PERMALINK}|{FORWARD_PERMALINK}>"
        forward_card = build_attachments_for_sync([_share()])[0]
        for subtype, handler, ctx_arg in ((None, "new_post", 3), ("message_changed", "edit", 2)):
            body = _link_preview_body(text, [_mention_block(FORWARD_PERMALINK, FORWARD_PERMALINK)], subtype=subtype)

            ctx = _run(body)[handler].call_args.args[ctx_arg]

            assert ctx["attachments"] == [forward_card]
            assert ctx["msg_text"].count(FORWARD_PERMALINK) == 2

    def test_the_previews_name_labels_the_link(self):
        """The card's ``author_subname`` is noted, so the link reads like Slack's: ``{name} in #channel``."""
        from helpers._cache import clear_all_caches
        from helpers.user_map import _message_link_name

        clear_all_caches()
        body = _link_preview_body(
            f"<{FORWARD_PERMALINK}|{FORWARD_PERMALINK}>",
            [_mention_block(FORWARD_PERMALINK, FORWARD_PERMALINK)],
            subtype="message_changed",
        )

        _run(body)

        assert _message_link_name(FORWARD_PERMALINK, None) == "Ada Lovelace (via App)"
        clear_all_caches()

    def test_a_link_with_the_authors_own_words_gets_no_card(self):
        text = f"see <{FORWARD_PERMALINK}|the morning report>"
        body = _link_preview_body(text, [_mention_block(FORWARD_PERMALINK, "the morning report")])

        ctx = _run(body)["new_post"].call_args.args[3]

        assert ctx["attachments"] == []
        assert ctx["msg_text"] == text
        assert ctx["content_blocks"][0]["elements"][0]["elements"][0]["text"] == "the morning report"

    def test_a_link_both_worded_and_bare_keeps_its_card(self):
        text = f"see <{FORWARD_PERMALINK}|the report> and <{FORWARD_PERMALINK}?thread_ts=1.0&cid=C0GENERAL>"
        body = _link_preview_body(text)

        assert len(_run(body)["new_post"].call_args.args[3]["attachments"]) == 1

    def test_an_edit_does_not_download_forward_files(self):
        body = forwarded_app_post_body()
        share = body["event"]["attachments"][0]
        share["files"] = [{"id": "F0PDF", "mode": "hosted", "url_private": "https://files.slack.com/F0PDF"}]
        event = body["event"]
        body["event"] = {
            "type": "message",
            "subtype": "message_changed",
            "channel": event["channel"],
            "message": {**event, "ts": event["ts"]},
        }
        with ExitStack() as stack:
            mocks = _handler_patches(stack, **{"handlers.message._build_file_context": ([], [])})
            download = stack.enter_context(patch("handlers.message._download_forward_files"))
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        download.assert_not_called()
        mocks["edit"].assert_called_once()

    def test_an_unreadable_forward_file_is_marked_and_the_card_still_posts(self):
        body = forwarded_app_post_body()
        body["event"]["attachments"][0]["files"] = [
            {"id": "F0PDF", "mode": "hosted", "url_private": "https://files.slack.com/F0PDF"}
        ]
        with ExitStack() as stack:
            mocks = _handler_patches(
                stack,
                **{
                    "handlers.message._build_file_context": ([], []),
                    "handlers.message.helpers.download_slack_files": [],
                    "handlers.message.helpers.get_user_token": None,
                },
            )
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mocks["new_post"].assert_called_once()
        ctx = mocks["new_post"].call_args.args[3]
        assert ctx["files_failed"] is True
        assert ctx["msg_text"].endswith(f"_{NOTE_COPY_DIFFERS}_")
        # The one DM goes out after fan-out, from the post handler.
        mocks["notify"].assert_not_called()

    def test_forward_file_falls_back_to_the_forwarders_token(self):
        body = forwarded_app_post_body()
        body["event"]["attachments"][0]["files"] = [
            {"id": "F0PDF", "mode": "hosted", "url_private": "https://files.slack.com/F0PDF"}
        ]
        downloaded = [{"path": "/tmp/report.pdf", "name": "report.pdf", "slack_file_id": "F0PDF"}]
        with ExitStack() as stack:
            mocks = _handler_patches(
                stack,
                **{
                    "handlers.message._build_file_context": ([], []),
                    "handlers.message.helpers.get_user_token": "xoxp-forwarder",
                },
            )
            download = stack.enter_context(
                patch("handlers.message.helpers.download_slack_files", side_effect=[[], downloaded])
            )
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        assert download.call_count == 2
        mocks["handlers.message.helpers.get_user_token"].assert_called_once_with("T0WORKSPACEA", "U0ADA")
        assert mocks["new_post"].call_args.args[5] == downloaded
        mocks["notify"].assert_not_called()


class TestNothingReadable:
    def _body(self):
        body = forwarded_app_post_body()
        event = body["event"]
        event.pop("attachments")
        event["blocks"] = [
            {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Go"}}]}
        ]
        return body

    def test_posts_the_source_permalink_and_warns(self):
        client = MagicMock()
        client.chat_getPermalink.return_value = {"ok": True, "permalink": FORWARD_PERMALINK}
        with ExitStack() as stack:
            mocks = _handler_patches(stack, **{"handlers.message._build_file_context": ([], [])})
            respond_to_message_event(self._body(), client, MagicMock(), {})

        mocks["new_post"].assert_called_once()
        assert mocks["new_post"].call_args.args[3]["msg_text"] == f"{FORWARD_PERMALINK}\n_{NOTE_COPY_DIFFERS}_"
        degraded = [c for c in mocks["warn"].call_args_list if c.args[0] == "message_degraded"]
        assert degraded[0].kwargs["reason"] == "nothing_readable"
        assert degraded[0].kwargs["block_types"] == ["actions"]
        mocks["notify"].assert_not_called()

    def test_without_a_permalink_nothing_posts(self):
        client = MagicMock()
        client.chat_getPermalink.side_effect = SlackApiError("nope", {"ok": False, "error": "channel_not_found"})
        with ExitStack() as stack:
            mocks = _handler_patches(stack, **{"handlers.message._build_file_context": ([], [])})
            respond_to_message_event(self._body(), client, MagicMock(), {})

        mocks["new_post"].assert_not_called()
        skips = [c for c in mocks["warn"].call_args_list if c.args[0] == "message_skip"]
        assert skips[0].kwargs["reason"] == "nothing_readable"


class TestCanvasAndSubtypes:
    def test_canvas_share_posts_its_link_without_a_dm(self):
        canvas = {
            "id": "F0CANVAS",
            "mode": "quip",
            "permalink": "https://workspace-a.slack.com/docs/T0WORKSPACEA/F0CANVAS",
        }
        body = forwarded_app_post_body(subtype="file_share")
        body["event"].pop("attachments")
        body["event"]["files"] = [canvas]
        with ExitStack() as stack:
            mocks = _handler_patches(stack, **{"handlers.message._build_file_context": ([], [])})
            respond_to_message_event(body, MagicMock(), MagicMock(), {})

        mocks["new_post"].assert_called_once()
        assert canvas["permalink"] in mocks["new_post"].call_args.args[3]["msg_text"]
        mocks["notify"].assert_not_called()

    def test_hosted_file_without_a_url_is_not_a_link(self):
        from helpers.files import get_link_only_file_url

        assert (
            get_link_only_file_url({"mode": "hosted", "permalink": "https://workspace-a.slack.com/files/U/F/a.pdf"})
            is None
        )

    def test_unknown_subtype_posts_with_a_warning_and_hidden_skips(self):
        for hidden, posts in ((False, True), (True, False)):
            body = forwarded_app_post_body(text="hello", subtype="some_new_slack_type")
            body["event"].pop("attachments")
            if hidden:
                body["event"]["hidden"] = True
            with ExitStack() as stack:
                mocks = _handler_patches(stack, **{"handlers.message._build_file_context": ([], [])})
                respond_to_message_event(body, MagicMock(), MagicMock(), {})

            assert mocks["new_post"].called is posts
            event = "message_degraded" if posts else "message_skip"
            warned = [c for c in mocks["warn"].call_args_list if c.args[0] == event]
            assert warned[0].kwargs["reason"] == "unknown_subtype"

    def test_system_subtypes_skip(self):
        for subtype in ("channel_join", "huddle_thread", "bot_add", "message_replied"):
            body = forwarded_app_post_body(text="joined", subtype=subtype)
            with ExitStack() as stack:
                mocks = _handler_patches(stack, **{"handlers.message._build_file_context": ([], [])})
                respond_to_message_event(body, MagicMock(), MagicMock(), {})
            mocks["new_post"].assert_not_called()


def _workspace():
    return SimpleNamespace(id=2, team_id="T_TARGET")


def _sync_channel():
    return SimpleNamespace(id=22, channel_id="C_TARGET", sync_id=7, subscribes=True)


def _envelope(**overrides):
    envelope = {
        "kind": "message",
        "action": "create",
        "post_id": "P1",
        "source_workspace_id": 1,
        "source_user_id": "U0ADA",
        "mapped_user_id": None,
        "user_name": "Ada Lovelace",
        "workspace_name": "Workspace A",
        "text": " ",
        "attachments": [_card()],
    }
    envelope.update(overrides)
    return envelope


def _refused(code="invalid_attachments"):
    return SlackApiError("refused", {"ok": False, "error": code})


def _write_patches(stack: ExitStack, post_side_effect):
    stack.enter_context(patch("helpers.slack_write.get_bot_token", return_value="xoxb-bot"))
    stack.enter_context(patch("helpers.slack_write.get_user_token", return_value=None))
    stack.enter_context(patch("helpers.slack_write.WebClient"))
    stack.enter_context(patch("helpers.workspace.get_workspace_by_id", return_value=None))
    stack.enter_context(
        patch(
            "helpers.slack_write.get_display_name_and_icon_for_synced_message",
            return_value=("Ada Lovelace", "https://example.com/ada.png", False, None),
        )
    )
    return stack.enter_context(patch("helpers.slack_write.post_message", side_effect=post_side_effect))


class TestPostingCards:
    def test_create_posts_the_card(self):
        with ExitStack() as stack:
            post = _write_patches(stack, [{"ts": "9.0"}])
            ts, _posted_as, error = slack_write_create(
                envelope=_envelope(), sync_channel=_sync_channel(), workspace=_workspace()
            )

        assert (ts, error) == ("9.0", None)
        assert post.call_args.kwargs["attachments"] == [_card()]
        assert post.call_args.kwargs["msg_text"].strip() == ""

    def test_refused_card_posts_its_words_as_text(self):
        with ExitStack() as stack:
            post = _write_patches(stack, [_refused(), {"ts": "9.0"}])
            ts, _posted_as, _error = slack_write_create(
                envelope=_envelope(text="please read"), sync_channel=_sync_channel(), workspace=_workspace()
            )

        assert ts == "9.0"
        retry = post.call_args_list[1].kwargs
        assert "attachments" not in retry
        assert retry["msg_text"].startswith("please read\n")
        assert "Ada Lovelace (via App)" in retry["msg_text"]
        assert "All good." in retry["msg_text"]

    def test_edit_keeps_the_card_and_retries_as_text(self):
        meta = SimpleNamespace(ts="10.000000", posted_as_user_id=None)
        with ExitStack() as stack:
            post = _write_patches(stack, [None])
            assert slack_write_edit(
                envelope=_envelope(action="edit"),
                sync_channel=_sync_channel(),
                workspace=_workspace(),
                target_post_meta=meta,
            )
        assert post.call_args.kwargs["attachments"] == [_card()]
        assert post.call_args.kwargs["update_ts"] == "10.000000"

        with ExitStack() as stack:
            post = _write_patches(stack, [_refused(), None])
            assert slack_write_edit(
                envelope=_envelope(action="edit"),
                sync_channel=_sync_channel(),
                workspace=_workspace(),
                target_post_meta=meta,
            )
        retry = post.call_args_list[1].kwargs
        assert retry["attachments"] == []
        assert "All good." in retry["msg_text"]

    def test_a_forward_with_files_puts_the_card_on_the_upload(self):
        envelope = _envelope(text="please read", file_refs=[{"path": "/tmp/report.pdf", "name": "report.pdf"}])
        with ExitStack() as stack:
            post = _write_patches(stack, [])
            upload = stack.enter_context(
                patch("helpers.slack_write.upload_files_to_slack", return_value=(None, "20.0"))
            )
            ts, _posted_as, _error = slack_write_create(
                envelope=envelope, sync_channel=_sync_channel(), workspace=_workspace()
            )

        assert ts == "20.0"
        post.assert_not_called()
        kwargs = upload.call_args.kwargs
        assert kwargs["initial_comment"] is None
        assert kwargs["blocks"][0] == {"type": "section", "text": {"type": "mrkdwn", "text": "please read"}}
        assert kwargs["blocks"][1:] == _card()["blocks"]

    def test_post_message_omits_blank_text_on_create_and_sends_it_on_update(self):
        card = _card()
        with patch("helpers.slack_api.WebClient") as client_cls:
            client = client_cls.return_value
            client.chat_postMessage.return_value = {"ts": "1.0"}
            post_message(bot_token="xoxb", channel_id="C1", msg_text=" ", attachments=[card])
            post_message(bot_token="xoxb", channel_id="C1", msg_text=" ", update_ts="1.0")

        created = client.chat_postMessage.call_args.kwargs
        assert "text" not in created
        assert created["attachments"] == [card]
        updated = client.chat_update.call_args.kwargs
        assert updated["text"] == ""
        assert updated["attachments"] == []


class TestFederationRewrite:
    def test_origin_rewrites_the_card_and_the_peer_payload_keeps_it(self):
        envelope = _envelope()
        with patch(
            "helpers.user_map.resolve_channel_references",
            side_effect=lambda text, *_a, **_k: text.replace("<#C0GENERAL>", "`#general (Workspace A)`"),
        ):
            rewrite_envelope_channel_refs(envelope, MagicMock(), SimpleNamespace(id=1, name="Workspace A"))

        channel_line = envelope["attachments"][0]["blocks"][1]["elements"][0]["text"]
        assert channel_line == "Posted in `#general (Workspace A)`"
        remote = build_remote_envelope(envelope, "C_REMOTE")
        assert remote["attachments"] == envelope["attachments"]

    def test_an_appended_link_is_labeled_in_text_and_blocks(self):
        comment = [
            {
                "type": "rich_text",
                "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "hi"}]}],
            }
        ]
        text, blocks = append_link_lines("hi", comment, [FORWARD_PERMALINK, FORWARD_PERMALINK])
        label = f"<{FORWARD_PERMALINK}|message in #general (Workspace A)>"

        rewritten = rewrite_content_blocks(
            blocks, lambda value: value.replace(FORWARD_PERMALINK, label), lambda uid: uid, lambda uid: uid
        )

        assert text == f"hi\n<{FORWARD_PERMALINK}>"
        assert rewritten[-1]["elements"][0]["elements"] == [
            {"type": "link", "url": FORWARD_PERMALINK, "text": "message in #general (Workspace A)"}
        ]
        assert append_link_lines(text, blocks, [FORWARD_PERMALINK]) == (text, blocks)

    def test_a_reply_preview_keeps_the_authors_name_linked_and_says_from_a_thread(self):
        """A reply's permalink has ``?thread_ts=…&cid=…``; the rewrite must not split the header link."""
        reply_url = (
            "https://workspace-a.slack.com/archives/C0GENERAL/p1733497250123456"
            "?thread_ts=1733497200.423469&cid=C0GENERAL"
        )
        preview = {
            "fallback": "[December 6th, 2024 3:00 PM] Ada Lovelace: I see it!",
            "from_url": reply_url,
            "original_url": reply_url,
            "author_subname": "Ada Lovelace",
            "author_name": "SyncBot",
            "is_msg_unfurl": True,
            "is_reply_unfurl": True,
            "ts": "1733497250.123456",
            "channel_id": "C0GENERAL",
            "text": "I see it!",
            "footer": "Thread in Slack Conversation",
        }

        card = build_attachments_for_sync([preview])[0]
        header = card["blocks"][0]["elements"][-1]["text"]

        assert header.startswith(f"*<{reply_url}|Ada Lovelace>*")
        assert resolve_channel_references(header, None, None) == header
        assert card["blocks"][1]["elements"][0]["text"] == "From a thread in <#C0GENERAL>"

    def test_rewrite_attachments_reaches_text_fields_and_blocks(self):
        raw = [{"text": "hi <@U1>", "fields": [{"title": "Who", "value": "<@U1>"}], "blocks": [{"type": "divider"}]}]

        out = rewrite_attachments(raw, lambda value: value.replace("<@U1>", "<@U2>"), lambda blocks: blocks + blocks)

        assert out[0]["text"] == "hi <@U2>"
        assert out[0]["fields"][0]["value"] == "<@U2>"
        assert len(out[0]["blocks"]) == 2
        assert raw[0]["text"] == "hi <@U1>"
