"""Copy and flatten Slack Block Kit for message sync.

Events API ``text`` is a notification fallback. Long Block Kit posts (forms
and similar) often arrive with newlines stripped and the tail omitted. The
full body is in ``blocks``. ``Show more`` is a client chrome on those blocks,
not a second payload.

Interactive blocks (buttons, pickers) are dropped: they belong to the source
app and would be dead controls in the target. Tables are copied. A video from
another site becomes a link. A Slack-file image inside a block is dropped.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from urllib.parse import urlparse

from logger import log_debug, log_warning

_CONTENT_BLOCK_TYPES = frozenset(
    {
        "section",
        "header",
        "context",
        "divider",
        "image",
        "rich_text",
        "markdown",
        "video",
        "table",
    }
)
_SLACK_MAX_BLOCKS = 50
_SLACK_MAX_SECTION_TEXT = 3000
_SKIP_BLOCK_TYPES = frozenset(
    {
        "actions",
        "input",
        "file",
        "rich_text_input",
        "call",
    }
)
_INTERACTIVE_ACCESSORY = frozenset(
    {
        "button",
        "overflow",
        "datepicker",
        "timepicker",
        "datetimepicker",
        "static_select",
        "multi_static_select",
        "external_select",
        "multi_external_select",
        "users_select",
        "multi_users_select",
        "conversations_select",
        "multi_conversations_select",
        "channels_select",
        "multi_channels_select",
        "checkboxes",
        "radio_buttons",
        "workflow_button",
    }
)
_BODY_BLOCK_TYPES = frozenset({"section", "header", "rich_text", "context", "markdown"})
_SLACK_HOSTED_MEDIA_HOSTS = frozenset({"files.slack.com", "slack-files.com"})
_SLACK_FILE_ID = re.compile(r"^F[0-9A-Z]{8,}$")


def is_slack_hosted_media_url(url: object) -> bool:
    """True when a Block Kit media URL is a Slack-hosted file the target cannot play."""
    if not isinstance(url, str) or not url.strip():
        return False
    host = (urlparse(url).hostname or "").lower()
    if host in _SLACK_HOSTED_MEDIA_HOSTS:
        return True
    return any(host.endswith(f".{root}") for root in _SLACK_HOSTED_MEDIA_HOSTS)


def event_has_slack_hosted_media_blocks(event: dict | None) -> bool:
    """True when the event's Block Kit shows a Slack-hosted video or image."""
    for raw in get_event_layout_blocks(event or {}):
        if not isinstance(raw, dict):
            continue
        btype = raw.get("type")
        if btype == "image" and is_slack_hosted_media_url(raw.get("image_url")):
            return True
        if btype == "video" and is_slack_hosted_media_url(raw.get("video_url")):
            return True
        slack_file = raw.get("slack_file")
        if btype in {"image", "video", "file"} and isinstance(slack_file, dict) and slack_file.get("id"):
            return True
    return False


def get_event_layout_blocks(event: dict) -> list[dict]:
    """Return Block Kit from a message event, including ``message_changed``."""
    if not event:
        return []
    nested = event.get("message") if isinstance(event.get("message"), dict) else {}
    blocks = event.get("blocks") or nested.get("blocks") or []
    return blocks if isinstance(blocks, list) else []


def blocks_include_body(blocks: list[dict] | None) -> bool:
    """True when *blocks* already carry the message body (do not prepend ``text``)."""
    return any(isinstance(b, dict) and b.get("type") in _BODY_BLOCK_TYPES for b in blocks or [])


def build_content_blocks_for_sync(blocks: list[dict] | None) -> list[dict]:
    """Copy layout blocks that can be re-posted; drop interactivity and private files."""
    out: list[dict] = []
    for raw in blocks or []:
        if not isinstance(raw, dict):
            continue
        btype = raw.get("type")
        if btype in _SKIP_BLOCK_TYPES or btype not in _CONTENT_BLOCK_TYPES:
            continue
        if btype == "image" and (not raw.get("image_url") or is_slack_hosted_media_url(raw.get("image_url"))):
            continue
        if btype == "video":
            if not raw.get("video_url") or is_slack_hosted_media_url(raw.get("video_url")):
                continue
            link = _build_video_link_block(raw)
            if link:
                out.append(link)
            continue
        block = copy.deepcopy(raw)
        block.pop("block_id", None)
        accessory = block.get("accessory")
        if isinstance(accessory, dict) and (
            accessory.get("type") in _INTERACTIVE_ACCESSORY or _is_slack_file_image(accessory)
        ):
            block.pop("accessory", None)
        if btype == "context":
            elements = [
                el for el in block.get("elements") or [] if not (isinstance(el, dict) and _is_slack_file_image(el))
            ]
            if not elements:
                continue
            block["elements"] = elements
        out.append(block)
    if len(out) > _SLACK_MAX_BLOCKS:
        log_warning("content_blocks_trimmed", original=len(out), kept=_SLACK_MAX_BLOCKS)
        out = out[:_SLACK_MAX_BLOCKS]
    return out


def _is_slack_file_image(node: dict) -> bool:
    """True when an image element shows a Slack file the target cannot open."""
    if node.get("type") != "image":
        return False
    if isinstance(node.get("slack_file"), dict):
        return True
    return is_slack_hosted_media_url(node.get("image_url"))


def _build_video_link_block(block: dict) -> dict | None:
    """A video from another site, as a link. Slack posts a video block only for the app that owns the video's domain."""
    title_url = block.get("title_url")
    video_url = block.get("video_url")
    url = title_url if isinstance(title_url, str) and title_url.startswith("https://") else video_url
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    title = block.get("title")
    title_text = title.get("text") if isinstance(title, dict) else None
    text = title_text or block.get("alt_text") or url
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [{"type": "link", "url": url, "text": text}]}],
    }


def trim_target_blocks(blocks: list[dict] | None, *, limit: int = _SLACK_MAX_BLOCKS) -> list[dict]:
    """Cap target Block Kit at Slack's postMessage limit."""
    items = list(blocks or [])
    if len(items) <= limit:
        return items
    log_warning("target_blocks_trimmed", original=len(items), kept=limit)
    return items[:limit]


def clamp_section_text_in_blocks(blocks: list[dict] | None, *, limit: int = _SLACK_MAX_SECTION_TEXT) -> list[dict]:
    """Truncate section/header ``text.text`` fields that exceed Slack's limit."""
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        text_obj = block.get("text")
        if isinstance(text_obj, dict) and isinstance(text_obj.get("text"), str):
            raw = text_obj["text"]
            if len(raw) > limit:
                log_warning("section_text_clamped", original=len(raw), kept=limit, block_type=block.get("type"))
                text_obj["text"] = raw[: limit - 1] + "…"
    return blocks or []


def text_from_blocks(blocks: list[dict] | None) -> str:
    """Flatten content blocks to mrkdwn, preserving newlines between sections."""
    parts: list[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        chunk = _block_to_text(block)
        if chunk.strip():
            parts.append(chunk)
    return "\n".join(parts)


def choose_message_text(event_text: str | None, blocks: list[dict] | None) -> str:
    """Prefer Block Kit text when it is richer than Slack's ``event.text`` fallback."""
    fallback = (event_text or "").strip()
    from_blocks = text_from_blocks(blocks)
    if not from_blocks:
        return fallback or " "
    if not fallback:
        return from_blocks
    if "\n" in from_blocks and "\n" not in fallback:
        return from_blocks
    if len(from_blocks) > len(fallback):
        return from_blocks
    return fallback


def rewrite_content_blocks(
    blocks: list[dict],
    rewrite_mrkdwn: Callable[[str], str],
    map_user_id: Callable[[str], str | None],
    unmapped_user_label: Callable[[str], str],
) -> list[dict]:
    """Rewrite mentions inside copied blocks for the target workspace."""
    rewritten = [_rewrite_node(copy.deepcopy(b), rewrite_mrkdwn, map_user_id, unmapped_user_label) for b in blocks]
    clamp_section_text_in_blocks(rewritten)
    return rewritten


def collect_user_ids_from_blocks(blocks: list[dict] | None) -> list[str]:
    """User IDs from ``<@U…>`` mrkdwn and rich_text user elements."""
    ids: list[str] = []
    seen: set[str] = set()

    def add(uid: str | None) -> None:
        if uid and uid not in seen:
            seen.add(uid)
            ids.append(uid)

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "user":
                add(node.get("user_id"))
            text = node.get("text")
            if isinstance(text, str):
                for uid in re.findall(r"<@(\w+)>", text):
                    add(uid)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blocks)
    return ids


def _slack_file_id_from_url(url: object) -> str | None:
    """First Slack file id in a URL path, or None."""
    if not isinstance(url, str) or not url.strip():
        return None
    for segment in urlparse(url).path.split("/"):
        if not segment:
            continue
        for part in segment.split("-"):
            if _SLACK_FILE_ID.match(part):
                return part
    return None


def collect_slack_file_ids_from_blocks(blocks: list[dict] | None) -> list[str]:
    """Slack file ids that image blocks show. The caller reads each id with ``files.info``."""
    ids: list[str] = []
    seen: set[str] = set()
    for raw in blocks or []:
        if not isinstance(raw, dict) or raw.get("type") != "image":
            continue
        found: str | None = None
        slack_file = raw.get("slack_file")
        if isinstance(slack_file, dict):
            file_id = slack_file.get("id")
            if isinstance(file_id, str) and _SLACK_FILE_ID.match(file_id):
                found = file_id
            else:
                found = _slack_file_id_from_url(slack_file.get("url"))
        if not found and is_slack_hosted_media_url(raw.get("image_url")):
            found = _slack_file_id_from_url(raw.get("image_url"))
        if found and found not in seen:
            seen.add(found)
            ids.append(found)
    return ids


def _block_to_text(block: dict) -> str:
    btype = block.get("type")
    if btype in {"section", "header", "markdown"}:
        parts = [_text_obj_to_str(block.get("text"))]
        for field in block.get("fields") or []:
            parts.append(_text_obj_to_str(field))
        return "\n".join(p for p in parts if p)
    if btype == "context":
        bits = [_text_obj_to_str(el) for el in block.get("elements") or []]
        return " ".join(b for b in bits if b)
    if btype == "rich_text":
        return _rich_text_elements_to_str(block.get("elements") or [])
    if btype == "image":
        return block.get("alt_text") or block.get("title", {}).get("text") or ""
    if btype == "video":
        return block.get("title", {}).get("text") or block.get("alt_text") or ""
    return ""


def _text_obj_to_str(obj: object) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict) and (obj.get("type") in {"mrkdwn", "plain_text", "markdown"} or "text" in obj):
        return str(obj.get("text") or "")
    return ""


def _rich_text_elements_to_str(elements: list) -> str:
    parts: list[str] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        etype = el.get("type")
        if etype == "text":
            parts.append(el.get("text") or "")
        elif etype == "emoji":
            parts.append(_emoji_to_str(el))
        elif etype == "user":
            uid = el.get("user_id")
            parts.append(f"<@{uid}>" if uid else "")
        elif etype == "channel":
            cid = el.get("channel_id")
            parts.append(f"<#{cid}>" if cid else "")
        elif etype in {"link", "message_mention"}:
            url = el.get("url") or ""
            label = el.get("text") or url
            parts.append(f"<{url}|{label}>" if label and label != url else url)
        elif etype == "broadcast":
            parts.append(f"<!{el.get('range') or 'channel'}>")
        elif etype in {"rich_text_section", "rich_text_quote", "rich_text_preformatted"}:
            parts.append(_rich_text_elements_to_str(el.get("elements") or []))
        elif etype == "rich_text_list":
            items = el.get("elements") or []
            for item in items:
                line = _rich_text_elements_to_str(item.get("elements") or [] if isinstance(item, dict) else [])
                if line:
                    parts.append(f"• {line}")
            if parts and not parts[-1].endswith("\n"):
                parts.append("\n")
        elif "elements" in el:
            parts.append(_rich_text_elements_to_str(el.get("elements") or []))
    return "".join(parts)


_MRKDWN_LINK = re.compile(r"^<([^<>|]+)\|([^>]+)>$")
_MRKDWN_LINK_SPAN = re.compile(r"<([^<>|\s]+)\|([^<>]+)>")
_MRKDWN_CODE = re.compile(r"^`([^`]+)`$")


def is_url_display_text(display: str, url: str) -> bool:
    """True when link text is Slack's URL rendering, not author-chosen words."""
    shown = display.strip().rstrip("…").rstrip("/")
    if not shown:
        return True
    bare = url.strip().removeprefix("https://").removeprefix("http://")
    shown = shown.removeprefix("https://").removeprefix("http://")
    return ("/" in shown or "." in shown) and bare.lower().startswith(shown.lower())


def _text_element(text: str, style: dict | None) -> dict:
    out: dict = {"type": "text", "text": text}
    if style:
        out["style"] = style
    return out


def _link_element(url: str, text: str, style: dict | None) -> dict:
    out: dict = {"type": "link", "url": url, "text": text}
    if style:
        out["style"] = style
    return out


def _rewrite_link(
    url: str,
    display: str,
    rewrite_mrkdwn: Callable[[str], str],
    style: dict | None,
) -> dict | None:
    """Rebuild a rich_text ``link`` or ``message_mention`` as a target ``link``."""
    labeled = _MRKDWN_LINK.fullmatch((display or "").strip())
    if labeled:
        url = url or labeled.group(1)
        display = labeled.group(2)
    if not url:
        return None
    if is_url_display_text(display, url):
        rewritten = rewrite_mrkdwn(url)
        if rewritten != url:
            return _rich_text_from_mrkdwn_span(rewritten, style=style)
    return _link_element(url, display or url, style)


def _rich_text_from_mixed_text(text: str, style: dict | None = None) -> dict | list[dict]:
    """Split ``<url|label>`` mrkdwn out of a rich_text text node into real link elements."""
    parts: list[dict] = []
    pos = 0
    for match in _MRKDWN_LINK_SPAN.finditer(text):
        if match.start() > pos:
            parts.append(_text_element(text[pos : match.start()], style))
        parts.append(_link_element(match.group(1), match.group(2), style))
        pos = match.end()
    if not parts:
        return _text_element(text, style)
    if pos < len(text):
        parts.append(_text_element(text[pos:], style))
    return parts


def _rich_text_from_mrkdwn_span(mrkdwn: str, *, style: dict | None = None) -> dict:
    """Turn ``resolve_channel_references`` output into a rich_text ``link`` or code-ticked text."""
    s = (mrkdwn or "").strip()
    m = _MRKDWN_LINK.fullmatch(s)
    if m:
        return _link_element(m.group(1), m.group(2), style)
    code = _MRKDWN_CODE.fullmatch(s)
    if code:
        merged = dict(style or {})
        merged["code"] = True
        return {"type": "text", "text": code.group(1), "style": merged}
    return _text_element(mrkdwn, style)


def _emoji_to_str(el: dict) -> str:
    name = el.get("name") or ""
    uni = el.get("unicode")
    if isinstance(uni, str) and uni:
        try:
            return "".join(chr(int(part, 16)) for part in uni.replace(" ", "-").split("-") if part)
        except ValueError:
            log_debug("emoji_unicode_parse_failed", unicode=uni, emoji_name=name)
    return f":{name}:" if name else ""


def _rewrite_node(
    node: object,
    rewrite_mrkdwn: Callable[[str], str],
    map_user_id: Callable[[str], str | None],
    unmapped_user_label: Callable[[str], str],
) -> object:
    if isinstance(node, list):
        out_list: list = []
        for item in node:
            rewritten_item = _rewrite_node(item, rewrite_mrkdwn, map_user_id, unmapped_user_label)
            if isinstance(rewritten_item, list):
                out_list.extend(rewritten_item)
            else:
                out_list.append(rewritten_item)
        return out_list
    if not isinstance(node, dict):
        return node
    if node.get("type") == "user" and node.get("user_id"):
        mapped = map_user_id(node["user_id"])
        if mapped:
            out = dict(node)
            out["user_id"] = mapped
            return out
        return _rich_text_from_mrkdwn_span(
            unmapped_user_label(node["user_id"]),
            style=node.get("style") if isinstance(node.get("style"), dict) else None,
        )
    if node.get("type") == "channel" and node.get("channel_id"):
        return _rich_text_from_mrkdwn_span(
            rewrite_mrkdwn(f"<#{node['channel_id']}>"),
            style=node.get("style") if isinstance(node.get("style"), dict) else None,
        )
    style = node.get("style") if isinstance(node.get("style"), dict) else None
    if node.get("type") in {"link", "message_mention"}:
        url = node["url"] if isinstance(node.get("url"), str) else ""
        display = node["text"] if isinstance(node.get("text"), str) else ""
        rewritten = _rewrite_link(url, display, rewrite_mrkdwn, style)
        if rewritten:
            return rewritten
        cid = node.get("channel_id")
        if node.get("type") == "message_mention" and isinstance(cid, str) and cid:
            return _rich_text_from_mrkdwn_span(rewrite_mrkdwn(f"<#{cid}>"), style=style)
    if node.get("type") == "text" and isinstance(node.get("text"), str):
        return _rich_text_from_mixed_text(rewrite_mrkdwn(node["text"]), style=style)
    out: dict = {}
    for key, val in node.items():
        if key == "text" and isinstance(val, str):
            out[key] = rewrite_mrkdwn(val)
        else:
            out[key] = _rewrite_node(val, rewrite_mrkdwn, map_user_id, unmapped_user_label)
    return out


_SHARE_FLAGS = ("is_share", "is_msg_unfurl", "is_reply_unfurl", "is_thread_root_unfurl")
_ATTACHMENT_KEYS = (
    "fallback",
    "color",
    "pretext",
    "author_name",
    "author_link",
    "author_icon",
    "title",
    "title_link",
    "text",
    "fields",
    "image_url",
    "thumb_url",
    "footer",
    "footer_icon",
    "ts",
    "mrkdwn_in",
    "blocks",
)
_ATTACHMENT_TEXT_KEYS = ("pretext", "author_name", "title", "text", "footer")
_BROADCAST = re.compile(r"<!((?:here|channel|everyone))(?:\|[^>]*)?>", re.IGNORECASE)


def event_attachments(event: dict | None) -> list[dict]:
    """Attachments on a message event, including ``message_changed``."""
    if not isinstance(event, dict):
        return []
    nested = event.get("message") if isinstance(event.get("message"), dict) else {}
    raw = event.get("attachments") or nested.get("attachments") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def attachment_is_share(attachment: dict | None) -> bool:
    """True for a forward or a Slack message preview Slack already delivered."""
    if not isinstance(attachment, dict):
        return False
    return any(attachment.get(flag) for flag in _SHARE_FLAGS)


def attachment_is_image_only(attachment: dict | None) -> bool:
    """GIF picker and Giphy: a public image and no other attachment body."""
    if not isinstance(attachment, dict):
        return False
    if any(
        attachment.get(key) for key in ("text", "pretext", "title", "author_name", "fields", "from_url", "original_url")
    ):
        return False
    if attachment.get("image_url"):
        return True
    blocks = attachment.get("blocks") or []
    return bool(blocks) and all(isinstance(block, dict) and block.get("type") == "image" for block in blocks)


def _https_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    stripped = url.strip()
    if not stripped.startswith("https://"):
        return None
    if stripped.startswith("https://app.slack.com/client") or "slack://" in stripped:
        return None
    if any(char in stripped for char in (" ", "\t", "\n", "<", ">", "|")):
        return None
    return stripped


def _neutralize_broadcasts(value: str) -> str:
    return _BROADCAST.sub(lambda match: f"@{match.group(1).lower()}", value)


def _neutralize_block_broadcasts(node: object) -> object:
    if isinstance(node, list):
        return [_neutralize_block_broadcasts(item) for item in node]
    if not isinstance(node, dict):
        return node
    if node.get("type") == "broadcast":
        name = str(node.get("range") or "channel")
        return {"type": "text", "text": f"@{name}"}
    return {key: _neutralize_block_broadcasts(val) for key, val in node.items()}


def _share_body_blocks(attachment: dict) -> list[dict]:
    blocks: list[dict] = []
    for item in attachment.get("message_blocks") or []:
        message = item.get("message") if isinstance(item, dict) else None
        nested = message.get("blocks") if isinstance(message, dict) else None
        if isinstance(nested, list):
            blocks.extend(block for block in nested if isinstance(block, dict))
    return build_content_blocks_for_sync(blocks)


def _copy_fields(raw_fields: object, *, neutralize: bool) -> list[dict]:
    if not isinstance(raw_fields, list):
        return []
    fields: list[dict] = []
    for field in raw_fields:
        if not isinstance(field, dict):
            continue
        copied: dict = {}
        title = field.get("title")
        value = field.get("value")
        if isinstance(title, str) and title:
            copied["title"] = _neutralize_broadcasts(title) if neutralize else title
        if isinstance(value, str) and value:
            copied["value"] = _neutralize_broadcasts(value) if neutralize else value
        if "short" in field:
            copied["short"] = bool(field.get("short"))
        if copied:
            fields.append(copied)
    return fields


def build_attachments_for_sync(attachments: list[dict] | None) -> list[dict]:
    """Attachments safe to post. Image-only attachments stay on the images path.

    A forward's ``author_link`` is its source permalink. ``<!here>`` inside a
    forward or message preview becomes plain ``@here`` so the target Channel
    is not pinged. A bot attachment keeps Slack broadcasts.
    """
    out: list[dict] = []
    for raw in attachments or []:
        if not isinstance(raw, dict) or attachment_is_image_only(raw):
            continue
        share = attachment_is_share(raw)
        link_preview = not share and bool(
            (raw.get("original_url") or raw.get("from_url")) and (raw.get("service_name") or raw.get("is_app_unfurl"))
        )
        copied: dict = {}
        for key in _ATTACHMENT_KEYS:
            if key in raw and key not in {
                "fields",
                "blocks",
                "author_link",
                "title_link",
                "image_url",
                "thumb_url",
                "text",
            }:
                copied[key] = raw.get(key)
        if share:
            words = choose_message_text(
                raw.get("text") if isinstance(raw.get("text"), str) else "",
                _share_body_blocks(raw),
            ).strip()
            if words:
                copied["text"] = _neutralize_broadcasts(words)
                copied["mrkdwn_in"] = ["text"]
            author_link = _https_url(raw.get("from_url"))
            if author_link:
                copied["author_link"] = author_link
        else:
            text = raw.get("text")
            if isinstance(text, str) and text:
                copied["text"] = text
            author_link = _https_url(raw.get("author_link"))
            if author_link:
                copied["author_link"] = author_link
        title_link = _https_url(raw.get("title_link"))
        if title_link:
            copied["title_link"] = title_link
        for image_key in ("image_url", "thumb_url"):
            url = _https_url(raw.get(image_key))
            if url and not is_slack_hosted_media_url(url):
                copied[image_key] = url
        fields = _copy_fields(raw.get("fields"), neutralize=share)
        if fields:
            copied["fields"] = fields
        blocks = build_content_blocks_for_sync(raw.get("blocks") if isinstance(raw.get("blocks"), list) else [])
        if share and blocks:
            blocks = _neutralize_block_broadcasts(blocks)
        if blocks:
            copied["blocks"] = blocks
        if link_preview:
            service = raw.get("service_name")
            icon = raw.get("service_icon")
            if isinstance(service, str) and service:
                copied["author_name"] = service
            if isinstance(icon, str) and _https_url(icon):
                copied["author_icon"] = icon
        if share:
            for key in _ATTACHMENT_TEXT_KEYS:
                value = copied.get(key)
                if isinstance(value, str):
                    copied[key] = _neutralize_broadcasts(value)
        cleaned = {key: value for key, value in copied.items() if value not in (None, "", [])}
        if cleaned:
            out.append(cleaned)
    return out


def text_from_attachments(attachments: list[dict] | None) -> str:
    """Mrkdwn fallback for attachments. Shares and previews do not ping.

    Built attachments are already neutralized. A raw share still is, so the
    words can be read before ``build_attachments_for_sync``.
    """
    parts: list[str] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        neutralize = attachment_is_share(attachment)

        def shown(value: object, plain: bool = neutralize) -> str:
            text = value if isinstance(value, str) else ""
            if plain and text:
                return _neutralize_broadcasts(text)
            return text

        lines: list[str] = []
        author = shown(attachment.get("author_name"))
        author_link = _https_url(attachment.get("author_link")) or (
            _https_url(attachment.get("from_url")) if attachment.get("is_share") else None
        )
        if author:
            lines.append(f"<{author_link}|{author}>" if author_link else author)
        pretext = shown(attachment.get("pretext")).strip()
        if pretext:
            lines.append(pretext)
        title = shown(attachment.get("title"))
        title_link = _https_url(attachment.get("title_link"))
        if title:
            lines.append(f"<{title_link}|{title}>" if title_link else title)
        body = shown(attachment.get("text")).strip()
        if body:
            lines.append(body)
        for field in attachment.get("fields") or []:
            if not isinstance(field, dict):
                continue
            label = shown(field.get("title"))
            value = shown(field.get("value"))
            joined = f"{label}: {value}".strip(": ").strip()
            if joined:
                lines.append(joined)
        footer = shown(attachment.get("footer")).strip()
        if footer:
            lines.append(footer)
        if lines:
            parts.append("\n".join(lines))
    return "\n".join(parts)


def share_permalinks(attachments: list[dict] | None) -> list[str]:
    """Source permalinks for forwards. A message preview's link is already in the text."""
    urls: list[str] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict) or not attachment.get("is_share"):
            continue
        url = _https_url(attachment.get("from_url"))
        if url and url not in urls:
            urls.append(url)
    return urls


def share_files(attachments: list[dict] | None) -> list[dict]:
    """Files Slack attached to a forward. The peer never downloads these itself."""
    files: list[dict] = []
    seen: set[str] = set()
    for attachment in attachments or []:
        if not isinstance(attachment, dict) or not attachment.get("is_share"):
            continue
        for item in attachment.get("files") or []:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("id") or "")
            if file_id and file_id in seen:
                continue
            if file_id:
                seen.add(file_id)
            files.append(item)
    return files


def compose_message_text(comment: str | None, permalinks: list[str] | None, attachment_words: str | None) -> str:
    """Comment, source permalinks, then attachment words for peers that drop the card."""
    base = (comment or "").strip()
    lines = [base] if base else []
    for url in permalinks or []:
        if url and url not in base:
            lines.append(url)
    words = (attachment_words or "").strip()
    if words and words not in base:
        lines.append(words)
    return "\n".join(lines)


def text_beside_attachments(full_text: str | None, attachment_words: str | None) -> str:
    """Text to show above a card. The card already carries *attachment_words*."""
    full = full_text or ""
    words = (attachment_words or "").strip()
    if words and full.rstrip().endswith(words):
        return full.rstrip()[: -len(words)].rstrip()
    return full


def rewrite_attachment_strings(attachments: list[dict] | None, rewrite_mrkdwn: Callable[[str], str]) -> list[dict]:
    """Run the message-body rewriter over attachment text. Drops nothing else."""
    rewritten: list[dict] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        copied = dict(attachment)
        for key in _ATTACHMENT_TEXT_KEYS:
            value = copied.get(key)
            if isinstance(value, str) and value:
                copied[key] = rewrite_mrkdwn(value)
        fields = []
        for field in copied.get("fields") or []:
            if not isinstance(field, dict):
                continue
            updated = dict(field)
            for key in ("title", "value"):
                value = updated.get(key)
                if isinstance(value, str) and value:
                    updated[key] = rewrite_mrkdwn(value)
            fields.append(updated)
        if fields:
            copied["fields"] = fields
        rewritten.append(copied)
    return rewritten
