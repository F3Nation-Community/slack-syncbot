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
from datetime import UTC, datetime
from decimal import Decimal
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
# Input fields of an image block. Slack's events also carry output-only sizes and flags.
_IMAGE_BLOCK_KEYS = ("type", "image_url", "slack_file", "alt_text", "title")
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
        if btype == "image":
            if raw.get("image_url") and not is_slack_hosted_media_url(raw.get("image_url")):
                out.append(_build_image_block_input(raw))
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


def _build_image_block_input(raw: dict) -> dict:
    """An image block with only the keys Slack accepts. Events add ``image_bytes`` and similar."""
    block = {key: copy.deepcopy(raw[key]) for key in _IMAGE_BLOCK_KEYS if key in raw}
    alt_text = block.get("alt_text")
    if not isinstance(alt_text, str) or not alt_text.strip():
        block["alt_text"] = "image"
    return block


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
_ATTACHMENT_TEXT_KEYS = ("pretext", "author_name", "title", "text", "footer")
_BROADCAST = re.compile(r"<!((?:here|channel|everyone))(?:\|[^>]*)?>", re.IGNORECASE)
_PUBLIC_CHANNEL_ID = re.compile(r"^C[A-Z0-9]+$")
_MESSAGE_LINK_KEY = re.compile(r"/archives/([CDG][A-Z0-9]+)/p(\d+)")
_MRKDWN_URL = re.compile(r"<(https://[^|>\s]+)(?:\|([^>]*))?>")


def get_event_attachments(event: dict | None) -> list[dict]:
    """Attachments on a message event, including ``message_changed``."""
    if not isinstance(event, dict):
        return []
    nested = event.get("message") if isinstance(event.get("message"), dict) else {}
    raw = event.get("attachments") or nested.get("attachments") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def is_share_attachment(attachment: dict | None) -> bool:
    """True for a forward or a Slack message preview Slack already delivered."""
    if not isinstance(attachment, dict):
        return False
    return any(attachment.get(flag) for flag in _SHARE_FLAGS)


def is_image_only_attachment(attachment: dict | None) -> bool:
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


def get_safe_https_url(url: object) -> str | None:
    """An ``https`` URL that is safe inside ``<url|label>`` mrkdwn, and not a Slack client link."""
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


def _get_public_image_url(url: object) -> str | None:
    """An https image another workspace can load. Slack-hosted files cannot be."""
    found = get_safe_https_url(url)
    return found if found and not is_slack_hosted_media_url(found) else None


def _parse_attachment_ts(value: object) -> int | None:
    """Legacy ``ts`` is an integer epoch. Slack sends a string such as ``"1733497200.423469"``."""
    try:
        seconds = int(Decimal(str(value)))
    except (ArithmeticError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _escape_mrkdwn(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _neutralize_broadcasts(value: str) -> str:
    return _BROADCAST.sub(lambda match: f"@{match.group(1).lower()}", value)


def _neutralize_block_broadcasts(node: object) -> object:
    """Plain ``@here`` for broadcast elements and mrkdwn ``<!here>`` inside copied blocks."""
    if isinstance(node, list):
        return [_neutralize_block_broadcasts(item) for item in node]
    if not isinstance(node, dict):
        return node
    if node.get("type") == "broadcast":
        name = str(node.get("range") or "channel")
        return {"type": "text", "text": f"@{name}"}
    out: dict = {}
    for key, val in node.items():
        if key == "text" and isinstance(val, str) and node.get("type") in {"mrkdwn", "plain_text", "markdown"}:
            out[key] = _neutralize_broadcasts(val)
        else:
            out[key] = _neutralize_block_broadcasts(val)
    return out


def _build_share_body_blocks(attachment: dict) -> list[dict]:
    """The forwarded message's Block Kit: ``blocks`` on the share, else ``message_blocks``."""
    raw = attachment.get("blocks")
    blocks: list[dict] = [block for block in raw if isinstance(block, dict)] if isinstance(raw, list) else []
    if not blocks:
        for item in attachment.get("message_blocks") or []:
            message = item.get("message") if isinstance(item, dict) else None
            nested = message.get("blocks") if isinstance(message, dict) else None
            if isinstance(nested, list):
                blocks.extend(block for block in nested if isinstance(block, dict))
    return build_content_blocks_for_sync(blocks)


def _build_share_header_blocks(attachment: dict) -> list[dict]:
    """Who posted it, when, and where, as the client draws above a forward."""
    name = next(
        (
            value.strip()
            for value in (attachment.get("author_subname"), attachment.get("author_name"))
            if isinstance(value, str) and value.strip()
        ),
        "",
    )
    link = get_safe_https_url(attachment.get("from_url"))
    bits: list[str] = []
    if name:
        shown = _escape_mrkdwn(_neutralize_broadcasts(name))
        bits.append(f"*<{link}|{shown}>*" if link else f"*{shown}*")
    seconds = _parse_attachment_ts(attachment.get("ts"))
    if seconds:
        when = datetime.fromtimestamp(seconds, UTC)
        bits.append(f"<!date^{seconds}^{{date_short_pretty}}|{when:%b} {when.day}, {when.year}>")
    blocks: list[dict] = []
    if bits:
        elements: list[dict] = []
        icon = _get_public_image_url(attachment.get("author_icon"))
        if icon:
            elements.append({"type": "image", "image_url": icon, "alt_text": name or "author"})
        elements.append({"type": "mrkdwn", "text": "  ".join(bits)})
        blocks.append({"type": "context", "elements": elements})
    channel_id = attachment.get("channel_id")
    if isinstance(channel_id, str) and _PUBLIC_CHANNEL_ID.match(channel_id):
        # The client's words: a reply (``is_reply_unfurl``) is "From a thread in", anything else "Posted in".
        where = "From a thread in" if attachment.get("is_reply_unfurl") else "Posted in"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"{where} <#{channel_id}>"}]})
    return blocks


def _build_share_card(attachment: dict) -> dict | None:
    """A forward or message preview as ``color``, ``fallback``, and ``blocks``.

    The client's rounded forward card cannot be posted. The rebuilt card has the
    same header, body, and Channel line. Legacy display fields are left out, so
    Slack has nothing to draw beside the blocks.
    """
    body = _build_share_body_blocks(attachment)
    words = attachment.get("text") if isinstance(attachment.get("text"), str) else ""
    if words.strip() and not blocks_include_body(body):
        body = [{"type": "section", "text": {"type": "mrkdwn", "text": words.strip()}}, *body]
    blocks = _neutralize_block_broadcasts(_build_share_header_blocks(attachment) + body)
    clamp_section_text_in_blocks(blocks)
    blocks = trim_target_blocks(blocks)
    if not blocks:
        return None
    card: dict = {"blocks": blocks}
    color = attachment.get("color")
    if isinstance(color, str) and color:
        card["color"] = color
    fallback = attachment.get("fallback")
    if not (isinstance(fallback, str) and fallback.strip()):
        fallback = text_from_blocks(body)
    if fallback.strip():
        card["fallback"] = _neutralize_broadcasts(fallback.strip())
    return card


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


def _build_legacy_attachment(raw: dict) -> dict | None:
    """An app or webhook attachment, or a link preview, with only documented fields.

    A link preview shows ``service_name`` as its author. Its ``@here`` is made plain;
    an app's own attachment keeps it, the same as the app's text.
    """
    link_preview = bool(
        (raw.get("original_url") or raw.get("from_url")) and (raw.get("service_name") or raw.get("is_app_unfurl"))
    )
    copied: dict = {
        key: raw.get(key)
        for key in ("fallback", "color", "pretext", "author_name", "title", "text", "footer", "mrkdwn_in")
        if key in raw
    }
    for key in ("author_link", "title_link"):
        url = get_safe_https_url(raw.get(key))
        if url:
            copied[key] = url
    for key in ("image_url", "thumb_url", "author_icon", "footer_icon"):
        url = _get_public_image_url(raw.get(key))
        if url:
            copied[key] = url
    seconds = _parse_attachment_ts(raw.get("ts"))
    if seconds:
        copied["ts"] = seconds
    fields = _copy_fields(raw.get("fields"), neutralize=link_preview)
    if fields:
        copied["fields"] = fields
    blocks = build_content_blocks_for_sync(raw.get("blocks") if isinstance(raw.get("blocks"), list) else [])
    if blocks:
        copied["blocks"] = _neutralize_block_broadcasts(blocks) if link_preview else blocks
    if link_preview:
        service = raw.get("service_name")
        if isinstance(service, str) and service:
            copied["author_name"] = service
        icon = _get_public_image_url(raw.get("service_icon"))
        if icon:
            copied["author_icon"] = icon
        for key in _ATTACHMENT_TEXT_KEYS:
            if isinstance(copied.get(key), str):
                copied[key] = _neutralize_broadcasts(copied[key])
    cleaned = {key: value for key, value in copied.items() if value not in (None, "", [])}
    return cleaned or None


def build_attachments_for_sync(attachments: list[dict] | None) -> list[dict]:
    """Attachments a copy can post. Image-only attachments stay on the images path.

    A forward or message preview is rebuilt as a card (``_build_share_card``).
    Other attachments keep Slack's documented fields.
    """
    out: list[dict] = []
    for raw in attachments or []:
        if not isinstance(raw, dict) or is_image_only_attachment(raw):
            continue
        built = _build_share_card(raw) if is_share_attachment(raw) else _build_legacy_attachment(raw)
        if built:
            out.append(built)
    return out


def text_from_attachments(attachments: list[dict] | None) -> str:
    """Mrkdwn for attachments built by ``build_attachments_for_sync``.

    Text-only fallbacks use it: a refused card, a file share, and mention lookup.
    """
    parts: list[str] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        lines: list[str] = []
        author = attachment.get("author_name") if isinstance(attachment.get("author_name"), str) else ""
        author_link = get_safe_https_url(attachment.get("author_link"))
        if author:
            lines.append(f"<{author_link}|{author}>" if author_link else author)
        pretext = attachment.get("pretext")
        if isinstance(pretext, str) and pretext.strip():
            lines.append(pretext.strip())
        title = attachment.get("title") if isinstance(attachment.get("title"), str) else ""
        title_link = get_safe_https_url(attachment.get("title_link"))
        if title:
            lines.append(f"<{title_link}|{title}>" if title_link else title)
        body = attachment.get("text")
        if isinstance(body, str) and body.strip():
            lines.append(body.strip())
        for field in attachment.get("fields") or []:
            if not isinstance(field, dict):
                continue
            label = field.get("title") if isinstance(field.get("title"), str) else ""
            value = field.get("value") if isinstance(field.get("value"), str) else ""
            joined = f"{label}: {value}".strip(": ").strip()
            if joined:
                lines.append(joined)
        block_words = text_from_blocks(attachment.get("blocks") if isinstance(attachment.get("blocks"), list) else [])
        if block_words.strip():
            lines.append(block_words.strip())
        footer = attachment.get("footer")
        if isinstance(footer, str) and footer.strip():
            lines.append(footer.strip())
        if lines:
            parts.append("\n".join(lines))
    return "\n".join(parts)


def append_link_lines(
    text: str | None,
    blocks: list | None,
    urls: list[str] | None,
) -> tuple[str | None, list | None]:
    """Append each URL on its own line. Skip one already in *text*.

    When *blocks* carry the body, the links also go in a ``rich_text`` block,
    because Slack shows the blocks and not the text. The target rewrite labels
    a message permalink ``message in #channel (Workspace)``.
    """
    source = text or ""
    links = [url for url in dict.fromkeys(urls or []) if url and url not in source]
    if not links:
        return text, blocks
    extra = "\n".join(f"<{url}>" for url in links)
    new_text = extra if not source.strip() else f"{source.rstrip()}\n{extra}"
    new_blocks = list(blocks or [])
    if blocks_include_body(new_blocks):
        elements: list[dict] = []
        for index, url in enumerate(links):
            if index:
                elements.append({"type": "text", "text": "\n"})
            elements.append({"type": "link", "url": url})
        new_blocks.append({"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": elements}]})
    return new_text, new_blocks


NOTE_COPY_DIFFERS = "This synced message may not look the same as the original."


def add_copy_note(text: str | None, blocks: list | None) -> tuple[str | None, list | None]:
    """Say on a copy that SyncBot could not copy it all the way (a fail-open copy).

    A ``context`` block when *blocks* carry the body, else an italic last line of
    the text. Added once.
    """
    note_line = f"_{NOTE_COPY_DIFFERS}_"
    source = text or ""
    if note_line in source:
        return text, blocks
    new_text = f"{source.rstrip()}\n{note_line}" if source.strip() else note_line
    new_blocks = list(blocks or [])
    if blocks_include_body(new_blocks):
        note = {"type": "context", "elements": [{"type": "mrkdwn", "text": NOTE_COPY_DIFFERS}]}
        new_blocks = [*new_blocks[: _SLACK_MAX_BLOCKS - 1], note]
    return new_text, new_blocks


def get_share_permalinks(attachments: list[dict] | None) -> list[str]:
    """Source permalinks of forwards. A preview's link is already in the message."""
    urls: list[str] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict) or not attachment.get("is_share"):
            continue
        url = get_safe_https_url(attachment.get("from_url"))
        if url and url not in urls:
            urls.append(url)
    return urls


def _get_message_link_key(url: object) -> tuple[str, str] | None:
    """Channel and message of a Slack permalink, ignoring host and query."""
    match = _MESSAGE_LINK_KEY.search(url) if isinstance(url, str) else None
    return (match.group(1), match.group(2)) if match else None


def _find_message_link_labels(text: str | None, blocks: list | None) -> list[tuple[tuple[str, str], bool]]:
    """Each message link in the source, and whether the author gave it their own words."""
    found: list[tuple[tuple[str, str], bool]] = []
    for match in _MRKDWN_URL.finditer(text or ""):
        key = _get_message_link_key(match.group(1))
        if key:
            label = match.group(2) or ""
            found.append((key, not is_url_display_text(label, match.group(1))))

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") in {"link", "message_mention"} and isinstance(node.get("url"), str):
            key = _get_message_link_key(node["url"])
            if key:
                label = node.get("text") if isinstance(node.get("text"), str) else ""
                found.append((key, not is_url_display_text(label, node["url"])))
        for value in node.values():
            walk(value)

    walk(blocks)
    return found


def drop_previews_of_labeled_links(
    attachments: list[dict] | None,
    text: str | None,
    blocks: list | None,
) -> list[dict]:
    """Leave out a message preview of a link the author wrote their own words for.

    That link is copied as written (words and source URL), with no card. A link
    that also appears bare keeps its card. A forward always keeps its card.
    """
    labels = _find_message_link_labels(text, blocks)
    worded = {key for key, own_words in labels if own_words}
    bare = {key for key, own_words in labels if not own_words}
    kept: list[dict] = []
    for attachment in attachments or []:
        if isinstance(attachment, dict) and attachment.get("is_msg_unfurl") and not attachment.get("is_share"):
            key = _get_message_link_key(attachment.get("from_url") or attachment.get("original_url"))
            if key and key in worded and key not in bare:
                continue
        kept.append(attachment)
    return kept


def get_share_files(attachments: list[dict] | None) -> list[dict]:
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


def rewrite_attachments(
    attachments: list[dict] | None,
    rewrite_mrkdwn: Callable[[str], str],
    rewrite_blocks: Callable[[list[dict]], list[dict]] | None = None,
) -> list[dict]:
    """Run the message-body rewrites over attachment text, fields, and blocks."""
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
        nested = copied.get("blocks")
        if rewrite_blocks and isinstance(nested, list) and nested:
            copied["blocks"] = rewrite_blocks(nested)
        rewritten.append(copied)
    return rewritten
