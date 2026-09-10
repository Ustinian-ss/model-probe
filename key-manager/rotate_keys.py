"""NVIDIA API Key 轮换辅助脚本。

子命令：
    list                          列出当前所有 Key
    add    --new KEY               追加一个新 Key
    remove  --old KEY              删除一个旧 Key
    rotate  --old KEY --new KEY    一步替换：追加新 Key → 删除旧 Key（不自动删 NGC 端）
    audit                          打印 Key 的 SHA256 前 8 位，便于与 NGC 端对照

不会触碰 .env 之外的任何文件。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"
KEY_LINE_PREFIX = "NVIDIA_API_KEYS="


def load_keys() -> list[str]:
    if not ENV_FILE.exists():
        raise SystemExit(f"找不到 {ENV_FILE}")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(KEY_LINE_PREFIX):
            raw = line[len(KEY_LINE_PREFIX):]
            return [k.strip() for k in raw.split(",") if k.strip()]
    raise SystemExit(f"{ENV_FILE} 中没有 {KEY_LINE_PREFIX} 行")


def save_keys(keys: list[str]) -> None:
    """原子写回 .env：先备份，再写临时文件后 os.replace，避免中途崩溃丢掉全部 Key。"""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    new_line = KEY_LINE_PREFIX + ",".join(keys)
    new_lines = [new_line if l.startswith(KEY_LINE_PREFIX) else l for l in lines]
    if not any(l.startswith(KEY_LINE_PREFIX) for l in lines):
        new_lines.insert(0, new_line)
    content = "\n".join(new_lines) + "\n"

    # 备份旧文件（轮换工具出问题时至少能手工找回）
    if ENV_FILE.exists():
        ENV_FILE.with_suffix(".env.bak").write_text(
            ENV_FILE.read_text(encoding="utf-8"), encoding="utf-8"
        )

    fd, tmp_name = tempfile.mkstemp(dir=ENV_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, ENV_FILE)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def fingerprint(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()[:8]


def cmd_list(args: argparse.Namespace) -> None:
    keys = load_keys()
    print(f"共 {len(keys)} 个 Key：")
    for i, k in enumerate(keys, 1):
        # 默认只显示指纹，避免前后缀叠加缩小暴力破解空间；--verbose 才显示前后缀
        if getattr(args, "verbose", False):
            print(f"  {i:2d}. {fingerprint(k)}  {k[:12]}…{k[-6:]}")
        else:
            print(f"  {i:2d}. {fingerprint(k)}")


def cmd_add(args: argparse.Namespace) -> None:
    keys = load_keys()
    new = args.new.strip()
    if not new.startswith("nvapi-"):
        raise SystemExit("新 Key 格式异常（应以 nvapi- 开头）")
    if new in keys:
        print("该 Key 已在 .env 中，无需重复添加。")
        return
    keys.append(new)
    save_keys(keys)
    print(f"已追加新 Key（{fingerprint(new)}），共 {len(keys)} 个 Key。")


def cmd_remove(args: argparse.Namespace) -> None:
    keys = load_keys()
    old = args.old.strip()
    if old not in keys:
        print(f"未在 .env 中找到该 Key（{fingerprint(old)}）。请核对完整值。")
        sys.exit(2)
    keys = [k for k in keys if k != old]
    save_keys(keys)
    print(f"已移除 Key（{fingerprint(old)}），剩余 {len(keys)} 个。")


def cmd_rotate(args: argparse.Namespace) -> None:
    """追加新 Key 后立即删除旧 Key。**不**做灰度；紧急场景下使用。"""
    cmd_add(argparse.Namespace(new=args.new))
    cmd_remove(argparse.Namespace(old=args.old))


def cmd_audit(args: argparse.Namespace) -> None:
    keys = load_keys()
    print("Key 指纹（用于与 NGC 控制台对照，不会泄露完整 Key）：")
    for k in keys:
        if getattr(args, "verbose", False):
            print(f"  {fingerprint(k)}  ({k[:10]}…)")
        else:
            print(f"  {fingerprint(k)}")


def main() -> None:
    p = argparse.ArgumentParser(description="NVIDIA API Key 轮换助手")
    sub = p.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("list", help="列出所有 Key")
    lp.add_argument("--verbose", "-v", action="store_true", help="同时显示 Key 前后缀（默认仅指纹）")
    lp.set_defaults(func=cmd_list)
    a = sub.add_parser("add", help="追加一个新 Key")
    a.add_argument("--new", required=True)
    a.set_defaults(func=cmd_add)
    r = sub.add_parser("remove", help="删除一个旧 Key")
    r.add_argument("--old", required=True)
    r.set_defaults(func=cmd_remove)
    rot = sub.add_parser("rotate", help="一步替换（紧急场景，不做灰度）")
    rot.add_argument("--old", required=True)
    rot.add_argument("--new", required=True)
    rot.set_defaults(func=cmd_rotate)
    ap = sub.add_parser("audit", help="打印 Key 指纹")
    ap.add_argument("--verbose", "-v", action="store_true", help="同时显示 Key 前缀（默认仅指纹）")
    ap.set_defaults(func=cmd_audit)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
