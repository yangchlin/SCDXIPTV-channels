#!/usr/bin/env python3
"""
EPG 合并脚本
从多个 XMLTV 源拉取 EPG，合并后输出为 epg_merged.xml.gz
"""

import gzip
import io
import sys
import xml.etree.ElementTree as ET
from urllib.request import urlopen, Request
from urllib.error import URLError

# ============ 配置区 ============
# 要合并的 EPG 源列表（本地文件路径或 HTTP URL 均可）
EPG_SOURCES = [
    "http://192.168.1.10:5000/output/epg.xml.gz",   # 你自己脚本生成的
    "http://example.com/epg.xml",                     # 别人的 EPG
]

# 输出文件
OUTPUT_FILE = "output/epg_merged.xml.gz"
# ================================


def fetch_xml(source: str) -> ET.Element:
    """从 URL 或本地文件读取 XMLTV 根节点"""
    try:
        if source.startswith("http://") or source.startswith("https://"):
            req = Request(source, headers={"User-Agent": "Mozilla/5.0"})
            data = urlopen(req, timeout=30).read()
        else:
            with open(source, "rb") as f:
                data = f.read()

        if source.endswith(".gz"):
            data = gzip.decompress(data)

        tree = ET.parse(io.BytesIO(data))
        return tree.getroot()
    except Exception as e:
        print(f"[WARN] 拉取失败: {source} -> {e}", file=sys.stderr)
        return None


def merge_epg(sources: list[str]) -> ET.Element:
    """合并多个 XMLTV 源，返回合并后的 <tv> 根节点"""
    root = None
    seen_channels = set()
    seen_programs = set()

    for src in sources:
        src_root = fetch_xml(src)
        if src_root is None:
            continue

        if root is None:
            # 第一个源作为基底，直接复用
            root = src_root
            for ch in root.findall("channel"):
                cid = ch.get("id")
                if cid:
                    seen_channels.add(cid)
            for pg in root.findall("programme"):
                key = (pg.get("channel"), pg.get("start"), pg.get("stop"))
                seen_programs.add(key)
            continue

        # 后续源：追加不重复的 channel 和 programme
        for ch in src_root.findall("channel"):
            cid = ch.get("id")
            if cid and cid not in seen_channels:
                root.append(ch)
                seen_channels.add(cid)

        for pg in src_root.findall("programme"):
            key = (pg.get("channel"), pg.get("start"), pg.get("stop"))
            if key not in seen_programs:
                root.append(pg)
                seen_programs.add(key)

    return root


def write_gzip(root: ET.Element, path: str):
    """写入 gzip 压缩的 XML 文件"""
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with gzip.open(path, "wb") as f:
        f.write(xml_bytes)
    print(f"[OK] 已输出: {path}  (共 {len(root.findall('channel'))} 个频道, "
          f"{len(root.findall('programme'))} 条节目)")


if __name__ == "__main__":
    merged = merge_epg(EPG_SOURCES)
    if merged is None:
        print("[ERROR] 所有源都拉取失败，未生成文件", file=sys.stderr)
        sys.exit(1)
    write_gzip(merged, OUTPUT_FILE)