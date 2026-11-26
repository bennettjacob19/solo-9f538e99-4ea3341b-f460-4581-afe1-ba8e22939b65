"""IPv4 / port parsing and validation helpers.

All addresses inside the application are canonical strings.  Ports are
canonical strings too (numeric ports lose leading zeroes), and forwarding
comparisons are always done as *sets*, so ordering never matters.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Iterable

PORT_SPLIT_RE = re.compile(r"[,，;；\s/]+")
PORT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")

MAX_ROWS = 500
MAX_FRAMES = 2**63 - 1


class ValidationError(ValueError):
    """Structured validation error.

    ``issues`` is a list of dicts: {"scope", "row", "field", "message"}.
    """

    def __init__(self, message: str = "输入校验失败", issues: list[dict] | None = None):
        super().__init__(message)
        self.message = message
        self.issues: list[dict] = issues or []

    def to_dict(self) -> dict:
        return {"message": self.message, "issues": self.issues}


def parse_ipv4(value, field: str = "address") -> str:
    if value is None:
        raise ValueError(f"{field} 不能为空")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} 不能为空")
    try:
        addr = ipaddress.IPv4Address(text)
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise ValueError(f"{field} 不是合法的 IPv4 地址: {text!r}") from exc
    return str(addr)


def is_multicast(group: str) -> bool:
    try:
        return ipaddress.IPv4Address(group) in ipaddress.IPv4Network("224.0.0.0/4")
    except ValueError:
        return False


def parse_source(value) -> str | None:
    """Return None for '*' / empty (any source), canonical IP otherwise."""
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text == "*" or text.lower() == "any":
        return None
    return parse_ipv4(text, "源地址")


def normalize_port(token) -> str:
    text = str(token).strip()
    if not text:
        raise ValueError("空端口")
    if text.isdigit():
        # Canonicalise e.g. "003" -> "3"
        text = str(int(text))
    if not PORT_TOKEN_RE.match(text):
        raise ValueError(f"非法端口名: {token!r}")
    return text


def parse_ports(value) -> list[str]:
    """Parse a port list from a list/tuple or a delimited string.

    Returns a de-duplicated list in a stable natural order.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        raw_tokens: list[str] = [str(v) for v in value]
    else:
        raw_tokens = PORT_SPLIT_RE.split(str(value))
    ports: set[str] = set()
    for raw in raw_tokens:
        token = raw.strip()
        if not token:
            continue
        ports.add(normalize_port(token))
    return sorted(ports, key=port_sort_key)


def port_sort_key(name: str):
    """Natural sort: embedded integers compare numerically."""
    parts = re.split(r"(\d+)", name)
    key = []
    for part in parts:
        if part.isdigit():
            key.append((1, int(part)))
        else:
            key.append((0, part.lower()))
    return key


def parse_nonneg_int(value, field: str, *, default: int | None = None) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is not None:
            return default
        raise ValueError(f"{field} 不能为空")
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是整数")
    try:
        n = int(str(value).strip()) if not isinstance(value, int) else value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数: {value!r}") from exc
    if n < 0:
        raise ValueError(f"{field} 不能为负数")
    if n > MAX_FRAMES:
        raise ValueError(f"{field} 超出允许范围")
    return n


def ip_sort_key(ip: str) -> int:
    return int(ipaddress.IPv4Address(ip))


def sorted_ports(ports: Iterable[str]) -> list[str]:
    return sorted(set(ports), key=port_sort_key)
