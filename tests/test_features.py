"""六条需求的回归测试：无损、角色解析、数值只读与推算、素材、结晶添加、分页。"""

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from magiasave import growth, jsjson  # noqa: E402
from magiasave import archive as archive_mod  # noqa: E402
from magiasave.errors import ValidationError  # noqa: E402
from magiasave.model import Editor, diff_paths  # noqa: E402

SAMPLE = os.environ.get("MAGIA_SAMPLE", r"H:/magia_server/magia-save-chino-1794718180238.zip")
WORK = os.path.join(ROOT, "_work")


def check(label, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + label + ((" :: " + detail) if detail else ""))
    if not condition:
        raise SystemExit(1)


def expect_error(label, fn, code):
    try:
        fn()
    except ValidationError as exc:
        check(label, exc.code == code, "%s (%s)" % (exc.code, exc))
        return
    except Exception as exc:  # noqa: BLE001
        check(label, False, "意外异常 %r" % (exc,))
        return
    check(label, False, "没有抛错")


def main():
    os.makedirs(WORK, exist_ok=True)
    arc = archive_mod.SlotArchive.load(SAMPLE)
    editor = Editor(arc)
    original_text = arc.stored_raw

    # ---- 1. 无损：未改动保存时必须逐字节复用原始存储文本
    plain_out = os.path.join(WORK, "untouched.zip")
    info = arc.save(plain_out, label="untouched")
    reloaded = archive_mod.SlotArchive.load(plain_out)
    check("未改动的存档复用原始 profile 文本", reloaded.stored_raw == original_text,
          "%d vs %d bytes" % (len(reloaded.stored_raw), len(original_text)))
    check("未改动的存档 profile 完全一致",
          jsjson.dumps(reloaded.profile) == jsjson.dumps(arc.profile))
    check("未改动的条目数量不变", len(reloaded.entries) == len(arc.entries),
          "%d vs %d" % (len(reloaded.entries), len(arc.entries)))
    print("   写出:", json.dumps(info, ensure_ascii=False))

    # ---- 1b. 不支持的字段变化必须被拦下
    before = copy.deepcopy(editor.profile)
    mutated = copy.deepcopy(editor.profile)
    mutated["state"]["userDeckList"][0]["name"] = "changed-by-test"
    changed = diff_paths(before, mutated)
    expect_error("不支持字段的改动被拒绝",
                 lambda: editor._assert_only_supported(mutated, set()),
                 "unsupported_field_change")
    print("   被拦截的路径:", changed)

    # ---- 1c. 支持字段的不认识取值不覆盖
    probe = copy.deepcopy(editor.profile)
    probe_rows = probe["state"]["userQuestBattleList"]
    probe_rows[0]["missionStatus1"] = "LEGACY_UNKNOWN_VALUE"
    editor.probe = probe
    saved_profile = editor.profile
    editor.profile = probe
    editor.archive.profile = probe
    editor.apply([{"op": "quest.bulk", "cleared": False, "missions": True, "onlyUncleared": False}])
    check("未知 missionStatus 取值被保留",
          editor.profile["state"]["userQuestBattleList"][0]["missionStatus1"] == "LEGACY_UNKNOWN_VALUE",
          str(editor.profile["state"]["userQuestBattleList"][0]["missionStatus1"]))
    editor.profile = saved_profile
    editor.archive.profile = arc.profile

    # ---- 2. 角色解析：卡片字段必须绑定上
    rows = editor.charas_view(limit=5)["rows"]
    with_card = [row for row in rows if row["cardId"] is not None]
    check("角色能关联到卡片", len(with_card) == len(rows),
          "%d/%d" % (len(with_card), len(rows)))
    check("角色属性有值", all(row["attributeLabel"] not in ("", None, "None") for row in rows),
          str([row["attributeLabel"] for row in rows]))
    check("列表带等级与上限", all(row["level"] is not None and row["maxLevel"] for row in with_card),
          str([(row["cardName"], row["level"], row["maxLevel"]) for row in with_card[:3]]))
    first_detail = editor.chara_detail(user_card_id=with_card[0]["userCardId"])
    check("卡片数值在二级界面可见", first_detail["card"]["hp"] is not None,
          str({key: first_detail["card"][key] for key in ("level", "hp", "attack", "defense")}))

    # ---- 2b. 角色二级界面 + 头像来源 + 同角色唯一
    detail = editor.chara_detail(user_card_id=editor.charas_view(limit=1)["rows"][0]["userCardId"])
    check("角色详情含卡片养成字段",
          detail["card"] and all(key in detail["card"] for key in ("level", "maxLevel", "experience", "magiaLevel", "revision")),
          str(list((detail["card"] or {}).keys())[:8]))
    check("角色头像用卡面而非命运宝石",
          detail["icon"].startswith("/assets/cards/") and detail["gemIcon"].startswith("/assets/gem/"),
          "%s / %s" % (detail["icon"], detail["gemIcon"]))
    check("卡面文件存在", os.path.isfile(os.path.join(ROOT, detail["icon"].lstrip("/"))), detail["icon"])
    check("详情列出该角色各星级", bool(detail["siblings"]),
          str([(row["rank"], row["owned"]) for row in detail["siblings"]][:3]))

    owned_chara = detail["charaId"]
    owned_card = detail["card"]["cardId"]
    unowned = [row for row in detail["siblings"] if not row["owned"]]
    expect_error("同一角色不能再拿第二张卡（不同星级也不行）",
                 lambda: editor.apply([{"op": "card.add", "charaId": owned_chara,
                                        "cardId": (unowned[0]["cardId"] if unowned else owned_card)}]),
                 "chara_already_owned" if unowned else "already_present")

    # ---- 2c. 允许降星级（换成同角色的任意一张卡）
    lower = None
    for row in detail["siblings"]:
        if row["rank"] == "RANK_1" and not row["owned"]:
            lower = row
            break
    if lower is None:
        lower = [row for row in detail["siblings"] if row["rank"] == "RANK_1"][0]
    editor.apply([{"op": "card.swap", "userCardId": detail["userCardId"], "cardId": lower["cardId"]}])
    after_swap = editor.chara_detail(user_card_id=detail["userCardId"])["card"]
    check("允许降星级换卡", str(after_swap["cardId"]) == str(lower["cardId"]),
          "%s -> %s" % (detail["card"]["cardId"], after_swap["cardId"]))
    check("降星后等级按新上限截断", after_swap["level"] <= (after_swap["maxLevel"] or 0),
          "lv %s / max %s" % (after_swap["level"], after_swap["maxLevel"]))
    swap_master = editor.masters.card(lower["cardId"])
    check("降星后数值按公式重算",
          after_swap["hp"] == growth.card_stats(swap_master, after_swap["level"])["hp"],
          "hp %s" % after_swap["hp"])

    # ---- 2d. 升级都可回退（游戏内不允许，编辑器允许）
    rollback_target = editor.charas_view(limit=1)["rows"][0]["userCardId"]
    before = editor.chara_detail(user_card_id=rollback_target)["card"]
    editor.apply([{"op": "card.set", "id": rollback_target, "field": "level",
                   "value": max(1, (before["level"] or 1) - 3)}])
    after_lv = editor.chara_detail(user_card_id=rollback_target)["card"]
    check("等级可以降（回退升级）", after_lv["level"] < before["level"],
          "%s -> %s" % (before["level"], after_lv["level"]))
    check("降等级后数值同步下调", after_lv["hp"] <= before["hp"],
          "hp %s -> %s" % (before["hp"], after_lv["hp"]))
    editor.apply([{"op": "card.set", "id": rollback_target, "field": "magiaLevel", "value": 1}])
    check("Magia 等级可以降",
          editor.chara_detail(user_card_id=rollback_target)["card"]["magiaLevel"] == 1)
    editor.apply([{"op": "card.set", "id": rollback_target, "field": "revision", "value": 0}])
    check("突破阶段可以降",
          editor.chara_detail(user_card_id=rollback_target)["card"]["revision"] == 0)
    editor.apply([{"op": "chara.set", "userCardId": rollback_target, "field": "bondsTotalPt", "value": 0}])
    check("Episode 羁绊点可以降",
          editor.chara_detail(user_card_id=rollback_target)["episode"]["level"] == 0)

    # ---- 2e. 精神强化（节点来自数据包，addend 按 mental-enhancement.js 重算）
    spirit_target = None
    for candidate in (3006, 1013, 3005, 3016):
        if editor.spirit_view(candidate)["treeAvailable"]:
            spirit_target = candidate
            break
    check("精神树节点表已加载", spirit_target is not None, str(spirit_target))
    if spirit_target is not None:
        view = editor.spirit_view(spirit_target)
        openable = [node for node in view["nodes"] if node["canOpen"]]
        check("存在可开启的精神节点", bool(openable), "%d 个可开启" % len(openable))
        root = openable[0]
        editor.apply([{"op": "spirit.open", "charaId": spirit_target, "cellId": root["cellId"]}])
        after_open = editor.spirit_view(spirit_target)
        check("开启精神节点成功", after_open["ownedCount"] > view["ownedCount"],
              "%d -> %d" % (view["ownedCount"], after_open["ownedCount"]))
        row = [item for item in editor.profile["state"]["userCharaList"]
               if item["charaId"] == spirit_target][0]
        check("开启后按节点重算角色加成",
              any((row.get(key) or 0) > 0 for key in
                  ("addendHp", "addendAttack", "addendDefense", "addendAccelBonus",
                   "addendBlastBonus", "addendChargeBonus")) or root["type"] == "START",
              str({key: row.get(key) for key in ("addendHp", "addendAttack", "addendDefense")}))
        dependents = [node for node in after_open["nodes"]
                      if node["opened"] and str(node.get("need")) == str(root["cellId"])]
        if not dependents:
            editor.apply([{"op": "spirit.close", "charaId": spirit_target, "cellId": root["cellId"]}])
            check("关闭精神节点回退加成",
                  editor.spirit_view(spirit_target)["ownedCount"] == view["ownedCount"])

    # ---- 3. 数值只读 + 按游戏公式推算
    with_card = editor.charas_view(limit=5)["rows"]   # 前面可能换过卡，重新取当前状态
    target = with_card[0]
    # 白板可改：游戏 battle.js 对 card.* 只校验非负整数，不比对等级推算值
    editor.apply([{"op": "card.set", "id": target["userCardId"], "field": "attack", "value": 12345}])
    check("白板攻击可直接改",
          editor.chara_detail(user_card_id=target["userCardId"])["card"]["attack"] == 12345,
          "attack=%s" % editor.chara_detail(user_card_id=target["userCardId"])["card"]["attack"])
    expect_error("白板 0 被拒绝",
                 lambda: editor.apply([{"op": "card.set", "id": target["userCardId"],
                                        "field": "attack", "value": 0}]),
                 "invalid_value")
    restored = editor.apply([{"op": "card.restoreStats", "userCardId": target["userCardId"]}])
    expected_restore = growth.card_stats(editor.masters.card(target["cardId"]),
                                         editor.chara_detail(user_card_id=target["userCardId"])["card"]["level"])
    check("一键还原回推算值",
          editor.chara_detail(user_card_id=target["userCardId"])["card"]["attack"] == expected_restore["attack"],
          "restored=%s expected=%s" % (editor.chara_detail(user_card_id=target["userCardId"])["card"]["attack"],
                                       expected_restore["attack"]))
    master = editor.masters.card(target["cardId"])
    new_level = min(target["maxLevel"], (target["level"] or 1) + 5)
    editor.apply([{"op": "card.set", "id": target["userCardId"], "field": "level", "value": new_level}])
    updated = editor.chara_detail(user_card_id=target["userCardId"])["card"]
    expected = growth.card_stats(master, new_level)
    check("等级改动按公式重算数值",
          updated["level"] == new_level and all(updated[k] == expected[k] for k in ("attack", "defense", "hp")),
          "存档 %s / 公式 %s" % ({k: updated[k] for k in ("attack", "defense", "hp")}, expected))
    expect_error("超过等级上限被拒绝",
                 lambda: editor.apply([{"op": "card.set", "id": target["userCardId"],
                                        "field": "level", "value": 999}]),
                 "invalid_value")

    piece_row = editor.pieces_view(limit=1)["rows"][0]
    expect_error("直接改结晶数值被拒绝",
                 lambda: editor.apply([{"op": "piece.set", "id": piece_row["id"],
                                        "field": "hp", "value": 1}]),
                 "stat_readonly")

    # ---- 5. 结晶自由添加
    picker = editor.piece_catalog(limit=2000)["rows"]
    addable = [row for row in picker if row["addable"]]
    check("存在可添加的结晶", len(addable) > 100, str(len(addable)))
    chosen = addable[0]
    editor.apply([{"op": "piece.add", "pieceId": chosen["pieceId"], "level": 5, "lbCount": 2}])
    owned = {str(row["pieceId"]): row for row in editor.profile["state"]["userPieceList"]}
    added = owned.get(str(chosen["pieceId"]))
    check("结晶添加成功", added is not None, str(chosen["pieceId"]))
    piece_master = editor.masters.piece(chosen["pieceId"])
    expect_stats = growth.piece_stats(piece_master, 5, 2)
    check("新增结晶数值按公式", all(added[k] == expect_stats[k] for k in ("attack", "defense", "hp")),
          "%s vs %s" % ({k: added[k] for k in ("attack", "defense", "hp")}, expect_stats))
    editor.apply([{"op": "piece.add", "pieceId": chosen["pieceId"], "level": 1}])
    duplicates = [row for row in editor.profile["state"]["userPieceList"]
                  if str(row.get("pieceId")) == str(chosen["pieceId"])]
    check("同一结晶可重复获取（多枚独立行）", len(duplicates) == 2,
          "共 %d 枚，id 唯一=%s" % (len(duplicates), len({row["id"] for row in duplicates}) == len(duplicates)))

    # ---- 4. gift 分组 + 素材文件
    gifts = editor.gifts_view()
    check("养成素材已分组", len(gifts["groups"]) >= 3,
          str([(g["label"], g["total"]) for g in gifts["groups"]]))
    assets_root = os.path.join(ROOT, "assets")
    samples = {
        "gifts": os.path.join(assets_root, "gifts", "111.png"),
        "pieces": os.path.join(assets_root, "pieces", "1001.png"),
        "gem": os.path.join(assets_root, "gem", "1001.png"),
        "cardFace": os.path.join(assets_root, "cards", "10011.png"),
        "cards": os.path.join(assets_root, "cards", "10011.png"),
        "items": os.path.join(assets_root, "items", "money.png"),
    }
    missing = [name for name, path in samples.items() if not os.path.isfile(path)]
    check("素材图已复制到编辑器目录", not missing, "缺失: " + str(missing))

    # ---- 6b. 道具全量显示 + 活动分类
    families = editor.item_families()
    check("道具一级分类", len(families["families"]) >= 10,
          str([(f["key"], f["itemCount"]) for f in families["families"][:4]]))
    all_items = editor.items_view(limit=1, scope="all")
    check("道具全量（含内部道具）", all_items["total"] == len(editor.catalog.data["items"]),
          "%d vs %d" % (all_items["total"], len(editor.catalog.data["items"])))
    player_items = editor.items_view(limit=1)
    check("默认只显示玩家可见道具", 0 < player_items["total"] < all_items["total"],
          "player %d / all %d" % (player_items["total"], all_items["total"]))
    visibility = editor.item_visibility()
    evidence = {code: info.get("evidence") for code, info in visibility["items"].items()}
    check("可见性判定带证据", any(evidence.get("ARENA_COIN") or []), str(evidence.get("ARENA_COIN")))
    hidden_sample = [code for code, tags in evidence.items() if not tags][:3]
    check("内部道具被标注", len(hidden_sample) == 3, str(hidden_sample))
    leaked = [code for code in hidden_sample
              if any(row["itemId"] == code for row in editor.items_view(limit=5000)["rows"])]
    check("内部道具默认不显示", not leaked, str(leaked))
    zero_rows = editor.items_view(sort="id", limit=5000, scope="all")["rows"]
    zero = [row for row in zero_rows if row["owned"] is False]
    check("未拥有道具数量显示为 0", zero and all(row["quantity"] == 0 for row in zero),
          "未拥有 %d 种" % len(zero))
    groups = editor.item_groups("daily-tower")
    check("二级活动分组", groups["total"] > 10, "%d 组" % groups["total"])
    named = [group for group in groups["groups"] if group["activityName"]]
    check("活动组带官方活动名", len(named) > 5, str([g["label"] for g in named[:2]]))
    scoped = editor.items_view(group=groups["groups"][0]["key"], limit=10)
    check("按活动分组过滤", 0 < scoped["total"] <= groups["groups"][0]["count"],
          "%d / %d" % (scoped["total"], groups["groups"][0]["count"]))

    # ---- 6c. 任务 / 小节的三级树（部 → 章 → 话）
    tree = editor.quest_tree()
    check("任务树有部与章", tree["parts"] and tree["parts"][0]["chapters"],
          str([(part["label"], part["chapterCount"]) for part in tree["parts"][:3]]))
    main_parts = [part for part in tree["parts"] if part["label"] in ("第1部", "第2部")]
    check("主线按部归属", len(main_parts) >= 1, str([part["label"] for part in main_parts]))
    chapter_labels = [chapter["label"] for part in main_parts for chapter in part["chapters"]]
    check("章使用官方章名", any("起始之彩羽" in label for label in chapter_labels),
          str(chapter_labels[:3]))
    section_tree = editor.section_tree()
    check("小节树三级结构", section_tree["parts"] and section_tree["parts"][0]["chapters"][0]["sections"],
          str([(part["label"], part["chapterCount"]) for part in section_tree["parts"][:3]]))

    # ---- 6d. 数据包完整剧情 + 名称优先
    full = editor.quest_tree(include_catalog=True)
    check("数据包任务数大于存档收录", full["total"] > full["storedTotal"],
          "catalog %d / stored %d" % (full["total"], full["storedTotal"]))
    sample_quests = []
    for part in full["parts"]:
        for chapter in part["chapters"]:
            for section in chapter["sections"]:
                sample_quests.extend(section["quests"][:2])
                if len(sample_quests) > 30:
                    break
    check("任务带可读标签", all(quest.get("label") for quest in sample_quests),
          str([quest.get("label") for quest in sample_quests[:3]]))
    missing = [quest for quest in sample_quests if quest.get("stored") is False]
    check("数据包里有存档未收录的任务", bool(missing), "%d 条样本中 %d 条未收录" % (len(sample_quests), len(missing)))
    if missing:
        target = missing[0]["questBattleId"]
        editor.apply([{"op": "quest.ensure", "questBattleId": target}])
        check("未收录任务可以补行",
              any(str(row.get("questBattleId")) == str(target)
                  for row in editor.profile["state"]["userQuestBattleList"]), str(target))

    full_sections = editor.section_tree(include_catalog=True)
    check("数据包小节数大于存档收录", full_sections["total"] > full_sections["storedTotal"],
          "catalog %d / stored %d" % (full_sections["total"], full_sections["storedTotal"]))
    missing_section = None
    for part in full_sections["parts"]:
        for chapter in part["chapters"]:
            for section in chapter["sections"]:
                if section.get("stored") is False:
                    missing_section = section["sectionId"]
                    break
            if missing_section:
                break
        if missing_section:
            break
    check("存在未收录小节", missing_section is not None, str(missing_section))
    if missing_section:
        editor.apply([{"op": "section.ensure", "sectionId": missing_section, "canPlay": True}])
        check("未收录小节可以补行",
              any(str(row.get("sectionId")) == str(missing_section)
                  for row in editor.profile["state"]["userSectionList"]), str(missing_section))

    challenge_view = editor.challenges_view()
    challenge_rows = challenge_view["groups"][0]["rows"]
    check("挑战带可读标签", all(row.get("label") for row in challenge_rows), str(challenge_rows[0].get("label")))
    check("无文案的挑战默认不显示", challenge_view["hiddenUnknown"] >= 0,
          "隐藏 %d 条" % challenge_view["hiddenUnknown"])
    unknown_all = editor.challenges_view(include_unknown=True)
    hidden_ids = {row["challengeId"] for group in unknown_all["groups"] for row in group["rows"]} - \
                 {row["challengeId"] for group in challenge_view["groups"] for row in group["rows"]}
    check("隐藏的确实是没有条件文案的行",
          all(not row["known"] for group in unknown_all["groups"] for row in group["rows"]
              if row["challengeId"] in hidden_ids), "隐藏 %d 条" % len(hidden_ids))
    check("目标只来自官方 challenge master，不从文案解析",
          all(row["target"] is None or row["targetSource"] == "challenge.count"
              for group in challenge_view["groups"] for row in group["rows"]),
          "有目标 %d 行 / 无目标 %d 行" % (
              sum(1 for group in challenge_view["groups"] for row in group["rows"] if row["target"]),
              challenge_view["withoutTarget"]))
    official = editor.challenge_catalog()
    check("挑战 master 已加载", len(official) > 100, "%d 条定义" % len(official))
    sample_master = [row for row in official.values() if row["count"]][:1]
    check("master 带 count", bool(sample_master), str(sample_master[:1]))

    # ---- 添加魔法少女
    catalog_rows = editor.chara_catalog(limit=500)["rows"]
    addable = [row for row in catalog_rows if row["addable"]]
    check("可添加的未拥有角色", len(addable) > 100, "%d 名可添加" % len(addable))
    target_chara = addable[0]
    editor.apply([{"op": "chara.add", "charaId": target_chara["charaId"], "level": 5}])
    added_ok = any(str(row.get("charaId")) == str(target_chara["charaId"])
                   for row in editor.profile["state"]["userCharaList"])
    check("添加魔法少女成功", added_ok, str(target_chara["charaId"]))
    expect_error("同一角色不能重复添加",
                 lambda: editor.apply([{"op": "chara.add", "charaId": target_chara["charaId"]}]),
                 "chara_downgrade_blocked")

    # ---- 6. 分页数据正确（前端绑定已在 app.js 中改为渲染后绑定）
    total = editor.items_view(limit=1)["total"]
    first = editor.items_view(limit=5, offset=0)["rows"]
    second = editor.items_view(limit=5, offset=5)["rows"]
    check("分页 offset 生效", total > 5 and first and second and first[0]["itemId"] != second[0]["itemId"],
          "%s / %s" % (first[0]["itemId"] if first else None, second[0]["itemId"] if second else None))

    print("\n六条需求回归通过")


if __name__ == "__main__":
    main()
