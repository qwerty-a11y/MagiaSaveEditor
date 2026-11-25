"""判定哪些道具是「玩家可见 / 有实际作用」，哪些只是内部道具。

判据全部可复核，逐条来自官方数据，不做主观取舍：
  icon      官方背包图标存在（assets/items/<itemCode>.png，源自 image_web/item/main）
  type      itemList.itemType 非空（BACKGROUND / CHALLENGE / COMPOSE / MONEY）
  shop      shopItemList.json 的 rewardCode / needItemId / genericId 引用
  reward    数据包 offline/data/*.json 的奖励代码引用（ITEM_<code>）
  currency  货币与门票白名单（存档里作为持有物出现的那些）

任一命中即视为玩家可见（scope=player）；全不命中记为 internal。
生成 data/item-visibility.json。
"""

import argparse
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURRENCY = {
    "MONEY", "PRESENTED_MONEY", "YELL", "PRISM", "ARENA_COIN", "GACHA_TICKET",
    "SCENEZERO_PLAY_TICKET", "DAILY_COIN", "DESTINY_BOTTLE", "DESTINY_CRYSTAL",
    "EPISODE_COMPOSE_PP", "EVENTSTORY_OPEN_KEY",
}


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description="道具可见性判定")
    parser.add_argument("--libs", required=True)
    parser.add_argument("--zip", required=True, help="源码/数据包 zip（读取 offline/data 的奖励引用）")
    parser.add_argument("--assets", default=os.path.join(ROOT, "assets", "items"))
    parser.add_argument("--max-mb", type=float, default=40)
    args = parser.parse_args(argv)

    libs = os.path.abspath(args.libs)
    items = load_json(os.path.join(libs, "itemList.json"))
    shops = load_json(os.path.join(libs, "shopItemList.json"))
    codes = {row["itemCode"]: row for row in items if row.get("itemCode")}
    # catalog 里还有 MCATBIN1 独有、itemList 未收录的活动交换品（如 EVENT_TRAINING_1001），
    # 不纳入就会在 scope=player 过滤时被整条丢掉。
    catalog_path = os.path.join(ROOT, "data", "catalog.json")
    if os.path.isfile(catalog_path):
        for code in load_json(catalog_path).get("items") or {}:
            if code not in codes:
                codes[code] = {"itemCode": code}
    icons = {os.path.splitext(name)[0] for name in os.listdir(args.assets)}

    evidence = {code: set() for code in codes}
    for code in codes:
        if code.lower() in icons:
            evidence[code].add("icon")
        if codes[code].get("itemType"):
            evidence[code].add("type")
        if code in CURRENCY:
            evidence[code].add("currency")

    for row in shops:
        for field in ("rewardCode", "needItemId", "genericId"):
            text = row.get(field)
            if not isinstance(text, str):
                continue
            for piece in text.split(","):
                piece = piece.strip()
                if not piece:
                    continue
                candidates = []
                if piece.startswith("ITEM_"):
                    body = piece[5:]
                    candidates = [body, re.sub(r"_\d+$", "", body)]
                else:
                    candidates = [piece, re.sub(r"_\d+$", "", piece)]
                for candidate in candidates:
                    if candidate in evidence:
                        evidence[candidate].add("shop")

    limit = int(args.max_mb * 1024 * 1024)
    pattern = re.compile(rb"ITEM_([A-Z][A-Z0-9_]{2,60})")
    with zipfile.ZipFile(args.zip) as archive:
        for name in archive.namelist():
            if not (name.startswith("offline/data/") and name.endswith(".json")):
                continue
            info = archive.getinfo(name)
            if info.file_size > limit:
                continue
            try:
                raw = archive.read(name)
            except Exception:
                continue
            for match in pattern.finditer(raw):
                body = match.group(1).decode("ascii", "ignore")
                for candidate in (body, re.sub(r"_\d+$", "", body)):
                    if candidate in evidence:
                        evidence[candidate].add("reward")

    out = {"items": {}, "counts": {}}
    counts = {}
    for code, tags in evidence.items():
        scope = "player" if tags else "internal"
        out["items"][code] = {"scope": scope, "evidence": sorted(tags)}
        counts[scope] = counts.get(scope, 0) + 1
        for tag in tags:
            counts["tag:" + tag] = counts.get("tag:" + tag, 0) + 1
    out["counts"] = counts

    path = os.path.join(ROOT, "data", "item-visibility.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(out, handle, ensure_ascii=False, separators=(",", ":"))

    print("道具", len(codes))
    print("player", counts.get("player", 0), "internal", counts.get("internal", 0))
    for tag in ("icon", "type", "shop", "reward", "currency"):
        print("   证据 %-9s %d" % (tag, counts.get("tag:" + tag, 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
