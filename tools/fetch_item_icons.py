"""把道具图标从游戏数据目录复制进编辑器（尽量全）。

游戏内道具图标的实际位置（按优先级）：
  <files>/magica/resource/image_web/item/main/<code 小写>.png
  <files>/magica/resource/image_web/item/event/event_<code 小写>.png
  <files>/magica/resource/image_web/common/icon/icon_<code>_f.png
  <files>/madomagi/resource/image_native/item/<code>.png
  <files>/.cn-localization/**/staged/magica/resource/image_web/**   （汉化补充包）

编辑器统一存成 assets/items/<itemId 小写>.png，所以这里对每个 catalog itemId
按若干命名变体反查真实文件，找到就复制过去。

用法：
  python tools/fetch_item_icons.py --data-root "H:/magia_server/io.kamihama.totentanz/files"
"""

import argparse
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, "data", "catalog.json")
ASSETS = os.path.join(ROOT, "assets", "items")

PREFIXES = ("", "event_", "icon_", "item_")
SUFFIXES = ("", "_f", "_s", "_l")


def build_index(data_root):
    """全盘扫 png，建立「归一化文件名 → 真实路径」索引。"""
    index = {}
    for base, _dirs, names in os.walk(data_root):
        for name in names:
            if not name.lower().endswith(".png"):
                continue
            stem = name[:-4].lower()
            path = os.path.join(base, name)
            index.setdefault(stem, path)
            for prefix in PREFIXES:
                if prefix and stem.startswith(prefix):
                    index.setdefault(stem[len(prefix):], path)
            for suffix in SUFFIXES:
                if suffix and stem.endswith(suffix):
                    index.setdefault(stem[: -len(suffix)], path)
    return index


# 少数道具的图标名与 itemCode 不同名，这里是逐条核对过的别名
ALIASES = {
    "OVER_LIMITTER_PIECE": "over_limitter",
    "OVER_LIMITTER_CORE_PIECE": "over_limitter_core",
}


def candidates(code):
    alias = ALIASES.get(code.upper())
    low = alias or code.lower()
    base = re.sub(r"_\d+$", "", low)
    mid = re.sub(r"^([a-z]+_[a-z]+)_\d+_(.+)$", r"\1_\2", low)
    out, seen = [], set()
    for stem in (low, base, mid, re.sub(r"_\d+$", "", mid)):
        for prefix in PREFIXES:
            for suffix in SUFFIXES:
                cand = prefix + stem + suffix
                if cand not in seen:
                    seen.add(cand)
                    out.append(cand)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True, help="游戏 files 目录")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    with open(CATALOG, encoding="utf-8") as handle:
        catalog = json.load(handle)
    items = catalog["items"]

    index = build_index(args.data_root)
    print("索引 png 文件名 %d 个" % len(index))

    copied, already, missing = 0, 0, []
    total = 0
    for code in items:
        target = os.path.join(ASSETS, code.lower() + ".png")
        if os.path.isfile(target):
            already += 1
            continue
        source = None
        for cand in candidates(code):
            if cand in index:
                source = index[cand]
                break
        if not source:
            missing.append(code)
            continue
        if not args.dry_run:
            os.makedirs(ASSETS, exist_ok=True)
            shutil.copyfile(source, target)
        copied += 1
        total += os.path.getsize(source)

    print("新增 %d 个（%.1f MB），已有 %d 个，仍缺 %d 个"
          % (copied, total / 1048576.0, already, len(missing)))
    if missing:
        print("仍缺样例: %s" % ", ".join(missing[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
