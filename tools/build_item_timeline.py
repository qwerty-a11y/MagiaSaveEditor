"""按时间线给道具分类：每个活动 = 一个时间窗口，道具、商店挂在对应窗口下。

时间来源全部取自官方/复原数据，不猜：
  daily-tower      offline/data/daily-tower-calendar-bindings.json  events[].runtimePeriod
  training         offline/data/event-training-calendar-bindings.json periods[].canonicalPeriod
  group-battle     offline/data/group-battle-periods.json           periods[].event.startAt
  arena            offline/data/local-arena-ranking-periods.json     periods[].startAt
  museum           offline/data/museum-history-periods.json         periods[].startAt
  branch           MCATBIN1 tables.section（questType=BRANCH，genericId 即 eventId）的 openDate
  gacha            offline/data/gacha-calendar-history.json         bannerIds → 活动号
  shop             libs/shopItemList.json 的 needItemId（EVENT_<FAM>_<id>_EXCHANGE_*）+ startAt

输出 data/item-timeline.json：
  timelines: 按开始时间排序的活动窗口，含 activities / items / shops
  items:     itemCode → {timelineKey, familyKey, activityId, startAt, endAt, basis}
  counts:    覆盖率统计（逐来源）
"""

import argparse
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_compact_catalog import CompactCatalog, as_int  # noqa: E402

# 数据包里确实没名字的活动，用族名 + 编号占位（不冒充官方名）
DEFAULT_LABELS = {
    "single-raid": "单人 Raid",
    "tower": "活动塔",
    "witch": "魔女战",
    "branch": "分支活动",
    "gacha": "扭蛋活动",
    "daily-tower": "每日塔",
    "training": "养成活动",
    "group-battle": "集团战",
    "arena": "竞技场",
    "accomplish": "达成活动",
}

DEFAULT_CATALOG = os.path.join(os.path.dirname(ROOT), "_re", "catalog_full.json")


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_zip_json(archive, name):
    try:
        return json.loads(archive.read(name).decode("utf-8"))
    except KeyError:
        return None


def norm_date(text):
    """取 YYYY/MM/DD 部分，保证可排序比较。"""
    if not isinstance(text, str):
        return None
    match = re.match(r"(\d{4})/(\d{2})/(\d{2})", text)
    return match.group(0) if match else None


def main(argv=None):
    parser = argparse.ArgumentParser(description="道具时间线分类")
    parser.add_argument("--libs", required=True)
    parser.add_argument("--zip", required=True)
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    args = parser.parse_args(argv)

    layout = load_json(os.path.join(ROOT, "data", "item-groups.json"))
    items = layout["items"]

    periods = {}      # (family, activityId) -> {"start": ..., "end": ..., "basis": ...}
    coverage = {}
    labels = {}       # (family, activityId) -> 官方活动名
    event_names = {}

    def put(family, activity_id, start, end, basis):
        if not isinstance(activity_id, int) or not activity_id:
            return
        start_date, end_date = norm_date(start), norm_date(end)
        if not start_date:
            return
        key = (family, activity_id)
        old = periods.get(key)
        # 已有更精确（有结束日）的就保留
        if old and old.get("end") and not end_date:
            return
        periods[key] = {"start": start_date, "end": end_date or start_date, "basis": basis}
        coverage[basis] = coverage.get(basis, 0) + 1

    with zipfile.ZipFile(args.zip) as archive:
        data = read_zip_json(archive, "offline/data/daily-tower-calendar-bindings.json")
        if data:
            for row in (data.get("events") or {}).values():
                if not isinstance(row, dict):
                    continue
                period = row.get("runtimePeriod") or {}
                put("daily-tower", as_int(row.get("eventId")),
                    period.get("startAt"), period.get("endAt"), "dailytower-calendar")

        # additions 里是 Puella Historia 等后补期数（1183 / 1186 / 1190 …）
        data = read_zip_json(archive, "offline/data/daily-tower-calendar-additions.json")
        if data:
            for row in (data.get("rows") or {}).values():
                put("daily-tower", as_int(row.get("eventId")),
                    row.get("startAt"), row.get("endAt") or row.get("closeAt"),
                    "dailytower-additions")
                if row.get("name"):
                    labels[("daily-tower", as_int(row.get("eventId")))] = row["name"]

        # 实例档案里还有 calendar-bindings 未列的期数（1010 / 1011 等）
        data = read_zip_json(archive, "offline/data/daily-tower-instances.json")
        if data:
            for row in data.get("events") or []:
                event = ((row.get("runtime") or {}).get("event") or {})
                put("daily-tower", as_int(event.get("eventId")),
                    event.get("startAt"), event.get("endAt") or event.get("closeAt"),
                    "dailytower-instances")

        data = read_zip_json(archive, "offline/data/event-training-calendar-bindings.json")
        if data:
            for row in data.get("periods") or []:
                canon = row.get("canonicalPeriod") or {}
                put("training", as_int(row.get("eventId")),
                    row.get("startAt") or canon.get("startAt"),
                    row.get("endAt") or canon.get("endAt"), "training-calendar")

        data = read_zip_json(archive, "offline/data/group-battle-periods.json")
        if data:
            for row in data.get("periods") or []:
                event = row.get("event") or {}
                put("group-battle", as_int(event.get("regularEventId")),
                    event.get("startAt"), event.get("endAt") or event.get("closeAt"),
                    "group-battle-periods")

        data = read_zip_json(archive, "offline/data/local-arena-ranking-periods.json")
        if data:
            for row in data.get("periods") or []:
                put("arena", as_int(row.get("eventId")) or as_int(row.get("originalIdProven")),
                    row.get("startAt"), row.get("endAt") or row.get("closeAt"), "arena-periods")

        data = read_zip_json(archive, "offline/data/museum-history-periods.json")
        if data:
            for row in data.get("periods") or []:
                put("accomplish", as_int(row.get("uiRegularEventId")),
                    row.get("startAt"), row.get("endAt"), "museum-periods")

        data = read_zip_json(archive, "offline/data/gacha-calendar-history.json")
        if data:
            banner_dates = {}
            for row in data.get("rows") or []:
                start = norm_date(row.get("startAt"))
                if not start:
                    continue
                for bid in row.get("bannerIds") or []:
                    banner_dates.setdefault(int(bid), (row.get("startAt"), row.get("endAt")))
            for code, info in items.items():
                if info.get("familyKey") != "gacha":
                    continue
                activity = info.get("activityId")
                if activity in banner_dates:
                    put("gacha", activity, banner_dates[activity][0], banner_dates[activity][1],
                        "gacha-banner")

    # 分支活动：小节的 genericId 就是活动号，openDate 是首话开放时间
    if os.path.isfile(args.catalog):
        root = CompactCatalog(open(args.catalog, "rb").read()).root_value()
        branch = {}
        for row in (root["tables"].get("section") or {}).values():
            if not isinstance(row, dict) or row.get("questType") != "BRANCH":
                continue
            activity = as_int(row.get("genericId"))
            date = norm_date(row.get("openDate"))
            if not activity or not date:
                continue
            bucket = branch.setdefault(activity, [date, date])
            bucket[0] = min(bucket[0], date)
            bucket[1] = max(bucket[1], date)
        for activity, (start, end) in branch.items():
            put("branch", activity, start, end, "branch-section-openDate")

    # 活动名：优先用各周期源自带的名字，其次 eventList.json 的官方名
    event_list = os.path.join(args.libs, "eventList.json")
    if os.path.isfile(event_list):
        for row in load_json(event_list):
            event_names[row.get("eventId")] = row.get("eventName")

    with zipfile.ZipFile(args.zip) as archive:
        data = read_zip_json(archive, "offline/data/daily-tower-calendar-bindings.json")
        for row in ((data or {}).get("events") or {}).values():
            if isinstance(row, dict) and row.get("runtimeName"):
                labels[("daily-tower", as_int(row.get("eventId")))] = row["runtimeName"]
        data = read_zip_json(archive, "offline/data/daily-tower-instances.json")
        for row in (data or {}).get("events") or []:
            event = ((row.get("runtime") or {}).get("event") or {})
            if event.get("name"):
                labels[("daily-tower", as_int(event.get("eventId")))] = event["name"]
        data = read_zip_json(archive, "offline/data/event-training-calendar-bindings.json")
        for row in (data or {}).get("periods") or []:
            name = (row.get("event") or {}).get("name")
            if name:
                labels[("training", as_int(row.get("eventId")))] = name
        # displayName 才是官方中文篇名（calendar-bindings 里没有 event.name）
        data = read_zip_json(archive, "offline/data/event-training-periods.json")
        for row in (data or {}).get("events") or []:
            event = row.get("event") or {}
            name = row.get("displayName") or event.get("name")
            if name:
                labels[("training", as_int(event.get("eventId")))] = name
        data = read_zip_json(archive, "offline/data/group-battle-periods.json")
        for row in (data or {}).get("periods") or []:
            event = row.get("event") or {}
            name = row.get("displayName") or event.get("name")
            if name:
                labels[("group-battle", as_int(event.get("regularEventId")))] = name
        data = read_zip_json(archive, "offline/data/local-arena-ranking-periods.json")
        for row in (data or {}).get("periods") or []:
            if row.get("name"):
                labels[("arena", as_int(row.get("eventId")) or as_int(row.get("originalIdProven")))] = row["name"]
        data = read_zip_json(archive, "offline/data/museum-history-periods.json")
        for row in (data or {}).get("periods") or []:
            if row.get("name"):
                labels[("accomplish", as_int(row.get("uiRegularEventId")))] = row["name"]
        data = read_zip_json(archive, "offline/data/gacha-calendar-history.json")
        for row in (data or {}).get("rows") or []:
            for bid in row.get("bannerIds") or []:
                labels[("gacha", int(bid))] = row.get("name") or row.get("sourceTitle")

        # 溯源表按 key=historical-gacha-<bannerId> 索引，覆盖比 calendar 更全（595 条）
        data = read_zip_json(archive, "offline/data/gacha-calendar-history.names.provenance.json")
        for row in (data or {}).get("rows") or []:
            key = str(row.get("key") or "")
            try:
                bid = int(key.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                continue
            name = row.get("displayTitle")
            if name:
                labels[("gacha", bid)] = name
            # 扭蛋币用的是 scheduleRoute 的编号（#/GachaTop/<scheduleId>），
            # 与 bannerId 不同命名空间，两个都要登记
            route = str(row.get("route") or "")
            match = re.search(r"/(\d+)$", route)
            if match and name:
                labels[("gacha", int(match.group(1)))] = name

    with zipfile.ZipFile(args.zip) as archive:
        # 单人 Raid：editions[eventId].section[*].openDate
        data = read_zip_json(archive, "offline/data/single-raid-archive-masters.json")
        for event_id, edition in ((data or {}).get("editions") or {}).items():
            dates = sorted({row.get("openDate") for row in (edition.get("section") or {}).values()
                            if row.get("openDate")})
            if dates:
                numeric = as_int(event_id)
                if isinstance(numeric, str) and numeric.isdigit():
                    numeric = int(numeric)
                put("single-raid", numeric, dates[0], dates[-1], "single-raid-sections")

        # 活动塔（复刻塔等）
        data = read_zip_json(archive, "offline/data/event-tower-1035.json")
        for row in (data or {}).get("events") or []:
            event = row.get("event") or {}
            put("tower", as_int(event.get("eventId")), event.get("startAt"),
                event.get("endAt") or event.get("closeAt"), "event-tower-instances")
            if row.get("displayName"):
                labels[("tower", as_int(event.get("eventId")))] = row["displayName"]

        # 魔女战
        data = read_zip_json(archive, "offline/data/witch-instances.json")
        for row in (data or {}).get("events") or []:
            put("witch", as_int(row.get("eventId")), row.get("startAt"),
                row.get("endAt") or row.get("closeAt"), "witch-instances")
            if row.get("name"):
                labels[("witch", as_int(row.get("eventId")))] = row["name"]

        # 每日塔补充期（Puella Historia 等）
        data = read_zip_json(archive, "offline/data/daily-tower-calendar-additions.json")
        for row in (data or {}).get("rows", {}).values():
            put("daily-tower", as_int(row.get("eventId")), row.get("startAt"),
                row.get("endAt") or row.get("closeAt"), "dailytower-calendar")

    # 商店：needItemId 里的 EVENT_<FAM>_<id>_EXCHANGE_* 把活动号和时间连起来
    shops = load_json(os.path.join(args.libs, "shopItemList.json"))
    shop_by_activity = {}
    need_pattern = re.compile(r"^EVENT_([A-Z]+)_(\d{3,6})_")
    family_map = {
        "DAILYTOWER": "daily-tower", "TOWER": "tower", "SINGLERAID": "single-raid",
        "GROUPBATTLE": "group-battle", "BRANCH": "branch", "TRAINING": "training",
        "WITCH": "witch", "ARENARANKING": "arena", "ARENARANKMATCH": "arena",
        "ARENAMISSION": "arena", "DUNGEON": "dungeon", "RAID": "raid",
        "ACCOMPLISH": "accomplish",
    }
    for row in shops:
        need = row.get("needItemId")
        start = norm_date(row.get("startAt"))
        if not isinstance(need, str) or not start:
            continue
        match = need_pattern.match(need)
        if not match:
            continue
        family = family_map.get(match.group(1))
        if not family:
            continue
        activity = int(match.group(2))
        shop_by_activity.setdefault((family, activity), set()).add(row.get("id"))
        if (family, activity) not in periods:
            put(family, activity, row.get("startAt"), row.get("endAt"), "shop-window")

    # 组装时间线：每个 (family, activity) 一个窗口，按开始日期排序
    timelines = []
    item_map = {}
    for code, info in items.items():
        family = info.get("familyKey")
        activity = info.get("activityId")
        window = periods.get((family, activity)) if activity else None
        if not window:
            continue
        key = "%s:%s" % (family, activity)
        item_map[code] = {
            "timelineKey": key,
            "familyKey": family,
            "activityId": activity,
            "activityName": info.get("activityName"),
            "start": window["start"],
            "end": window["end"],
            "basis": window["basis"],
        }

    buckets = {}
    for code, entry in item_map.items():
        bucket = buckets.setdefault(entry["timelineKey"], {
            "key": entry["timelineKey"],
            "familyKey": entry["familyKey"],
            "activityId": entry["activityId"],
            "label": (labels.get((entry["familyKey"], entry["activityId"]))
                      or event_names.get(entry["activityId"])
                      or entry["activityName"]
                      or DEFAULT_LABELS.get(entry["familyKey"], entry["familyKey"]) + " " + str(entry["activityId"])),
            "start": entry["start"],
            "end": entry["end"],
            "basis": entry["basis"],
            "items": [],
            "shops": sorted(shop_by_activity.get((entry["familyKey"], entry["activityId"])) or []),
        })
        bucket["items"].append(code)

    for bucket in buckets.values():
        bucket["items"].sort()
        bucket["itemCount"] = len(bucket["items"])
        bucket["year"] = bucket["start"][:4]
        timelines.append(bucket)
    timelines.sort(key=lambda row: (row["start"], row["familyKey"], row["activityId"]))

    # 给道具分类用：((family, activityId) -> 官方活动名)
    label_map = {"%s:%s" % key: value for key, value in labels.items() if value}

    payload = {
        "timelines": timelines,
        "labels": label_map,
        "items": item_map,
        "counts": {
            "items": len(items),
            "timelines": len(timelines),
            "timedItems": len(item_map),
            "untimedItems": len(items) - len(item_map),
            "byBasis": coverage,
        },
    }
    path = os.path.join(ROOT, "data", "item-timeline.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))

    print("时间线 %d 条，已定位道具 %d / %d（未定位 %d）"
          % (len(timelines), len(item_map), len(items), len(items) - len(item_map)))
    for basis, count in sorted(coverage.items()):
        print("   时间来源 %-24s %d 个活动" % (basis, count))
    print("体积 %.1f KB" % (os.path.getsize(path) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
