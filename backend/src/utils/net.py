"""URL 安全校验：防止"用户提交 URL、服务端代为请求"被利用为 SSRF 原语。

使用场景：设置页「测试模型连通性」等端点会把用户填写的 Base URL 直接用于
发起 HTTP 请求。若不做限制，攻击者可借此探测内网服务与云元数据
（169.254.169.254）、本机端口。

策略（最小可用、可测试）：
1. scheme 仅允许 http / https（阻断 file://、gopher://、ftp:// 等）
2. 主机名解析后的所有 IP 均不得落在回环/私有/链路本地/保留网段
3. 解析失败视为不安全（fail-closed）
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from src.core.exceptions import AppException

ALLOWED_SCHEMES = ("http", "https")

# 回环 / 私有 / 链路本地（云元数据 169.254.169.254）/ 运营商级 NAT / 未指定 / IPv6 私网
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _is_blocked_ip(raw_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return True
    return any(ip in net for net in _BLOCKED_NETWORKS)


def resolve_host_ips(host: str) -> list[str]:
    """解析主机名得到全部 IP（测试可 monkeypatch socket.getaddrinfo）。"""
    infos = socket.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


def is_safe_public_url(url: str) -> bool:
    """URL 是否可安全由服务端发起请求（fail-closed）。"""
    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        ips = resolve_host_ips(host)
    except (socket.gaierror, OSError, UnicodeError):
        return False
    if not ips:
        return False
    return not any(_is_blocked_ip(ip) for ip in ips)


def assert_safe_public_url(url: str, field: str = "Base URL") -> None:
    """不安全 URL 直接抛 400，避免在业务层重复判断。"""
    if not is_safe_public_url(url):
        raise AppException(
            400,
            f"{field} 不合法：仅允许可公网访问的 http/https 地址，"
            "禁止内网、本机、回环与云元数据地址",
        )
