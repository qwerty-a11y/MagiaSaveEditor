"""从客户端主数据（官方中文 libs）构建编辑器用的 ID→名称索引。

数据来源（安装包解压后的 APK 数据目录，非猜测）：
  <APK_DATA>/files/magica/js/libs/itemList.json     道具
  <APK_DATA>/files/magica/js/libs/charaList.json    魔法少女
  <APK_DATA>/files/magica/js/libs/cardList.json     卡片
  <APK_DATA>/files/magica/js/libs/pieceList.json    记忆结晶
  <APK_DATA>/files/magica/js/libs/giftList.json     礼物
  <APK_DATA>/files/magica/js/libs/sectionList.json  关卡小节
  <APK_DATA>/files/magica/js/libs/chapterList.json  章节
  <APK_DATA>/files/magica/js/libs/eventList.json    活动
  <APK_DATA>/files/magica/js/libs/doppelList.json   魔女化身
  <APK_DATA>/files/magica/js/libs/missionChallengeMap.json 挑战任务文本
  <APK_DATA>/files/magica/js/libs/emotionSkillMap.json 情感技能（精神树）

可选：--full-catalog 指向 APK 的 MCATBIN1 catalog，用 tables.chara / tables.card 补齐 libs 里缺的属性。

用法：
  python tools/build_catalog.py --libs "H:/magia_server/io.kamihama.totentanz/files/magica/js/libs"       --out data/catalog.json
"""

import argparse
import json
import os
import sys
import time


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def load_master_tables(full_catalog_path):
    """读 MCATBIN1 的 tables.chara / tables.card，用来补 libs 缺的字段。"""
    if not full_catalog_path or not os.path.isfile(full_catalog_path):
        return {}, {}, {}
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from parse_compact_catalog import CompactCatalog
    with open(full_catalog_path, "rb") as handle:
        root = CompactCatalog(handle.read()).root_value()
    tables = root.get("tables") or {}
    return (tables.get("chara") or {}, tables.get("card") or {},
            tables.get("piece") or {}, tables.get("item") or {})


def build(libs_dir, full_catalog_path=None):
    items = load_json(os.path.join(libs_dir, "itemList.json"), []) or []
    chara = load_json(os.path.join(libs_dir, "charaList.json"), []) or []
    cards = load_json(os.path.join(libs_dir, "cardList.json"), []) or []
    pieces = load_json(os.path.join(libs_dir, "pieceList.json"), []) or []
    gifts = load_json(os.path.join(libs_dir, "giftList.json"), []) or []
    sections = load_json(os.path.join(libs_dir, "sectionList.json"), []) or []
    chapters = load_json(os.path.join(libs_dir, "chapterList.json"), []) or []
    events = load_json(os.path.join(libs_dir, "eventList.json"), []) or []
    doppels = load_json(os.path.join(libs_dir, "doppelList.json"), []) or []
    challenges = load_json(os.path.join(libs_dir, "missionChallengeMap.json"), {}) or {}
    emotion_skills = load_json(os.path.join(libs_dir, "emotionSkillMap.json"), {}) or {}

    catalog = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": libs_dir,
        "items": {},
        "chara": {},
        "cards": {},
        "pieces": {},
        "gifts": {},
        "sections": {},
        "chapters": {},
        "events": {},
        "doppels": {},
        "challenges": {},
        "emotionSkills": {},
    }
    for row in items:
        code = row.get("itemCode")
        if not code:
            continue
        entry = {"name": row.get("name") or code}
        for key, out in (("itemType", "type"), ("unit", "unit"), ("description", "desc"),
                         ("shortDescription", "short"), ("rank", "rank"),
                         ("treasureChestColor", "chest")):
            if row.get(key):
                entry[out] = row[key]
        catalog["items"][code] = entry
    master_chara, master_cards, master_pieces, master_items = load_master_tables(full_catalog_path)
    # MCATBIN1 里还有 libs 未收录的道具（早期活动交换品），一并补上
    for code, row in master_items.items():
        if not isinstance(row, dict) or code in catalog["items"]:
            continue
        if not row.get("name"):
            continue
        entry = {"name": row["name"]}
        for key, out in (("description", "desc"), ("unit", "unit"), ("itemType", "type")):
            if row.get(key):
                entry[out] = row[key]
        entry["source"] = "mcatbin1"
        catalog["items"][code] = entry

    # 卡片上的属性作为第二保险：同一角色的卡属性一致
    card_attribute = {}
    for row in master_cards.values():
        if not isinstance(row, dict):
            continue
        chara_no = row.get("charaNo")
        attribute = row.get("attributeId")
        if chara_no is not None and attribute:
            card_attribute.setdefault(str(int(chara_no)), attribute)
    for row in chara:
        key = row.get("id")
        if key is None:
            continue
        attribute = row.get("attributeId")
        title = row.get("title")
        school = row.get("school")
        initial = row.get("initialType")
        master = master_chara.get(str(key)) or {}
        if not attribute:
            attribute = master.get("attributeId") or card_attribute.get(str(key))
        if not title:
            title = master.get("title")
        if not school:
            school = master.get("school")
        if not initial:
            initial = master.get("initialType")
        catalog["chara"][str(key)] = {
            "name": row.get("name") or str(key),
            "attribute": attribute,
            "school": school,
            "title": title,
            "initialType": initial,
        }
    for row in cards:
        key = row.get("cardId")
        if key is None:
            continue
        catalog["cards"][str(key)] = {
            "name": row.get("cardName") or str(key),
            "illustrator": row.get("illustrator"),
        }
    for row in pieces:
        key = row.get("pieceId")
        if key is None:
            continue
        master = master_pieces.get(str(key)) or {}
        catalog["pieces"][str(key)] = {
            "name": row.get("pieceName") or str(key),
            "type": row.get("pieceType") or master.get("pieceType"),
            "kind": row.get("pieceKind") or master.get("pieceKind"),
            "attribute": row.get("attributeId") or master.get("attributeId"),
            "rank": row.get("rank") or master.get("rank"),
        }
    for row in gifts:
        key = row.get("id")
        if key is None:
            continue
        catalog["gifts"][str(key)] = row.get("name") or str(key)
    for row in sections:
        key = row.get("sectionId")
        if key is None:
            continue
        catalog["sections"][str(key)] = {
            "title": row.get("title"),
            "area": row.get("areaDetailName"),
            "chara": row.get("charaName"),
            "questType": row.get("questType"),
        }
    for row in chapters:
        key = row.get("chapterId")
        if key is None:
            continue
        catalog["chapters"][str(key)] = {
            "no": row.get("chapterNoForView"),
            "title": row.get("title"),
            "type": row.get("chapterType"),
            "questType": row.get("questType"),
        }
    for row in events:
        key = row.get("eventId")
        if key is None:
            continue
        catalog["events"][str(key)] = row.get("eventName") or str(key)
    for row in doppels:
        key = row.get("id")
        if key is None:
            continue
        catalog["doppels"][str(key)] = {
            "name": row.get("name"),
            "title": row.get("title"),
        }
    for key, text in challenges.items():
        catalog["challenges"][key] = text

    for key, row in emotion_skills.items():
        if not isinstance(row, dict):
            continue
        catalog["emotionSkills"][str(key)] = {
            "name": row.get("name"),
            "shortDescription": row.get("shortDescription"),
            "type": row.get("type"),
            "skillEffectRange": row.get("skillEffectRange"),
        }
    return catalog


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建编辑器主数据索引")
    parser.add_argument("--libs", required=True, help="客户端 files/magica/js/libs 目录")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data", "catalog.json"))
    parser.add_argument("--full-catalog", help="MCATBIN1 catalog（补齐属性等）")
    args = parser.parse_args(argv)
    catalog = build(os.path.abspath(args.libs), args.full_catalog)
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")))
    counts = {key: len(value) for key, value in catalog.items() if isinstance(value, dict)}
    print("wrote", out_path)
    print("counts", json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
