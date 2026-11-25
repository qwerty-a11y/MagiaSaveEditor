"""把道具按来源活动分类（不猜：规则全部来自 itemCode 与官方 eventList）。

itemCode 是官方字段，形如 EVENT_SINGLERAID_1023_EXCHANGE_1 / CAMPAIGN_1004_EXCHANGE /
GACHA_FREEBIE_MEDAL_LIGHTGREEN_2052。脚本解析出 (族, 活动号)，组名优先用 eventList.json 的官方活动名。

输出 data/item-groups.json：
  families: {key: {label, order, itemCount, groupCount}}
  groups:   {key: {family, label, order, count, activityId}}
  items:    {itemCode: {familyKey, familyLabel, groupKey, groupLabel, activityId, activityName}}
两级：类型（每日塔 / 分支活动 / 扭蛋活动 / 通用素材 …）→ 具体活动（编号 + 官方名）。
"""

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (正则, 族键, 族名, 族序, 组名前缀)
RULES = [
    (r"^EVENT_DAILYTOWER_(\d{3,6})", "daily-tower", "每日塔", 20, "每日塔"),
    (r"^EVENT_TOWER_(\d{3,6})", "tower", "活动塔", 21, "活动塔"),
    (r"^EVENT_SINGLERAID_(\d{3,6})", "single-raid", "单人 Raid", 22, "单人 Raid"),
    (r"^EVENT_GROUPBATTLE_(\d{3,6})", "group-battle", "集团战", 23, "集团战"),
    (r"^EVENT_BRANCH_(\d{3,6})", "branch", "分支活动", 24, "分支活动"),
    (r"^EVENT_TRAINING_(\d{3,6})", "training", "养成活动", 25, "养成活动"),
    (r"^EVENT_WITCH_(\d{3,6})", "witch", "魔女战", 26, "魔女战"),
    (r"^EVENT_ARENARANKING_(\d{3,6})", "arena", "竞技场", 27, "竞技场排名战"),
    (r"^EVENT_ARENARANKMATCH_(\d{3,6})", "arena", "竞技场", 27, "排名战"),
    (r"^EVENT_ARENAMISSION_(\d{3,6})", "arena", "竞技场", 27, "竞技场任务"),
    (r"^EVENT_DUNGEON_(\d{3,6})", "dungeon", "地牢活动", 28, "地牢活动"),
    (r"^EVENT_RAID_(\d{3,6})", "raid", "Raid", 29, "Raid"),
    (r"^EVENT_ACCOMPLISH_(\d{3,6})", "accomplish", "达成活动", 30, "达成活动"),
    (r"^EVENT_COMPOSE_(\d{3,6})", "compose-event", "合成活动", 31, "合成活动"),
    (r"^EVENT_(\d{3,6})", "event", "其他活动", 39, "活动"),
    (r"^CAMPAIGN_(\d{3,6})", "campaign", "推广活动", 40, "推广活动"),
    (r"^GACHA_FREEBIE_[A-Z0-9_]*?_(\d{3,6})", "gacha", "扭蛋活动", 41, "扭蛋活动"),
    (r"^ANNIVERSARY_GACHA10_(\d{3,6})", "gacha", "扭蛋活动", 41, "扭蛋活动"),
    (r"^HOME_EV_(\d{3,6})", "home-event", "主页活动", 42, "主页活动"),
    (r"^SELECTABLE_[A-Z_]+?_(\d{3,6})", "selectable", "自选券活动", 43, "自选券活动"),
]

GENERAL = [
    (r"^COMPOSE_ITEM_", "general:compose", "养成素材（属性强化）", 1),
    (r"^CURE_", "general:cure", "回复道具", 2),
    (r"^(MONEY|PRESENTED_MONEY|YELL|PRISM|ARENA_COIN|GACHA_TICKET|SCENEZERO_PLAY_TICKET)",
     "general:currency", "货币与门票", 3),
    (r"^(LIMIT_BREAK_ALL|MEMORIA_CIRCUIT|OVER_LIMITTER|EPISODE_COMPOSE_PP)",
     "general:growth", "通用养成素材", 4),
    (r"^BACKGROUND_|^BG_", "general:background", "主页背景", 5),
    (r"^SELECTABLE_", "general:selectable", "自选券", 6),
    (r"^CHALLENGE_", "general:challenge", "挑战奖励", 7),
    (r"^HOME_", "general:home", "主页道具", 8),
]


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# 这些族的编号与 eventList 的 eventId 同一命名空间，可以直接查官方活动名；
# 扭蛋 / 自选券等用的是 banner、券号命名空间，查 eventList 会串名。
EVENT_NAMESPACED = {
    "daily-tower", "tower", "single-raid", "group-battle", "branch", "training",
    "witch", "arena", "dungeon", "raid", "accomplish", "compose-event", "event",
}


def classify(code, events):
    for pattern, family, family_label, order, prefix in RULES:
        match = re.match(pattern, code)
        if not match:
            continue
        number = int(match.group(1))
        name = events.get(number) if family in EVENT_NAMESPACED else None
        label = ("%s · %s" % (name, prefix)) if name else ("%s %d" % (prefix, number))
        return {
            "familyKey": family, "familyLabel": family_label, "familyOrder": order,
            "groupKey": "%s:%d" % (family, number), "groupLabel": label, "groupOrder": 100 + number,
            "activityId": number, "activityName": name,
        }
    for pattern, key, label, order in GENERAL:
        if re.match(pattern, code):
            return {"familyKey": "general", "familyLabel": "通用 / 系统", "familyOrder": 10,
                    "groupKey": key, "groupLabel": label, "groupOrder": order * 100,
                    "activityId": None, "activityName": None}
    return {"familyKey": "general", "familyLabel": "通用 / 系统", "familyOrder": 10,
            "groupKey": "general:other", "groupLabel": "其他 / 未分类", "groupOrder": 900,
            "activityId": None, "activityName": None}


def main(argv=None):
    parser = argparse.ArgumentParser(description="道具按活动分类")
    parser.add_argument("--libs", required=True)
    args = parser.parse_args(argv)
    libs = os.path.abspath(args.libs)
    items = load_json(os.path.join(libs, "itemList.json"))
    events = {row["eventId"]: row["eventName"] for row in load_json(os.path.join(libs, "eventList.json"))}
    # catalog 里可能还有 libs 未收录的活动交换品（来自 MCATBIN1），一并纳入分类
    timeline_labels = {}
    timeline_path = os.path.join(ROOT, "data", "item-timeline.json")
    if os.path.isfile(timeline_path):
        timeline_labels = load_json(timeline_path).get("labels") or {}

    catalog_path = os.path.join(ROOT, "data", "catalog.json")
    if os.path.isfile(catalog_path):
        known = {row.get("itemCode") for row in items}
        for code in load_json(catalog_path).get("items") or {}:
            if code not in known:
                items.append({"itemCode": code})

    result = {"items": {}, "groups": {}, "families": {}}
    for row in items:
        code = row.get("itemCode")
        if not code:
            continue
        info = classify(code, events)
        # 时间线里有官方名的活动（扭蛋 / 魔女战 / 单人 Raid …），用它替换「族名 + 编号」
        official = timeline_labels.get("%s:%s" % (info.get("familyKey"), info.get("activityId")))
        if official:
            info["activityName"] = official
            info["groupLabel"] = official
        result["items"][code] = info
        family = result["families"].setdefault(info["familyKey"], {
            "label": info["familyLabel"], "order": info["familyOrder"], "itemCount": 0, "groupCount": 0})
        family["itemCount"] += 1
        group = result["groups"].get(info["groupKey"])
        if group is not None:
            official_label = timeline_labels.get("%s:%s" % (info.get("familyKey"), info.get("activityId")))
            if official_label:
                group["label"] = official_label
        if group is None:
            group = result["groups"][info["groupKey"]] = {
                "family": info["familyKey"], "label": info["groupLabel"], "order": info["groupOrder"],
                "count": 0, "activityId": info["activityId"]}
            family["groupCount"] += 1
        group["count"] += 1

    out = os.path.join(ROOT, "data", "item-groups.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, separators=(",", ":"))

    families = sorted(result["families"].items(), key=lambda pair: (pair[1]["order"], pair[1]["label"]))
    print("道具", len(result["items"]), "类型", len(families), "活动组", len(result["groups"]))
    named = sum(1 for info in result["items"].values() if info["activityName"])
    print("带官方活动名的道具:", named)
    for key, family in families:
        print("   %-14s %-14s 道具 %4d 组 %3d" % (key, family["label"], family["itemCount"], family["groupCount"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
