"""UTF-8 log pages with byte cursors and redaction across page boundaries."""

import base64
import binascii

from ..core.errors import ToolError

TERMINAL_STATES = frozenset({"finished", "failed", "cancelled", "timed_out", "deleted"})


def log_page(data, secrets, offset, limit):
    data = dict(data)
    try:
        raw = base64.b64decode(data.pop("data_b64"), validate=True)
        base = data.pop("window_offset")
        size = data["log_size"]
        if type(base) is not int or type(size) is not int or not 0 <= base <= size:
            raise ValueError()
        if base + len(raw) > size or base > min(offset, size):
            raise ValueError()
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise ToolError("远端日志分页响应无效。") from None
    encoded = [secret.encode("utf-8") for secret in secrets]
    tail = max((len(secret) - 1 for secret in encoded), default=0)
    terminal = data.get("state") in TERMINAL_STATES
    # Do not reveal a possible password prefix before the rest has been written.
    safe_end = size if terminal else max(0, size - tail)
    start = min(max(offset, base), size)
    end = max(start, min(offset + limit, safe_end, base + len(raw)))

    def continuation(position):
        return (
            base <= position < base + len(raw) and raw[position - base] & 0xC0 == 0x80
        )

    # A caller may explicitly seek into a character. Skip its continuation bytes.
    while start < end and continuation(start):
        start += 1
    extended = end
    while extended < min(size, base + len(raw)) and continuation(extended):
        extended += 1
    if extended <= safe_end:
        end = extended
    else:
        while end > start and continuation(end):
            end -= 1
    # A running producer may have written only the beginning of its last codepoint.
    if not terminal and end == size and end > start:
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        decoder.decode(raw[start - base : end - base], final=False)
        end -= len(decoder.getstate()[0])

    spans = []
    for secret in encoded:
        index = raw.find(secret)
        while index >= 0:
            spans.append((base + index, base + index + len(secret)))
            index = raw.find(secret, index + 1)
        # A truncated stored log may end halfway through a password. Conceal even
        # that suffix; a terminal state cannot make the missing bytes available.
        if base + len(raw) == size:
            for width in range(1, min(len(secret), len(raw) + 1)):
                if raw.endswith(secret[:width]):
                    spans.append((size - width, size))
    parts = []
    cursor = start
    for left, right in sorted(spans):
        left, right = max(left, start), min(right, end)
        if right <= cursor or left >= right:
            continue
        parts.append(raw[cursor - base : max(cursor, left) - base])
        parts.append(b"[REDACTED]")
        cursor = right
    parts.append(raw[cursor - base : end - base])
    data.update(
        text=b"".join(parts).decode("utf-8", errors="replace"),
        offset=offset,
        next_offset=max(offset, end),
    )
    return data
