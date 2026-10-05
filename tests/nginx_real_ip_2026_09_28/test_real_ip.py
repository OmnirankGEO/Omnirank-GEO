# -*- coding: utf-8 -*-
"""WO_303 · Review 09-28:容器 nginx 认宿主机传来的客户端 IP。

病:容器只从 Docker 网关收到请求,$remote_addr 恒为网关地址 ⇒ 转给后端的 X-Real-IP 全是同一个地址,
控制台按 IP 的登录 / 注册限频实际全站共用一个桶(容器 nginx 自己的 limit_conn / limit_req 也一样)。
修:http 级 `set_real_ip_from <Docker 网段>` + `real_ip_header X-Real-IP`(宿主机 nginx 那层设 X-Real-IP $remote_addr,由 Deploy 改)。

  R1 🔴 http 级:set_real_ip_from ≥ 1 条,每条都是私网 CIDR(10/8 · 172.16/12 · 192.168/16 内),不许 0.0.0.0/0、::/0、公网段
  R2 🔴 http 级恰一条 real_ip_header X-Real-IP;任何 server / location 里都不许再写这两种指令(不许局部改信任范围)
  R3 反臂:删 set_real_ip_from / 删 real_ip_header / 网段写成 0.0.0.0/0 / 公网段 / header 改成 X-Forwarded-For / server 里再写一条 ⇒ 各红
  R4 对照:改注释、网段换成另一个私网 CIDR ⇒ 不红
"""
from __future__ import annotations

import importlib.util
import ipaddress
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONF = ROOT / "nginx.conf"
_spec = importlib.util.spec_from_file_location(
    "wo279_model_realip", ROOT / "tests" / "nginx_backend_redirect_scheme_2026_09_23" / "test_backend_redirect_scheme.py")
M = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = M
_spec.loader.exec_module(M)

PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


def _walk(block: list):
    for d in block:
        yield d
        if d.block:
            yield from _walk(d.block)


def problems(text: str) -> list:
    tree = M.parse(text)
    top_from = [d.args for d in tree if d.name == "set_real_ip_from"]
    top_hdr = [d.args for d in tree if d.name == "real_ip_header"]
    out = []
    if not top_from:
        out.append("R1 http 级没有 set_real_ip_from")
    for args in top_from:
        try:
            net = ipaddress.ip_network(args[0], strict=False)
        except (ValueError, IndexError):
            out.append(f"R1 set_real_ip_from {args} 不是网段")
            continue
        if net.prefixlen == 0 or not any(net.subnet_of(p) for p in PRIVATE if p.version == net.version):
            out.append(f"R1 set_real_ip_from {net} 不是私网段(任何来源都能伪造 X-Real-IP)")
    if top_hdr != [["X-Real-IP"]]:
        out.append(f"R2 http 级 real_ip_header = {top_hdr},要恰一条 X-Real-IP")
    inner = [d.name for s in tree if s.block for d in _walk(s.block) if d.name in ("set_real_ip_from", "real_ip_header", "real_ip_recursive")]
    if inner:
        out.append(f"R2 server / location 里写了 {inner}(信任范围只许在 http 级定一处)")
    return out


def _real() -> str:
    return CONF.read_text(encoding="utf-8")


def test_real_config_trusts_only_the_docker_network():
    assert problems(_real()) == []


def test_arms_bite_and_controls_hold():
    text = _real()
    m = re.search(r"(?m)^set_real_ip_from (\S+);\n", text)
    h = "real_ip_header X-Real-IP;\n"
    assert m and text.count(h) == 1, "反臂没下成"
    line, net = m.group(0), m.group(1)
    first_server = text.index("\nserver {\n") + len("\nserver {\n")
    arms = {
        "删 set_real_ip_from": text.replace(line, ""),
        "删 real_ip_header": text.replace(h, ""),
        "网段 0.0.0.0/0": text.replace(line, "set_real_ip_from 0.0.0.0/0;\n"),
        "公网段": text.replace(line, "set_real_ip_from 8.8.8.0/24;\n"),
        "header 改 X-Forwarded-For": text.replace(h, "real_ip_header X-Forwarded-For;\n"),
        "server 里再写一条": text[:first_server] + "    " + line + text[first_server:],
    }
    for name, poisoned in arms.items():
        assert poisoned != text, name
        assert problems(poisoned), name
    assert problems(text.replace(line, "# 注释改写不影响判据\n" + line)) == []
    other = "10.254.0.0/16" if not net.startswith("10.254.") else "192.168.77.0/24"
    assert problems(text.replace(line, f"set_real_ip_from {other};\n")) == []
