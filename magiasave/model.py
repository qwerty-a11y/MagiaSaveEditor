"""存档语义层：视图、结构化编辑、只读字段保护。

原则（与需求一一对应）：
  1. 不支持的字段一律不动：每次提交前对编辑前后做路径级 diff，任何不在白名单内的变化都会回滚并报错。
  2. 角色数据按 userCharaList.userCardId → userCardList.id 关联（此前的 id 键错误会导致卡片信息全空）。
  3. 卡片 / 记忆结晶的 attack/defense/hp 只读，由等级与突破经 growth 模块按游戏公式推算。
  4. 记忆结晶可从已实装列表自由添加，数值同样走 growth 推算。
"""

import copy
import datetime
import json
import os
import re
import uuid

from . import growth, jsjson, paths, protocol, slot
from .catalog import STATUS_META, Catalog
from .errors import ValidationError
from .masters import Masters

_DATA_DIR = paths.data_dir()

PROTECTED_PATHS = (
    "state.user.id",
    "state.gameUser.userId",
    "state.user.userId",
    "state.user.loginName",
)

# 允许被编辑器改动的路径（其余一律视为"不支持"，不得变化）
_ALLOWED_PATTERNS = [
    r"^state\.gameUser\.(riche|level|exp|totalExpForCurrentLevel|totalExpForNextLevel|cardCapacity)$",
    r"^state\.userItemList(\[\d+\])?(\.(quantity|total))?$",
    r"^state\.userStatusList(\[\d+\])?(\.(point|checkedAt))?$",
    r"^state\.userCardList\[\d+\]\.(cardId|level|experience|magiaLevel|revision|attack|defense|hp|displayCardId|enabled|customized1|customized2|customized3|customized4|customized5|customized6)$",
    r"^state\.userCardList$",
    r"^state\.userCardList\[\d+\]$",
    r"^state\.userCharaList$",
    r"^state\.userCharaEnhancementCellList$",
    r"^state\.userCharaList\[\d+\]\.(lbItemNum|bondsTotalPt|live2dId|emotionSkillList|emotionSkillIds)$",
    r"^state\.userCharaList\[\d+\]\.(addendHp|addendAttack|addendDefense|addendAccelBonus|addendBlastBonus|addendChargeBonus)$",
    r"^state\.userCharaList\[\d+\]\.(lbItemNum|bondsTotalPt|live2dId|emotionSkillList)$",
    r"^state\.userCharaAtbEnhancementCellList$",
    r"^state\.userLive2dList(\[\d+\])?$",
    r"^state\.userFormationSheetList$",
    r"^state\.userDeckList(\[\d+\])?(\.(userCardId\d|userPieceId\d+|questPositionId\d|formationSheetId|questPositionHelper))?$",
    r"^state\.userDoppelList(\[\d+\])?$",
    r"^state\.userTotalForces(\.totalForces)?$",
    r"^state\.userPieceList(\[\d+\])?(\.(level|experience|lbCount|attack|defense|hp|protect|archive))?$",
    r"^state\.userQuestBattleList\[\d+\]\.(cleared|clearCount|rewardDone|missionStatus1|missionStatus2|missionStatus3)$",
    r"^state\.userSectionList\[\d+\]\.(canPlay|cleared)$",
    r"^state\.userChapterList(\[\d+\]\.(chapterId|cleared|canPlay|userId|createdAt))?$",
    r"^battleSession$",
    r"^battleReceipts$",
    r"^state\.gameUser\.userQuestBattleResultId$",
    r"^state\.userGiftList(\[\d+\])?(\.(quantity|total))?$",
    r"^state\.user(Daily|Limited|Total|Doppel)ChallengeList\[\d+\]\.(clearedCount|clearedAt|count)$",
    r"^state\.userGachaKindList\[\d+\]\.(totalCount|dailyCount|weeklyCount|monthlyCount)$",
    r"^state\.userGachaGroupList\[\d+\]\.(count|paid|totalCount|dailyCount|weeklyCount|monthlyCount)$",
]
_ALLOWED_RE = [re.compile(pattern) for pattern in _ALLOWED_PATTERNS]

# 默认只读（完全不被任何操作触达）的集合，用于界面明示
READONLY_COLLECTIONS = (
    "userQuestAdventureList", "userQuestBattleResultList",
"userPieceSetList", "userPieceStorageList",
    "userPieceArchiveList", "userPieceCollectionList",
    "userTitleList", "userDoppelChallengeList",
    "userPatrolList", "userArenaBattle", "archivedFriendRelations",
)

# 任务达成状态的真实值域（取自存档 userQuestBattleList.missionStatus1..3）：
#   CLEARED    已达成
#   NON_CLEAR  未达成
#   NONE       该任务没有这一条条件 / 未开放
MISSION_CLEARED = "CLEARED"
MISSION_NOT_CLEAR = "NON_CLEAR"
MISSION_NONE = "NONE"
MISSION_STATES = (MISSION_CLEARED, MISSION_NOT_CLEAR, MISSION_NONE)

GIFT_GROUPS = [
    (100, 199, "宝玉（属性觉醒）"),
    (200, 299, "书（魔力强化）"),
    (300, 399, "魔女素材·上"),
    (400, 499, "魔女素材·中"),
    (500, 509, "魔女素材·下"),
    (550, 559, "彩虹与特殊素材"),
    (600, 699, "活动 / 角色专用素材"),
    (800, 899, "感情碎片（精神力强化）"),
    (900, 999, "其他素材"),
]


def _segments(path):
    return path.split(".")


def allowed_path(path):
    for regex in _ALLOWED_RE:
        if regex.match(path):
            return True
    return False


def parse_path(path):
    """a.b[itemId=YELL].c → [(kind, ...), ...]"""
    if not path:
        return []
    segments = []
    pos = 0
    pattern = re.compile(r"([^.\[\]]+)|\[(\d+|-?\d+|[A-Za-z0-9_]+=[^\]]+)\]")
    while pos < len(path):
        match = pattern.match(path, pos)
        if not match:
            raise ValidationError("路径无法解析: %s" % path, code="invalid_path")
        key, bracket = match.group(1), match.group(2)
        if key:
            segments.append(("key", key))
        elif "=" in bracket:
            field, value = bracket.split("=", 1)
            segments.append(("match", field, value))
        else:
            segments.append(("index", int(bracket)))
        pos = match.end()
        if pos < len(path) and path[pos] == ".":
            pos += 1
    return segments


def get_value(root, path):
    node = root
    for segment in parse_path(path):
        if segment[0] == "key":
            if not isinstance(node, dict) or segment[1] not in node:
                raise ValidationError("路径不存在: %s" % path, code="path_missing")
            node = node[segment[1]]
        elif segment[0] == "index":
            if not isinstance(node, list):
                raise ValidationError("路径不是数组: %s" % path, code="path_missing")
            try:
                node = node[segment[1]]
            except IndexError:
                raise ValidationError("下标越界: %s" % path, code="path_missing")
        else:
            node = _find(node, segment[1], segment[2], path)
    return node


def _find(rows, field, expected, path="?"):
    if not isinstance(rows, list):
        raise ValidationError("路径不是数组: %s" % path, code="path_missing")
    hits = [row for row in rows if isinstance(row, dict) and str(row.get(field)) == str(expected)]
    if not hits:
        raise ValidationError("路径不存在（%s=%s）: %s" % (field, expected, path), code="path_missing")
    if len(hits) > 1:
        raise ValidationError("路径命中 %d 行（%s=%s）" % (len(hits), field, expected), code="path_ambiguous")
    return hits[0]


def set_value(root, path, value):
    segments = parse_path(path)
    if not segments:
        raise ValidationError("空路径", code="invalid_path")
    node = root
    for segment in segments[:-1]:
        if segment[0] == "key":
            if not isinstance(node, dict) or segment[1] not in node:
                raise ValidationError("路径不存在: %s" % path, code="path_missing")
            node = node[segment[1]]
        elif segment[0] == "index":
            node = node[segment[1]]
        else:
            node = _find(node, segment[1], segment[2], path)
    tail = segments[-1]
    if tail[0] == "key":
        node[tail[1]] = value
    elif tail[0] == "index":
        node[tail[1]] = value
    else:
        _find(node, tail[1], tail[2], path)[tail[1]] = value
    return value


def diff_paths(before, after, prefix="", out=None):
    """列出两棵树之间发生变化的路径（用于「不支持的字段绝不变化」断言）。"""
    if out is None:
        out = []
    if type(before) is not type(after) and not (isinstance(before, (int, float)) and isinstance(after, (int, float))):
        out.append(prefix or "$")
        return out
    if isinstance(before, dict):
        for key in before:
            child = "%s.%s" % (prefix, key) if prefix else key
            if key not in after:
                out.append(child)
            else:
                diff_paths(before[key], after[key], child, out)
        for key in after:
            if key not in before:
                child = "%s.%s" % (prefix, key) if prefix else key
                out.append(child)
        return out
    if isinstance(before, list):
        if len(before) != len(after):
            out.append(prefix or "$")
            return out
        for index in range(len(before)):
            diff_paths(before[index], after[index], "%s[%d]" % (prefix, index), out)
        return out
    if before != after:
        out.append(prefix or "$")
    return out


def _int(value, field, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValidationError("%s 必须是整数" % field, code="invalid_value")
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"-?\d+", text):
            raise ValidationError("%s 必须是整数" % field, code="invalid_value")
        number = int(text)
    else:
        number = int(value)
        if number != value:
            raise ValidationError("%s 必须是整数" % field, code="invalid_value")
    if minimum is not None and number < minimum:
        raise ValidationError("%s 不能小于 %s" % (field, minimum), code="invalid_value")
    if maximum is not None and number > maximum:
        raise ValidationError("%s 不能大于 %s" % (field, maximum), code="invalid_value")
    return number


def _mission_status(value, field="status"):
    """把布尔或三态文本统一成客户端值域（CLEARED / NON_CLEAR / NONE）。"""
    if isinstance(value, bool):
        return MISSION_CLEARED if value else MISSION_NOT_CLEAR
    text = str(value or "").strip().upper()
    if text in MISSION_STATES:
        return text
    if text in ("CLEAR", "TRUE", "1"):
        return MISSION_CLEARED
    if text in ("UNCLEAR", "FALSE", "0"):
        return MISSION_NOT_CLEAR
    raise ValidationError("%s 只能是 %s" % (field, " / ".join(MISSION_STATES)), code="invalid_value")


def _bool(value, field):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "1"):
        return True
    if isinstance(value, str) and value.lower() in ("false", "0"):
        return False
    raise ValidationError("%s 必须是布尔值" % field, code="invalid_value")


class Editor:
    def __init__(self, archive, catalog=None, masters=None):
        self.archive = archive
        self.catalog = catalog or Catalog()
        self.masters = masters or Masters()
        self.profile = archive.profile
        self.dirty = False
        self.original = jsjson.dumps(archive.profile)

    # ------------------------------------------------------------ 基础

    @property
    def state(self):
        return self.profile["state"]

    def stamp(self):
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y/%m/%d %H:%M:%S")

    def rows(self, name):
        value = self.state.get(name)
        return value if isinstance(value, list) else []

    def reset(self, profile=None):
        self.profile = profile if profile is not None else self.archive.profile
        self.dirty = jsjson.dumps(self.profile) != self.original

    # ------------------------------------------------------------ 概览

    def overview(self):
        user = self.state.get("user") or {}
        game_user = self.state.get("gameUser") or {}
        items = {row.get("itemId"): row for row in self.rows("userItemList")}
        quests = self.rows("userQuestBattleList")
        sections = self.rows("userSectionList")
        chapters = self.rows("userChapterList")

        def quantity(code):
            row = items.get(code) or {}
            return row.get("quantity")

        session = self.profile.get("battleSession")
        battle = {
            "stuck": bool(session) or bool(game_user.get("userQuestBattleResultId")),
            "resultId": game_user.get("userQuestBattleResultId"),
            "receiptCount": len(self.profile.get("battleReceipts") or []),
        }
        if isinstance(session, dict):
            record = session.get("record") or {}
            quest_id = record.get("questBattleId")
            quest = (self.quest_catalog().get("quests") or {}).get(str(quest_id)) or {}
            section_id = quest.get("sectionId")
            meta = self.catalog.section(section_id) if section_id else {}
            battle.update({
                "sessionId": session.get("id"),
                "phase": session.get("phase"),
                "intent": session.get("intent"),
                "questBattleId": quest_id,
                "questName": meta.get("title") if isinstance(meta, dict) else None,
                "createdAt": record.get("createdAt"),
            })

        return {
            "archive": self.archive.summary(),
            "battle": battle,
            "player": {
                "loginName": user.get("loginName"),
                "userId": game_user.get("userId"),
                "level": game_user.get("level"),
                "exp": game_user.get("exp"),
                "expForCurrent": game_user.get("totalExpForCurrentLevel"),
                "expForNext": game_user.get("totalExpForNextLevel"),
                "riche": game_user.get("riche"),
                "inviteCode": game_user.get("inviteCode"),
                "comment": game_user.get("comment"),
                "cardCapacity": game_user.get("cardCapacity"),
                "tutorialId": user.get("tutorialId"),
                "createdAt": game_user.get("createdAt"),
                "deckType": game_user.get("deckType"),
            },
            "currency": {
                "riche": game_user.get("riche"),
                "money": quantity("MONEY"),
                "presentedMoney": quantity("PRESENTED_MONEY"),
                "yell": quantity("YELL"),
                "arenaCoin": quantity("ARENA_COIN"),
                "prism": quantity("PRISM"),
                "gachaTicket": quantity("GACHA_TICKET"),
                "sceneZeroTicket": quantity("SCENEZERO_PLAY_TICKET"),
            },
            "status": [
                {"statusId": row.get("statusId"), "label": self.catalog.status_label(row.get("statusId")),
                 "point": row.get("point")}
                for row in sorted(self.rows("userStatusList"), key=lambda r: str(r.get("statusId")))
            ],
            "counts": {
                "chara": len(self.rows("userCharaList")),
                "card": len(self.rows("userCardList")),
                "piece": len(self.rows("userPieceList")),
                "item": len(items),
                "quest": len(quests),
                "questCleared": sum(1 for row in quests if row.get("cleared") is True),
                "section": len(sections),
                "sectionCanPlay": sum(1 for row in sections if row.get("canPlay") is True),
                "chapter": len(chapters),
                "chapterCleared": sum(1 for row in chapters if row.get("cleared") is True),
                "gift": len(self.rows("userGiftList")),
            },
            "readonly": {
                "collections": [name for name in READONLY_COLLECTIONS if name in self.state],
                "cardStats": ["attack", "defense", "hp"],
                "pieceStats": ["attack", "defense", "hp"],
                "grownBy": "等级/突破/经验经 growth 模块按游戏公式推算",
            },
            "masters": self.masters.stats(),
            "catalog": self.catalog.stats(),
            "profiles": {"topKeys": list(self.profile.keys()), "stateKeys": sorted(self.state.keys())},
        }

    # ------------------------------------------------------------ 列表

    def item_layout(self):
        """道具分类索引（tools/build_item_groups.py 生成）。"""
        if getattr(self, "_item_layout_cache", None) is None:
            path = os.path.join(_DATA_DIR, "item-groups.json")
            try:
                with open(path, encoding="utf-8") as handle:
                    self._item_layout_cache = json.load(handle)
            except Exception:
                self._item_layout_cache = {"items": {}, "groups": {}, "families": {}}
        return self._item_layout_cache

    def item_visibility(self):
        """道具可见性判定（tools/build_item_visibility.py 生成）：player / internal 与逐条证据。"""
        if getattr(self, "_item_visibility_cache", None) is None:
            path = os.path.join(_DATA_DIR, "item-visibility.json")
            try:
                with open(path, encoding="utf-8") as handle:
                    self._item_visibility_cache = json.load(handle)
            except Exception:
                self._item_visibility_cache = {"items": {}, "counts": {}}
        return self._item_visibility_cache

    def _is_player_item(self, code):
        info = (self.item_visibility().get("items") or {}).get(str(code))
        if info is None:
            return True
        return info.get("scope") == "player"

    def item_families(self, scope="player"):
        """一级分类：类型（通用素材 / 每日塔 / 分支活动 / 扭蛋活动 …）。"""
        layout = self.item_layout()
        owned = {str(row.get("itemId")) for row in self.rows("userItemList")}
        owned_by_family = {}
        count_by_family = {}
        group_by_family = {}
        for code, info in layout["items"].items():
            if scope == "player" and not self._is_player_item(code):
                continue
            key = info.get("familyKey")
            count_by_family[key] = count_by_family.get(key, 0) + 1
            if code in owned:
                owned_by_family[key] = owned_by_family.get(key, 0) + 1
        for code, info in layout["items"].items():
            if scope == "player" and not self._is_player_item(code):
                continue
            group_by_family.setdefault(info.get("familyKey"), set()).add(info.get("groupKey"))
        rows = []
        for key, meta in (layout.get("families") or {}).items():
            if not count_by_family.get(key):
                continue
            rows.append({"key": key, "label": meta.get("label"), "order": meta.get("order", 999),
                         "itemCount": count_by_family[key],
                         "groupCount": len(group_by_family.get(key) or ()),
                         "owned": owned_by_family.get(key, 0)})
        rows.sort(key=lambda item: (item["order"], item["label"]))
        return {"families": rows, "totalItems": sum(item["itemCount"] for item in rows), "scope": scope}

    def item_groups(self, family=None, scope="player"):
        """二级分类：某类型下的活动（活动号 + 官方活动名）。"""
        layout = self.item_layout()
        owned = {str(row.get("itemId")) for row in self.rows("userItemList")}
        groups = {}
        for code, info in (layout.get("items") or {}).items():
            if family and info.get("familyKey") != family:
                continue
            if scope == "player" and not self._is_player_item(code):
                continue
            bucket = groups.setdefault(info.get("groupKey"), {
                "key": info.get("groupKey"), "label": info.get("groupLabel"),
                "order": info.get("groupOrder", 999), "family": info.get("familyKey"),
                "activityId": info.get("activityId"), "activityName": info.get("activityName"),
                "count": 0, "owned": 0})
            bucket["count"] += 1
            if code in owned:
                bucket["owned"] += 1
        rows = sorted(groups.values(), key=lambda item: (item["order"], item["label"]))
        return {"groups": rows, "total": len(rows)}

    def items_view(self, query=None, offset=0, limit=200, sort="id", group=None, family=None, scope="player"):
        """全部已实装道具（不只已拥有的）：未拥有显示数量 0；可按活动分组过滤。"""
        owned = {}
        for row in self.rows("userItemList"):
            owned[str(row.get("itemId"))] = row
        layout = self.item_layout().get("items") or {}
        codes = set(self.catalog.data["items"].keys()) | set(owned.keys())
        rows = []
        for code in codes:
            info = layout.get(str(code)) or {"familyKey": "general", "groupKey": "general:other",
                                             "groupLabel": "其他 / 未分类", "familyLabel": "通用 / 系统"}
            if group and info.get("groupKey") != group:
                continue
            if family and info.get("familyKey") != family:
                continue
            if scope == "player" and not self._is_player_item(code):
                continue
            meta = self.catalog.item(code)
            row = owned.get(str(code)) or {}
            rows.append({
                "itemId": code,
                "name": meta.get("name") or code,
                "type": meta.get("type"),
                "unit": meta.get("unit"),
                "description": meta.get("desc"),
                "quantity": row.get("quantity") if row else 0,
                "total": row.get("total") if row else 0,
                "owned": bool(row),
                "environmentId": row.get("environmentId"),
                "createdAt": row.get("createdAt"),
                "icon": "/assets/items/%s.png" % str(code).lower(),
                "groupKey": info.get("groupKey"),
                "groupLabel": info.get("groupLabel"),
                "familyKey": info.get("familyKey"),
                "familyLabel": info.get("familyLabel"),
                "activityId": info.get("activityId"),
            })
        if query:
            needle = query.lower()
            rows = [row for row in rows
                    if needle in str(row["itemId"]).lower() or needle in str(row["name"]).lower()]
        if sort == "name":
            rows.sort(key=lambda row: (str(row["name"]), str(row["itemId"])))
        elif sort == "qty":
            rows.sort(key=lambda row: (-(row.get("quantity") or 0), str(row["name"]), str(row["itemId"])))
        else:
            # 默认按 ID 排序（itemCode 字符串序），避免数量为零的条目散落各处
            rows.sort(key=lambda row: str(row["itemId"]))
        return {"total": len(rows), "offset": offset, "limit": limit, "rows": rows[offset: offset + limit],
                "sort": sort}

    def charas_view(self, query=None, offset=0, limit=200):
        """一级界面：只列魔法少女（卡面作头像），点进 chara_detail 看详细参数。"""
        cards = {}
        for row in self.rows("userCardList"):
            key = row.get("id")
            if key is not None:
                cards[key] = row
        rows = []
        for row in self.rows("userCharaList"):
            chara_id = row.get("charaId")
            meta = self.catalog.chara(chara_id)
            display_name = self.catalog.chara_name(chara_id)
            card = cards.get(row.get("userCardId")) or {}
            master = self.masters.card(card.get("cardId"))
            cap = None
            if master:
                try:
                    cap = growth.card_max_level(master.get("rank"))
                except growth.GrowthError:
                    cap = None
            rows.append({
                "charaId": chara_id,
                "userCardId": row.get("userCardId"),
                "name": display_name,
                "title": meta.get("title"),
                "attribute": meta.get("attribute"),
                "attributeLabel": self.catalog.attribute_label(meta.get("attribute")),
                "cardId": card.get("cardId"),
                "cardName": self.catalog.card_name(card.get("cardId")),
                "cardRank": (master or {}).get("rank"),
                "level": card.get("level"),
                "maxLevel": cap,
                "magiaLevel": card.get("magiaLevel"),
                "lbItemNum": row.get("lbItemNum"),
                "bondsTotalPt": row.get("bondsTotalPt"),
                "icon": "/assets/cards/%s.png" % card.get("cardId"),
                "face": "/assets/faces/%s.png" % card.get("cardId"),
                "gemIcon": "/assets/gem/%s.png" % chara_id,
                "editable": bool(master),
            })
        if query:
            needle = query.lower()
            rows = [row for row in rows
                    if needle in str(row["charaId"]) or needle in str(row["name"]).lower()
                    or needle in str(row["cardName"]).lower()]
        rows.sort(key=lambda row: (row.get("charaId") or 0))
        return {"total": len(rows), "offset": offset, "limit": limit, "rows": rows[offset: offset + limit]}

    ATTRIBUTE_LABEL = {"FIRE": "火", "WATER": "水", "TIMBER": "木", "LIGHT": "光", "DARK": "暗", "VOID": "无"}
    ATTRIBUTE_STATS = ("HP", "ATTACK", "DEFENSE")

    def _data(self, name, default):
        """按需加载 data/ 下的官方 master 导出（APK MCATBIN1 解析产物）。"""
        cache = self.__dict__.setdefault("_data_cache", {})
        if name not in cache:
            try:
                with open(os.path.join(_DATA_DIR, name), encoding="utf-8") as handle:
                    cache[name] = json.load(handle)
            except Exception:
                cache[name] = default
        return cache[name]

    def attribute_catalog(self):
        return self._data("attribute.json", {"cells": {}, "charaAttribute": {}, "itemNames": {}})

    def awakening_catalog(self):
        return self._data("awakening.json", {"cards": {}, "charas": {}, "limitBreak": {}, "awakening": {}})

    def gift_index(self):
        return {int(row.get("giftId")): row for row in self.rows("userGiftList")
                if isinstance(row.get("giftId"), int)}

    def item_index(self):
        return {str(row.get("itemId")): row for row in self.rows("userItemList")}

    def spirit_catalog(self):
        """精神强化：APK master（MCATBIN1）的 23,248 个节点，含连线、开启消耗与情感技能定义。"""
        if getattr(self, "_spirit_cache", None) is None:
            path = os.path.join(_DATA_DIR, "spirit.json")
            try:
                with open(path, encoding="utf-8") as handle:
                    self._spirit_cache = json.load(handle)
            except Exception:
                self._spirit_cache = {"cells": {}, "groups": {}, "charaGroup": {}}
        return self._spirit_cache

    SPIRIT_FIELDS = {
        "HP": "addendHp",
        "ATTACK": "addendAttack",
        "DEFENSE": "addendDefense",
        "DISK_ACCELE": "addendAccelBonus",
        "DISK_BLAST": "addendBlastBonus",
        "DISK_CHARGE": "addendChargeBonus",
    }

    # ------------------------------------------------------------ 通用取值

    def _find_chara_row(self, profile, chara_id):
        for row in profile["state"].get("userCharaList") or []:
            if str(row.get("charaId")) == str(chara_id):
                return row
        raise ValidationError("找不到角色: %r" % (chara_id,), code="path_missing")

    def _find_card_row(self, profile, user_card_id=None, chara_row=None):
        target = user_card_id if user_card_id is not None else (chara_row or {}).get("userCardId")
        for row in profile["state"].get("userCardList") or []:
            if str(row.get("id")) == str(target):
                return row
        raise ValidationError("找不到卡片: %r" % (user_card_id,), code="path_missing")

    def _add_forces(self, profile, delta):
        if not delta:
            return
        holder = profile["state"].get("userTotalForces")
        if not isinstance(holder, dict):
            raise ValidationError("存档缺少 userTotalForces，无法结算总战力", code="path_missing")
        holder["totalForces"] = int(holder.get("totalForces") or 0) + int(delta)

    def _chara_field(self, chara_id, field):
        for row in self.rows("userCharaList"):
            if str(row.get("charaId")) == str(chara_id):
                return row.get(field)
        return None

    # ------------------------------------------------------------ 精神强化

    def spirit_view(self, chara_id):
        """精神树：节点带坐标（pointX/pointY）与连线（connectedCellIdList），供前端原样绘制。

        消耗只作展示：本编辑器的开关操作不扣材料（见 README「操作不改持有材料」）。
        """
        catalog = self.spirit_catalog()
        group_id = (catalog.get("charaGroup") or {}).get(str(chara_id))
        cells = catalog.get("cells") or {}
        group_ids = (catalog.get("groups") or {}).get(str(group_id), []) or []
        owned = [row for row in self.rows("userCharaEnhancementCellList")
                 if str(row.get("charaId")) == str(chara_id)]
        owned_ids = {str(row.get("charaEnhancementCellId")) for row in owned}
        gifts = self.gift_index()
        riche = int(self.state.get("gameUser", {}).get("riche") or 0)
        nodes = []
        xs, ys = [], []
        for cell_id in group_ids:
            cell = cells.get(str(cell_id))
            if not cell:
                continue
            need = int(cell.get("need") or 0)
            cc = int(cell.get("needCC") or 0)
            cost = []
            for gift in cell.get("gifts") or []:
                gid = int(gift.get("id") or 0)
                quantity = int(gift.get("quantity") or 0)
                if not gid or not quantity:
                    continue
                have = int((gifts.get(gid) or {}).get("quantity") or 0)
                cost.append({"giftId": gid, "name": self.catalog.gift_name(gid), "need": quantity,
                             "have": have, "enough": have >= quantity})
            skill = cell.get("skill")
            definition = (skill or {}).get("definition") or {}
            localized = self.catalog.emotion_skill(skill.get("id")) if skill else {}
            x = cell.get("x")
            y = cell.get("y")
            if isinstance(x, (int, float)):
                xs.append(x)
            if isinstance(y, (int, float)):
                ys.append(y)
            opened = str(cell_id) in owned_ids
            nodes.append({
                "cellId": cell.get("cellId"),
                "type": cell.get("type"),
                "value": cell.get("value"),
                "field": self.SPIRIT_FIELDS.get(cell.get("type")),
                "need": need or None,
                "needCC": cc,
                "cost": cost,
                "x": x,
                "y": y,
                "links": cell.get("links") or [],
                "skill": None if not skill else {
                    "id": skill.get("id"),
                    "name": localized.get("name") or definition.get("name"),
                    "type": definition.get("type"),
                    "description": localized.get("shortDescription") or definition.get("shortDescription"),
                    "artList": skill.get("artList"),
                    "wired": skill.get("wired"),
                },
                "opened": opened,
                "canOpen": not opened and (not need or str(need) in owned_ids),
                "skillLocked": bool(skill and not skill.get("wired")),
            })
        nodes.sort(key=lambda item: item.get("cellId") or 0)
        return {
            "groupId": group_id,
            "treeAvailable": bool(nodes),
            "cellCount": len(nodes),
            "ownedCount": len(owned_ids),
            "skillCount": sum(1 for node in nodes if node.get("skill")),
            "riche": riche,
            "nodes": nodes,
            "bounds": {
                "minX": min(xs) if xs else 0, "maxX": max(xs) if xs else 0,
                "minY": min(ys) if ys else 0, "maxY": max(ys) if ys else 0,
            },
            "reason": None if nodes else ("该角色的精神组 %s 不在 master 里" % group_id),
            "totals": {
                field: self._chara_field(chara_id, field) for field in self.SPIRIT_FIELDS.values()
            },
        }

    def _spirit_apply(self, profile, chara_row, extra):
        """照 mental-enhancement.js summary() 重算 addend*、emotionSkillList 与 emotionSkillIds。"""
        catalog = self.spirit_catalog()
        cells = catalog.get("cells") or {}
        chara_id = str(chara_row.get("charaId"))
        totals = {field: 0 for field in self.SPIRIT_FIELDS.values()}
        skill_defs, skill_ids = [], []
        force = 0
        for row in profile["state"].get("userCharaEnhancementCellList") or []:
            if str(row.get("charaId")) != chara_id:
                continue
            cell = cells.get(str(row.get("charaEnhancementCellId"))) or {}
            kind = cell.get("type")
            value = cell.get("value") if isinstance(cell.get("value"), int) else 0
            field = self.SPIRIT_FIELDS.get(kind)
            if field:
                totals[field] += value
                force += 100 if str(kind).startswith("DISK_") else value
            elif kind == "SKILL":
                skill = cell.get("skill") or {}
                if skill.get("id") is not None:
                    skill_defs.append(copy.deepcopy(skill.get("definition") or {}))
                    skill_ids.append(str(skill.get("id")))
                force += 100
        index = profile["state"]["userCharaList"].index(chara_row)
        for field, value in totals.items():
            chara_row[field] = value
            extra.add("state.userCharaList[%d].%s" % (index, field))
        chara_row["emotionSkillList"] = skill_defs
        chara_row["emotionSkillIds"] = ",".join(skill_ids)
        extra.add("state.userCharaList[%d].emotionSkillList" % index)
        extra.add("state.userCharaList[%d].emotionSkillIds" % index)
        return force

    def _spirit_ordered(self, cells, opened, wanted):
        """按 needOpenedCellId 展开前置，返回可依次开启的节点序列。"""
        ordered, visiting = [], set()
        present = set(opened)

        def push(cell):
            key = str(cell.get("cellId"))
            if key in present:
                return
            if key in visiting:
                raise ValidationError("精神树在节点 %s 处成环" % key, code="spirit_cycle")
            visiting.add(key)
            need = cell.get("need")
            if need:
                parent = cells.get(str(need))
                if parent is None:
                    raise ValidationError("前置节点 %s 不在 master 里" % need,
                                          code="spirit_prerequisite_missing")
                push(parent)
            visiting.discard(key)
            if key not in present:
                present.add(key)
                ordered.append(cell)

        for cell in wanted:
            push(cell)
        return ordered

    def _op_spirit_open(self, profile, op, _force, extra):
        chara_row = self._find_chara_row(profile, op.get("charaId"))
        chara_id = chara_row.get("charaId")
        catalog = self.spirit_catalog()
        cells = catalog.get("cells") or {}
        group_id = (catalog.get("charaGroup") or {}).get(str(chara_id))
        rows = profile["state"].setdefault("userCharaEnhancementCellList", [])
        opened = {str(row.get("charaEnhancementCellId")) for row in rows
                  if str(row.get("charaId")) == str(chara_id)}
        wanted = op.get("cellIds")
        if not wanted and op.get("cellId") is not None:
            wanted = [op.get("cellId")]
        if not isinstance(wanted, list) or not wanted:
            raise ValidationError("需要 cellId 或 cellIds", code="invalid_argument")
        pending = []
        for raw in wanted:
            cid = int(raw)
            cell = cells.get(str(cid))
            if cell is None:
                raise ValidationError("没有精神节点 %s" % cid, code="path_missing")
            if group_id is not None and str(cell.get("groupId")) != str(group_id):
                raise ValidationError("节点 %s 不属于该角色的精神树" % cid, code="spirit_group_mismatch")
            skill = cell.get("skill")
            if cell.get("type") == "SKILL" and skill and not skill.get("wired"):
                raise ValidationError("情感技能 %s 在客户端未接线，无法开启" % skill.get("id"),
                                      code="spirit_skill_unwired")
            if str(cid) in opened:
                continue
            pending.append(cell)
        ordered = self._spirit_ordered(cells, opened, pending)
        if not ordered:
            return
        stamp = self.stamp()
        for cell in ordered:
            rows.append({
                "userId": profile["state"]["gameUser"]["userId"],
                "charaId": chara_id,
                "charaEnhancementCellId": cell.get("cellId"),
                "enhancementCellStatus": "OPENED",
                "isReturnGift20200518": True,
                "createdAt": stamp,
            })
        extra.add("state.userCharaEnhancementCellList")
        force = self._spirit_apply(profile, chara_row, extra)
        return force

    def _op_spirit_close(self, profile, op, _force, extra):
        chara_row = self._find_chara_row(profile, op.get("charaId"))
        chara_id = chara_row.get("charaId")
        catalog = self.spirit_catalog()
        cells = catalog.get("cells") or {}
        rows = profile["state"].get("userCharaEnhancementCellList") or []
        owned = [row for row in rows if str(row.get("charaId")) == str(chara_id)]
        if op.get("all"):
            targets = [row for row in owned
                       if (cells.get(str(row.get("charaEnhancementCellId"))) or {}).get("type") != "START"]
        else:
            cid = op.get("cellId")
            if cid is None:
                raise ValidationError("需要 cellId 或 all=true", code="invalid_argument")
            targets = [row for row in owned if str(row.get("charaEnhancementCellId")) == str(cid)]
            if not targets:
                raise ValidationError("该节点未开启", code="path_missing")
        target_ids = {str(row.get("charaEnhancementCellId")) for row in targets}
        remaining = {str(row.get("charaEnhancementCellId")) for row in owned} - target_ids
        for cell_id in remaining:
            cell = cells.get(cell_id) or {}
            if str(cell.get("need")) in target_ids:
                raise ValidationError("节点 %s 依赖它，需先关闭后者" % cell_id, code="spirit_has_dependents")
        for row in targets:
            rows.remove(row)
        extra.add("state.userCharaEnhancementCellList")
        self._spirit_apply(profile, chara_row, extra)

    # ------------------------------------------------------------ 属性强化

    def chara_detail(self, user_card_id=None, chara_id=None):
        """二级界面：一个魔法少女的全部养成参数（可编辑项 + 只读推算项）。"""
        chara_row = None
        for row in self.rows("userCharaList"):
            if user_card_id and row.get("userCardId") == user_card_id:
                chara_row = row
                break
            if chara_id is not None and str(row.get("charaId")) == str(chara_id):
                chara_row = row
                break
        if chara_row is None:
            raise ValidationError("找不到角色: %r" % (chara_id or user_card_id,), code="path_missing")
        card = None
        for row in self.rows("userCardList"):
            if row.get("id") == chara_row.get("userCardId"):
                card = row
                break
        meta = self.catalog.chara(chara_row.get("charaId"))
        master = self.masters.card(card.get("cardId")) if card else None
        rank = (master or {}).get("rank")
        cap = None
        exp_cap = None
        if master and card:
            try:
                cap = growth.card_max_level(rank)
                exp_cap = growth.card_experience_cap(card.get("level") or 1, rank)
            except growth.GrowthError:
                cap = None
        siblings = []
        if master and master.get("charaNo") is not None:
            owned = {row.get("cardId") for row in self.rows("userCardList")}
            for key, row in (self.masters.cards or {}).items():
                if str(row.get("charaNo")) != str(master.get("charaNo")):
                    continue
                siblings.append({
                    "cardId": row.get("cardId"),
                    "rank": row.get("rank"),
                    "name": row.get("cardName") or self.catalog.card_name(row.get("cardId")),
                    "owned": row.get("cardId") in owned,
                })
            siblings.sort(key=lambda item: str(item.get("rank")))
        return {
            "charaId": chara_row.get("charaId"),
            "userCardId": chara_row.get("userCardId"),
            "name": self.catalog.chara_name(chara_row.get("charaId")),
            "kana": meta.get("kana"),
            "attribute": meta.get("attribute"),
            "attributeLabel": self.catalog.attribute_label(meta.get("attribute")),
            "school": meta.get("school"),
            "title": meta.get("title"),
            "icon": "/assets/cards/%s.png" % (card or {}).get("cardId"),
            "face": "/assets/faces/%s.png" % (card or {}).get("cardId"),
            "gemIcon": "/assets/gem/%s.png" % chara_row.get("charaId"),
            "chara": {
                "lbItemNum": chara_row.get("lbItemNum"),
                "bondsTotalPt": chara_row.get("bondsTotalPt"),
                "live2dId": chara_row.get("live2dId"),
                "emotionSkillList": chara_row.get("emotionSkillList"),
                "visualizable": chara_row.get("visualizable"),
                "createdAt": chara_row.get("createdAt"),
            },
            "charaReadonly": {
                "addendAttack": chara_row.get("addendAttack"),
                "addendDefense": chara_row.get("addendDefense"),
                "addendHp": chara_row.get("addendHp"),
                "addendAccelBonus": chara_row.get("addendAccelBonus"),
                "addendBlastBonus": chara_row.get("addendBlastBonus"),
                "addendChargeBonus": chara_row.get("addendChargeBonus"),
            },
            "card": None if card is None else {
                "id": card.get("id"),
                "cardId": card.get("cardId"),
                "cardName": self.catalog.card_name(card.get("cardId")),
                "rank": rank,
                "level": card.get("level"),
                "maxLevel": cap,
                "experience": card.get("experience"),
                "experienceCap": exp_cap,
                "magiaLevel": card.get("magiaLevel"),
                "revision": card.get("revision"),
                "enabled": card.get("enabled"),
                "displayCardId": card.get("displayCardId"),
                "customized": [card.get("customized%d" % i) for i in range(1, 7)],
                "attack": card.get("attack"),
                "defense": card.get("defense"),
                "hp": card.get("hp"),
                "createdAt": card.get("createdAt"),
                "hasMaster": bool(master),
            },
            "spirit": self.spirit_view(chara_row.get("charaId")),
            "doppel": self.doppel_view(chara_row.get("charaId")),
            "costumes": self.costumes_view(chara_row.get("charaId")),
            "stories": self.stories_view(chara_row.get("charaId")),
            "episode": {
                "points": (chara_row or {}).get("bondsTotalPt"),
                "level": growth.episode_level(chara_row.get("bondsTotalPt") or 0),
                "next": growth.episode_next(chara_row.get("bondsTotalPt") or 0),
                "thresholds": growth.EPISODE_THRESHOLDS,
            },
            "siblings": siblings,
        }

    def live2d_master(self):
        return self._data("live2d.json", {"chara": {}})

    def live2d_ids(self, chara_id):
        """主数据（MCATBIN1 tables.live2d）里该角色真实存在的衣装编号。"""
        entry = (self.live2d_master().get("chara") or {}).get(str(chara_id))
        if entry is None:
            return None
        return set(entry.keys())

    def costumes_catalog(self):
        return self._data("costumes.json", {"charaCostumes": {}})

    def costumes_view(self, chara_id):
        """衣装列表：默认服装（live2dId 00）永远算已拥有且不可取消。"""
        entries = (self.costumes_catalog().get("charaCostumes") or {}).get(str(chara_id)) or []
        master_ids = self.live2d_ids(chara_id)
        owned = {str(row.get("live2dId")) for row in self.rows("userLive2dList")
                 if str(row.get("charaId")) == str(chara_id)}
        chara_row = None
        for row in self.rows("userCharaList"):
            if str(row.get("charaId")) == str(chara_id):
                chara_row = row
                break
        current = str((chara_row or {}).get("live2dId"))
        rows = []
        for entry in entries:
            live2d_id = str(entry.get("live2dId"))
            is_default = bool(entry.get("default")) or live2d_id == "00"
            rows.append({
                "live2dId": live2d_id,
                "name": entry.get("name"),
                "default": is_default,
                "owned": is_default or live2d_id in owned,
                "current": live2d_id == current,
                "locked": is_default,
                # libs 的衣装清单可能比主数据多（如 1015 的 51），这类编号写进存档会让客户端读不出来
                "invalidMaster": bool(master_ids is not None and live2d_id not in master_ids),
            })
        known = {row["live2dId"] for row in rows}
        for live2d_id in sorted(owned - known):
            rows.append({"live2dId": live2d_id, "name": "未知衣装 %s" % live2d_id,
                         "default": live2d_id == "00", "owned": True,
                         "current": live2d_id == current, "locked": live2d_id == "00",
                         "unknown": True})
        return {
            "total": len(rows),
            "ownedCount": sum(1 for row in rows if row["owned"]),
            "current": current,
            "rows": rows,
        }

    def attribute_view(self, chara_id):
        """属性强化：72 个 cell（6 属性 × 3 项 × 4 段），效果作用于同属性全体角色。"""
        catalog = self.attribute_catalog()
        cells = catalog.get("cells") or {}
        by_chara = catalog.get("charaAttribute") or {}
        chara_row = None
        for row in self.rows("userCharaList"):
            if str(row.get("charaId")) == str(chara_id):
                chara_row = row
                break
        if chara_row is None:
            raise ValidationError("找不到角色: %r" % (chara_id,), code="path_missing")
        attribute = by_chara.get(str(chara_id))
        card = None
        for row in self.rows("userCardList"):
            if str(row.get("id")) == str(chara_row.get("userCardId")):
                card = row
                break
        revision = int((card or {}).get("revision") or 0)
        owned_rows = [row for row in self.rows("userCharaAtbEnhancementCellList")
                      if str(row.get("charaId")) == str(chara_id)]
        owned = {str(row.get("atbEnhancementCellId")) for row in owned_rows}
        status = {}
        for row in self.rows("userStatusList"):
            status[str(row.get("statusId"))] = row.get("point")
        items = self.item_index()
        members = []
        for row in self.rows("userCharaList"):
            if by_chara.get(str(row.get("charaId"))) != attribute:
                continue
            members.append({
                "charaId": row.get("charaId"),
                "name": self.catalog.chara_name(row.get("charaId")),
                "userCardId": row.get("userCardId"),
                "icon": "/assets/faces/%s.png" % self._card_id_of(row.get("userCardId")),
            })
        order = catalog.get("bulkOrder") or list(self.ATTRIBUTE_STATS)
        rows_out = []
        for cid, cell in cells.items():
            if cell.get("attribute") != attribute:
                continue
            cost = []
            for code, quantity in sorted((cell.get("cost") or {}).items()):
                row = items.get(code) or {}
                have = int(row.get("quantity") or 0)
                cost.append({"code": code, "name": self.catalog.item_name(code), "need": quantity,
                             "have": have, "enough": have >= quantity})
            rows_out.append({
                "cellId": cell.get("cellId"),
                "type": cell.get("type"),
                "stat": cell.get("stat"),
                "statLabel": {"HP": "HP", "ATTACK": "攻击", "DEFENSE": "防御"}.get(cell.get("stat")),
                "value": cell.get("value"),
                "revision": cell.get("revision"),
                "revisionOk": int(cell.get("revision") or 0) <= revision,
                "cost": cost,
                "opened": str(cid) in owned,
                "point": status.get(cell.get("type")),
                "forceEach": cell.get("value"),
                "forceTotal": int(cell.get("value") or 0) * len(members),
            })
        rows_out.sort(key=lambda item: (order.index(item.get("stat")) if item.get("stat") in order else 9,
                                        item.get("revision") or 0, item.get("cellId") or 0))
        return {
            "charaId": chara_id,
            "userCardId": chara_row.get("userCardId"),
            "attribute": attribute,
            "attributeLabel": self.ATTRIBUTE_LABEL.get(attribute, attribute),
            "cardRevision": revision,
            "members": members,
            "memberCount": len(members),
            "openedCount": len(owned),
            "cellTotal": len(rows_out),
            "cells": rows_out,
            "status": {key: value for key, value in status.items()
                       if key and (key.split("_")[0] == attribute)},
            "totalForces": (self.state.get("userTotalForces") or {}).get("totalForces"),
            "revision": revision,
        }

    def _card_id_of(self, user_card_id):
        for row in self.rows("userCardList"):
            if str(row.get("id")) == str(user_card_id):
                return row.get("cardId")
        return None

    def _asset_url(self, group, name):
        path = os.path.join(os.path.dirname(_DATA_DIR), "assets", group, name)
        return "/assets/%s/%s" % (group, name) if os.path.isfile(path) else None

    def _attribute_context(self, profile, chara_id):
        catalog = self.attribute_catalog()
        by_chara = catalog.get("charaAttribute") or {}
        chara_row = self._find_chara_row(profile, chara_id)
        card = self._find_card_row(profile, chara_row=chara_row)
        attribute = by_chara.get(str(chara_row.get("charaId")))
        if not attribute:
            raise ValidationError("角色 %s 没有属性记录" % chara_id, code="path_missing")
        return catalog, chara_row, card, attribute

    def attribute_bonus(self, attribute):
        """属性强化的全局加成：userStatusList 里 <ATTR>_HP / _ATTACK / _DEFENSE 的累计值。"""
        out = {"hp": 0, "attack": 0, "defense": 0}
        if not attribute:
            return out
        mapping = {"HP": "hp", "ATTACK": "attack", "DEFENSE": "defense"}
        for row in self.rows("userStatusList"):
            prefix, _, stat = str(row.get("statusId") or "").partition("_")
            if prefix != attribute:
                continue
            key = mapping.get(stat)
            if key:
                out[key] += int(row.get("point") or 0)
        return out

    def _op_attribute_open(self, profile, op, _force, extra):
        catalog, chara_row, card, attribute = self._attribute_context(profile, op.get("charaId"))
        cells = catalog.get("cells") or {}
        rows = profile["state"].setdefault("userCharaAtbEnhancementCellList", [])
        owned = {str(row.get("atbEnhancementCellId")) for row in rows
                 if str(row.get("charaId")) == str(chara_row.get("charaId"))}
        revision = int(card.get("revision") or 0)
        wanted = op.get("cellIds")
        if not wanted and op.get("cellId") is not None:
            wanted = [op.get("cellId")]
        targets = []
        if op.get("all"):
            for cid, cell in cells.items():
                if cell.get("attribute") != attribute or str(cid) in owned:
                    continue
                if int(cell.get("revision") or 0) > revision:
                    continue
                targets.append(cell)
        else:
            if not wanted:
                raise ValidationError("需要 cellId / cellIds / all", code="invalid_argument")
            for raw in wanted:
                cid = int(raw)
                cell = cells.get(str(cid))
                if cell is None:
                    raise ValidationError("没有属性强化格 %s" % cid, code="path_missing")
                if cell.get("attribute") != attribute:
                    raise ValidationError("格 %s 属于 %s，不是该角色的属性 %s"
                                          % (cid, cell.get("attribute"), attribute),
                                          code="attribute_mismatch")
                if str(cid) in owned:
                    continue
                if int(cell.get("revision") or 0) > revision:
                    raise ValidationError("该属性强化需要卡片突破到 %s" % cell.get("revision"),
                                          code="attribute_revision_locked")
                targets.append(cell)
        if not targets:
            return
        member_count = sum(1 for row in profile["state"].get("userCharaList") or []
                           if (catalog.get("charaAttribute") or {}).get(str(row.get("charaId"))) == attribute)
        stamp = self.stamp()
        delta = {}
        for cell in targets:
            rows.append({
                "userId": profile["state"]["gameUser"]["userId"],
                "charaId": chara_row.get("charaId"),
                "atbEnhancementCellId": cell.get("cellId"),
                "createdAt": stamp,
            })
            delta[cell.get("type")] = delta.get(cell.get("type"), 0) + int(cell.get("value") or 0)
        status_rows = profile["state"].setdefault("userStatusList", [])
        forces = 0
        for status_id, value in delta.items():
            row = None
            for candidate in status_rows:
                if str(candidate.get("statusId")) == status_id:
                    row = candidate
                    break
            if row is None:
                row = {"userId": profile["state"]["gameUser"]["userId"], "statusId": status_id,
                       "point": 0, "checkPeriod": 0, "periodicPoint": 0,
                       "checkedAt": stamp, "createdAt": stamp}
                status_rows.append(row)
            row["point"] = int(row.get("point") or 0) + value
            forces += value * member_count
        extra.add("state.userCharaAtbEnhancementCellList")
        extra.add("state.userStatusList")
        self._add_forces(profile, forces)

    def _op_attribute_close(self, profile, op, _force, extra):
        catalog, chara_row, _card, attribute = self._attribute_context(profile, op.get("charaId"))
        rows = profile["state"].get("userCharaAtbEnhancementCellList") or []
        chara_id = str(chara_row.get("charaId"))
        if op.get("all"):
            targets = [row for row in rows if str(row.get("charaId")) == chara_id]
        else:
            cid = op.get("cellId")
            if cid is None:
                raise ValidationError("需要 cellId 或 all=true", code="invalid_argument")
            targets = [row for row in rows if str(row.get("charaId")) == chara_id
                       and str(row.get("atbEnhancementCellId")) == str(cid)]
            if not targets:
                raise ValidationError("该属性强化未开启", code="path_missing")
        member_count = sum(1 for row in profile["state"].get("userCharaList") or []
                           if (catalog.get("charaAttribute") or {}).get(str(row.get("charaId"))) == attribute)
        status_rows = profile["state"].get("userStatusList") or []
        forces = 0
        for row in targets:
            cell = (catalog.get("cells") or {}).get(str(row.get("atbEnhancementCellId"))) or {}
            value = int(cell.get("value") or 0)
            for candidate in status_rows:
                if str(candidate.get("statusId")) == cell.get("type"):
                    candidate["point"] = max(0, int(candidate.get("point") or 0) - value)
                    break
            forces -= value * member_count
            rows.remove(row)
        extra.add("state.userCharaAtbEnhancementCellList")
        extra.add("state.userStatusList")
        self._add_forces(profile, forces)

    def awakening_view(self, user_card_id=None, chara_id=None):
        """觉醒（升星）与魔力解放（revision，消耗命运宝石 lbItemNum，每次 +1 结晶槽位）。"""
        awaken = self.awakening_catalog()
        limit = awaken.get("limitBreak") or {}
        rules = awaken.get("awakening") or {}
        cards = awaken.get("cards") or {}
        charas = awaken.get("charas") or {}
        chara_row = None
        if user_card_id is not None:
            for row in self.rows("userCharaList"):
                if str(row.get("userCardId")) == str(user_card_id):
                    chara_row = row
                    break
        elif chara_id is not None:
            for row in self.rows("userCharaList"):
                if str(row.get("charaId")) == str(chara_id):
                    chara_row = row
                    break
        if chara_row is None:
            raise ValidationError("找不到角色: %r" % (chara_id or user_card_id,), code="path_missing")
        card = None
        for row in self.rows("userCardList"):
            if str(row.get("id")) == str(chara_row.get("userCardId")):
                card = row
                break
        if card is None:
            raise ValidationError("角色没有对应卡片", code="path_missing")
        master = cards.get(str(card.get("cardId"))) or {}
        rank = master.get("rank")
        chain = (charas.get(str(chara_row.get("charaId"))) or {}).get("chain") or []
        revision = int(card.get("revision") or 0)
        costs = limit.get("costByRevision") or []
        gems = (limit.get("gemsByDefaultRank") or {}).get(str(rank))
        item_code = limit.get("item")
        items = self.item_index()
        item_have = int((items.get(item_code) or {}).get("quantity") or 0)
        riche = int(self.state.get("gameUser", {}).get("riche") or 0)
        steps = []
        for step in range(3):
            steps.append({
                "from": step,
                "to": step + 1,
                "cost": costs[step] if step < len(costs) else None,
                "force": limit.get("forcePerRevision"),
                "done": revision > step,
            })
        slots = []
        gifts = {int(row.get("giftId")): row for row in self.rows("userGiftList")
                 if isinstance(row.get("giftId"), int)}
        for slot in master.get("customize") or []:
            gift_id = int(slot.get("giftId") or 0)
            have = int((gifts.get(gift_id) or {}).get("quantity") or 0)
            slots.append({
                "target": slot.get("target"),
                "giftId": gift_id,
                "giftName": self.catalog.gift_name(gift_id),
                "icon": "/assets/gifts/%s.png" % gift_id,
                "quantity": slot.get("quantity"),
                "have": have,
                "enough": have >= int(slot.get("quantity") or 0),
                "bonusCode": slot.get("bonusCode"),
                "bonusNum": slot.get("bonusNum"),
                "done": bool(card.get("customized%d" % slot.get("target"))),
            })
        level_cap = master.get("maxLevel")
        next_card = (rules.get("nextCard") or {}).get(str(card.get("cardId")))
        next_master = cards.get(str(next_card)) or {}
        evolve_cost = (rules.get("evolveCost") or {}).get(str(rank))
        all_slots = bool(slots) and all(slot["done"] for slot in slots)
        level_ok = level_cap is not None and int(card.get("level") or 0) >= int(level_cap)
        return {
            "charaId": chara_row.get("charaId"),
            "userCardId": card.get("id"),
            "name": self.catalog.chara_name(chara_row.get("charaId")),
            "card": {
                "cardId": card.get("cardId"),
                "name": master.get("name"),
                "rank": rank,
                "rankStars": int(str(rank).replace("RANK_", "")) if str(rank).startswith("RANK_") else None,
                "maxLevel": level_cap,
                "level": card.get("level"),
                "revision": revision,
                "lbItemNum": chara_row.get("lbItemNum"),
                "gemsNeeded": gems,
                "customized": [card.get("customized%d" % i) for i in range(1, 7)],
            },
            "limitBreak": {
                "policy": limit.get("policy"),
                "steps": steps,
                "costs": costs,
                "gems": gems,
                "item": item_code,
                "itemName": self.catalog.item_name(item_code) if item_code else None,
                "itemIcon": self._asset_url("items", "%s.png" % str(item_code).lower()) if item_code else None,
                "itemHave": item_have,
                "forcePerRevision": limit.get("forcePerRevision"),
                "riche": riche,
            },
            "evolve": {
                "policy": rules.get("policy"),
                "chain": chain,
                "chainIndex": chain.index(card.get("cardId")) if card.get("cardId") in chain else -1,
                "nextCard": next_card,
                "nextName": next_master.get("name"),
                "nextRank": next_master.get("rank"),
                "cost": evolve_cost,
                "levelOk": level_ok,
                "slotsOk": all_slots,
                "slots": slots,
                "forcePerSlot": rules.get("forcePerSlot"),
                "riche": riche,
            },
        }

    def _op_awake_limit(self, profile, op, _force, extra):
        awaken = self.awakening_catalog()
        limit = awaken.get("limitBreak") or {}
        card = self._find_card_row(profile, op.get("userCardId"))
        revision = int(card.get("revision") or 0)
        count = int(op.get("count") or 1)
        if count < 1:
            raise ValidationError("count 必须 ≥ 1", code="invalid_value")
        target = min(3, revision + count)
        if target <= revision:
            return
        gained = target - revision
        card["revision"] = target
        self._add_forces(profile, gained * int(limit.get("forcePerRevision") or 500))
        extra.add("state.userTotalForces")

    def _op_awake_set(self, profile, op, _force, extra):
        awaken = self.awakening_catalog()
        limit = awaken.get("limitBreak") or {}
        card = self._find_card_row(profile, op.get("userCardId"))
        revision = int(card.get("revision") or 0)
        value = op.get("value")
        if not isinstance(value, int) or value < 0 or value > 3:
            raise ValidationError("revision 必须是 0-3", code="invalid_value")
        if value == revision:
            return
        card["revision"] = value
        self._add_forces(profile, (value - revision) * int(limit.get("forcePerRevision") or 500))
        extra.add("state.userTotalForces")

    def _op_awake_slot(self, profile, op, _force, extra):
        """设置（或清除）觉醒槽位旗标；升星判定用 customized1..6。"""
        card = self._find_card_row(profile, op.get("userCardId"))
        awaken = self.awakening_catalog()
        master = (awaken.get("cards") or {}).get(str(card.get("cardId"))) or {}
        slots = {int(slot.get("target")) for slot in master.get("customize") or []}
        targets = op.get("targets")
        if targets is None:
            targets = sorted(slots)
        if not isinstance(targets, list):
            raise ValidationError("targets 必须是数组", code="invalid_value")
        value = bool(op.get("value", True))
        per_slot = int((awaken.get("awakening") or {}).get("forcePerSlot") or 200)
        flipped = 0
        for target in targets:
            target = int(target)
            if target not in slots:
                raise ValidationError("卡片 %s 没有觉醒槽位 %s" % (card.get("cardId"), target),
                                      code="path_missing")
            current = card.get("customized%d" % target)
            key = "state.userCardList[%d].customized%d" % (profile["state"]["userCardList"].index(card), target)
            if value and current is not True:
                card["customized%d" % target] = True
                extra.add(key)
                flipped += 1
            elif not value and current is True:
                card["customized%d" % target] = False
                extra.add(key)
                flipped -= 1
        if flipped:
            self._add_forces(profile, flipped * per_slot)
            extra.add("state.userTotalForces")

    def doppels_catalog(self):
        return self._data("doppels.json", {"charaDoppel": {}})

    def doppel_view(self, chara_id):
        """魔女化身解锁状态：存档里 userDoppelList 有对应 doppelId 即为已解锁。"""
        meta = (self.doppels_catalog().get("charaDoppel") or {}).get(str(chara_id)) or {}
        doppel_id = meta.get("doppelId")
        rows = [row for row in self.rows("userDoppelList")
                if doppel_id and str(row.get("doppelId")) == str(doppel_id)]
        return {
            "doppelId": doppel_id,
            "name": meta.get("name"),
            "title": meta.get("title"),
            "description": meta.get("description"),
            "cardId": meta.get("cardId"),
            "unlocked": bool(rows),
            "rows": len(rows),
        }

    def formations_catalog(self):
        return self._data("formations.json", {"formations": {}})

    def formation_meta(self, formation_id):
        return (self.formations_catalog().get("formations") or {}).get(str(formation_id)) or {}

    def formation_owned(self, profile, formation_id):
        return any(str(row.get("formationSheetId")) == str(formation_id)
                   for row in profile["state"].get("userFormationSheetList") or [])

    def formations_view(self):
        owned = {str(row.get("formationSheetId")) for row in self.rows("userFormationSheetList")}
        used = {}
        for deck in self.rows("userDeckList"):
            key = str(deck.get("formationSheetId"))
            used.setdefault(key, []).append(deck.get("name") or ("队伍 %s" % deck.get("deckType")))
        rows = []
        for key, meta in (self.formations_catalog().get("formations") or {}).items():
            rows.append({
                "formationSheetId": meta.get("id"),
                "name": meta.get("name"),
                "description": meta.get("description"),
                "slots": meta.get("slots") or [],
                "skills": meta.get("skills") or {},
                "owned": key in owned,
                "usedBy": used.get(key, []),
            })
        rows.sort(key=lambda item: item.get("formationSheetId") or 0)
        return {
            "rows": rows,
            "total": len(rows),
            "ownedCount": sum(1 for row in rows if row["owned"]),
        }

    def _op_formation_add(self, profile, op, _force, extra):
        formation_id = _int(op.get("formationSheetId"), "formationSheetId", 1)
        if not self.formation_meta(formation_id):
            raise ValidationError("没有阵型 %s" % formation_id, code="path_missing")
        rows = profile["state"].setdefault("userFormationSheetList", [])
        for row in rows:
            if str(row.get("formationSheetId")) == str(formation_id):
                raise ValidationError("这个阵型已经获取了", code="already_present")
        rows.append({
            "userId": profile["state"]["gameUser"]["userId"],
            "formationSheetId": formation_id,
            "createdAt": self.stamp(),
        })
        extra.add("state.userFormationSheetList")

    def _op_formation_remove(self, profile, op, _force, extra):
        formation_id = _int(op.get("formationSheetId"), "formationSheetId", 1)
        for deck in profile["state"].get("userDeckList") or []:
            if str(deck.get("formationSheetId")) == str(formation_id):
                raise ValidationError("阵型 %s 正被「%s」使用，先换掉再取消获取"
                                      % (formation_id, deck.get("name") or deck.get("deckType")),
                                      code="formation_in_use")
        rows = profile["state"].get("userFormationSheetList") or []
        target = None
        for row in rows:
            if str(row.get("formationSheetId")) == str(formation_id):
                target = row
                break
        if target is None:
            raise ValidationError("这个阵型还没获取", code="path_missing")
        rows.remove(target)
        extra.add("state.userFormationSheetList")

    def _op_deck_formation(self, profile, op, _force, extra):
        deck = self._deck_target(profile, op)
        formation_id = _int(op.get("formationSheetId"), "formationSheetId", 1)
        meta = self.formation_meta(formation_id)
        if not meta:
            raise ValidationError("没有阵型 %s" % formation_id, code="path_missing")
        if not self.formation_owned(profile, formation_id):
            raise ValidationError("阵型 %s 还没获取，先获取再用" % formation_id, code="formation_not_owned")
        slots = sorted(meta.get("slots") or [])
        if len(slots) < 5:
            raise ValidationError("阵型 %s 的格子不足 5 个" % formation_id, code="formation_slots_invalid")
        deck["formationSheetId"] = formation_id
        for index in range(1, 5):
            deck["questPositionId%d" % index] = slots[index - 1]
        deck["questPositionHelper"] = slots[4]
        extra.add("state.userDeckList[deckType=%s]" % deck.get("deckType"))

    def _op_deck_helper(self, profile, op, _force, extra):
        deck = self._deck_target(profile, op)
        position = _int(op.get("position"), "position", 1, 9)
        formation_id = deck.get("formationSheetId")
        meta = self.formation_meta(formation_id)
        slots = sorted(meta.get("slots") or [])
        if slots and position not in slots:
            raise ValidationError("阵型 %s 没有第 %s 个格子" % (formation_id, position),
                                  code="deck_position_invalid")
        used = {deck.get("questPositionId%d" % index) for index in range(1, 5)}
        if position in used:
            raise ValidationError("第 %s 个格子已经有上阵角色了" % position, code="deck_position_taken")
        deck["questPositionHelper"] = position
        extra.add("state.userDeckList[deckType=%s]" % deck.get("deckType"))

    def stories_catalog(self):
        return self._data("stories.json", {"sections": {}, "byChara": {}})

    def reward_text(self, reward, chara_id=None):
        """首通奖励翻成中文：道具查 itemList，结晶查 pieceList，魔女化身查 doppelList，衣装查衣装表。"""
        if not isinstance(reward, dict):
            return None
        kind = reward.get("presentType")
        quantity = reward.get("quantity") or 0
        if kind == "ITEM":
            name = self.catalog.item_name(reward.get("itemId"))
        elif kind in ("PIECE", "MAXPIECE"):
            name = self.catalog.piece_name(reward.get("pieceId") or reward.get("genericId"))
        elif kind == "DOPPEL":
            name = self.catalog.doppel_name(reward.get("genericId")) or reward.get("displayName")
        elif kind == "LIVE2D":
            owner = reward.get("genericId") or chara_id
            live2d_id = str(reward.get("genericCode") or "").zfill(2)
            costume = (self._costume_entry(owner, live2d_id) or {}).get("name")
            name = ("%s的衣装【%s】" % (self.catalog.chara_name(owner), costume)) if costume \
                else (reward.get("displayName") or "衣装")
        elif kind == "GEM":
            name = "命运宝石"
        else:
            name = reward.get("displayName") or kind
        return {"kind": kind, "name": name, "quantity": quantity}

    def stories_view(self, chara_id):
        """角色剧情（CHARA）与衣装剧情（COSTUME）：段 + 段内任务 + 存档里的通关状态。"""
        data = self.stories_catalog()
        meta_all = data.get("sections") or {}
        bucket = (data.get("byChara") or {}).get(str(chara_id)) or {}
        section_rows = {str(row.get("sectionId")): row for row in self.rows("userSectionList")}
        quest_rows = {str(row.get("questBattleId")): row for row in self.rows("userQuestBattleList")}

        def pack(section_id):
            meta = meta_all.get(str(section_id)) or {}
            quests = []
            for qid in meta.get("questBattleIds") or []:
                row = quest_rows.get(str(qid)) or {}
                quests.append({
                    "questBattleId": qid,
                    "present": bool(row),
                    "cleared": row.get("cleared") is True,
                    "missions": [row.get("missionStatus%d" % i) for i in range(1, 4)],
                })
            section_row = section_rows.get(str(section_id)) or {}
            live2d_id = meta.get("live2dId")
            costume_name = None
            if live2d_id:
                costume_name = (self._costume_entry(chara_id, live2d_id) or {}).get("name")
            return {
                "sectionId": int(section_id),
                "title": meta.get("title"),
                "message": meta.get("message"),
                "area": meta.get("area"),
                "ap": meta.get("ap"),
                "difficulty": meta.get("difficulty"),
                "live2dId": live2d_id,
                "costumeName": costume_name,
                "reward": self.reward_text(meta.get("clearReward"), chara_id),
                "present": bool(section_row),
                "canPlay": section_row.get("canPlay"),
                "cleared": section_row.get("cleared") is True,
                "quests": quests,
            }

        chara_rows = [pack(sid) for sid in bucket.get("chara") or []]
        costume_rows = [pack(sid) for sid in bucket.get("costume") or []]
        return {
            "chara": chara_rows,
            "costume": costume_rows,
            "charaCleared": sum(1 for row in chara_rows if row["cleared"]),
            "costumeCleared": sum(1 for row in costume_rows if row["cleared"]),
        }

    def pieces_detail_catalog(self):
        return self._data("pieces-detail.json", {"pieces": {}})

    MATERIAL_PIECES = (1147, 1148, 1297, 1298)

    def materials_view(self):
        """结晶强化 / 突破用的素材（记忆结晶回路、限制解除器等），按实例数计量。"""
        rows = []
        for piece_id in self.MATERIAL_PIECES:
            meta = self.catalog.piece(piece_id)
            instances = [row for row in self.rows("userPieceList")
                         if str(row.get("pieceId")) == str(piece_id)]
            rows.append({
                "pieceId": piece_id,
                "name": meta.get("name"),
                "kind": meta.get("kind"),
                "count": len(instances),
                "icon": "/assets/pieces/%s.png" % piece_id,
            })
        return {"rows": rows}

    def _op_piece_material(self, profile, op, _force, extra):
        piece_id = _int(op.get("pieceId"), "pieceId", 1)
        count = _int(op.get("count"), "count", 0, 9999)
        rows = profile["state"].setdefault("userPieceList", [])
        instances = [row for row in rows if str(row.get("pieceId")) == str(piece_id)]
        current = len(instances)
        if count == current:
            raise ValidationError("数量没变", code="already_present")
        if count > current:
            master = self.masters.piece(piece_id)
            stamp = self.stamp()
            for _offset in range(count - current):
                row = {
                    "userId": profile["state"]["gameUser"]["userId"],
                    "id": str(uuid.uuid4()),
                    "pieceId": piece_id,
                    "level": 1,
                    "experience": 0,
                    "lbCount": 0,
                    "attack": 0,
                    "defense": 0,
                    "hp": 0,
                    "protect": False,
                    "archive": 0,
                    "createdAt": stamp,
                }
                if master:
                    row.update(growth.piece_stats(master, 1, 0))
                rows.append(row)
        else:
            for row in instances[count:]:
                rows.remove(row)
        extra.add("state.userPieceList")

    def piece_master_view(self, piece_id):
        """结晶的静态信息（未拥有也能看）：描述、技能、基础与满级数值。"""
        detail = (self.pieces_detail_catalog().get("pieces") or {}).get(str(piece_id)) or {}
        meta = self.catalog.piece(piece_id)
        master = self.masters.piece(piece_id)
        cap = None
        stats_max = None
        if master:
            try:
                cap = growth.piece_max_level(master.get("rank"), 0)
                stats_max = growth.piece_stats(master, cap, 0)
            except growth.GrowthError:
                cap = None
        return {
            "pieceId": int(piece_id) if str(piece_id).isdigit() else piece_id,
            "name": detail.get("name") or meta.get("name"),
            "rank": detail.get("rank") or meta.get("rank"),
            "type": detail.get("type") or meta.get("type"),
            "kind": detail.get("kind") or meta.get("kind"),
            "attribute": detail.get("attribute") or meta.get("attribute"),
            "description": detail.get("description") or meta.get("description"),
            "limitCharaIds": detail.get("limitCharaIds") or [],
            "limitCharaNames": [self.catalog.chara_name(cid) for cid in (detail.get("limitCharaIds") or [])],
            "skills": [
                dict(skill, unlockAt=(None if index == 0 else 4))
                for index, skill in enumerate(detail.get("skills") or [])
            ],
            "base": master,
            "maxLevel": cap,
            "statsAtMax": stats_max,
        }

    def pieces_view(self, query=None, offset=0, limit=200, rank=None, piece_type=None,
                    limit_mode=None, chara_id=None):
        rows = []
        index = self.deck_index()
        for row in self.rows("userPieceList"):
            piece_id = row.get("pieceId")
            meta = self.catalog.piece(piece_id)
            master = self.masters.piece(piece_id)
            cap = None
            if master:
                try:
                    cap = growth.piece_max_level(master.get("rank"), row.get("lbCount") or 0)
                except growth.GrowthError:
                    cap = None
            detail = (self.pieces_detail_catalog().get("pieces") or {}).get(str(piece_id)) or {}
            limit_ids = detail.get("limitCharaIds") or []
            rows.append({
                "id": row.get("id"),
                "pieceId": piece_id,
                "limitCharaIds": limit_ids,
                "limitCharaNames": [self.catalog.chara_name(cid) for cid in limit_ids],
                "name": meta.get("name"),
                "type": meta.get("type"),
                "kind": meta.get("kind"),
                "attribute": meta.get("attribute"),
                "rank": meta.get("rank") or (master or {}).get("rank"),
                "level": row.get("level"),
                "maxLevel": cap,
                "experience": row.get("experience"),
                "lbCount": row.get("lbCount"),
                "hp": row.get("hp"),
                "attack": row.get("attack"),
                "defense": row.get("defense"),
                "protect": row.get("protect"),
                "archive": row.get("archive"),
                "icon": "/assets/pieces/%s.png" % piece_id,
                "editable": bool(master),
                "decks": index.get(str(row.get("id")), []),
                "path": "state.userPieceList[id=%s]" % row.get("id"),
            })
        ranks = sorted({row["rank"] for row in rows if row.get("rank")})
        types = sorted({row["type"] for row in rows if row.get("type")})
        if query:
            needle = query.lower()
            rows = [row for row in rows
                    if needle in str(row["pieceId"]) or needle in str(row["name"]).lower()]
        if rank:
            rows = [row for row in rows if str(row.get("rank")) == str(rank)]
        if piece_type:
            rows = [row for row in rows if str(row.get("type")) == str(piece_type)]
        if limit_mode == "limited":
            rows = [row for row in rows if row.get("limitCharaIds")]
        elif limit_mode == "free":
            rows = [row for row in rows if not row.get("limitCharaIds")]
        if chara_id:
            rows = [row for row in rows
                    if not row.get("limitCharaIds")
                    or str(chara_id) in [str(item) for item in row["limitCharaIds"]]]
        rows.sort(key=lambda row: (row.get("pieceId") or 0))
        return {"total": len(rows), "offset": offset, "limit": limit, "rows": rows[offset: offset + limit],
                "ranks": ranks, "types": types}

    def piece_detail(self, instance_id):
        row = None
        for candidate in self.rows("userPieceList"):
            if str(candidate.get("id")) == str(instance_id):
                row = candidate
                break
        if row is None:
            raise ValidationError("找不到记忆结晶: %r" % (instance_id,), code="path_missing")
        meta = self.catalog.piece(row.get("pieceId"))
        detail = (self.pieces_detail_catalog().get("pieces") or {}).get(str(row.get("pieceId"))) or {}
        master = self.masters.piece(row.get("pieceId"))
        cap = None
        if master:
            try:
                cap = growth.piece_max_level(master.get("rank"), row.get("lbCount") or 0)
            except growth.GrowthError:
                cap = None
        return {
            "id": row.get("id"),
            "pieceId": row.get("pieceId"),
            "name": detail.get("name") or meta.get("name"),
            "rank": detail.get("rank") or meta.get("rank"),
            "type": detail.get("type") or meta.get("type"),
            "kind": detail.get("kind"),
            "attribute": detail.get("attribute"),
            "description": detail.get("description") or meta.get("description"),
            "illustrator": detail.get("illustrator"),
            "limitCharaIds": detail.get("limitCharaIds") or [],
            "limitCharaNames": [self.catalog.chara_name(cid) for cid in (detail.get("limitCharaIds") or [])],
            "skills": [
                dict(skill, unlockAt=(None if index == 0 else 4),
                     active=(index == (1 if int(row.get("lbCount") or 0) >= 4 else 0)))
                for index, skill in enumerate(detail.get("skills") or [])
            ],
            "activeSkillIndex": 1 if int(row.get("lbCount") or 0) >= 4 else 0,
            "level": row.get("level"),
            "maxLevel": cap,
            "experience": row.get("experience"),
            "lbCount": row.get("lbCount"),
            "hp": row.get("hp"),
            "attack": row.get("attack"),
            "defense": row.get("defense"),
            "protect": row.get("protect"),
            "archive": row.get("archive"),
            "icon": "/assets/pieces/%s.png" % row.get("pieceId"),
            "editable": bool(master),
            "decks": self.deck_index().get(str(row.get("id")), []),
        }

    SUPPORT_POSITIONS = {1: "ALL", 2: "FIRE", 3: "WATER", 4: "TIMBER", 5: "LIGHT", 6: "DARK"}

    DECK_PIECE_RE = re.compile(r"^userPieceId(\d{2})(\d)$")

    def deck_piece_slots(self, deck):
        slots = {}
        for key, value in (deck or {}).items():
            match = self.DECK_PIECE_RE.match(key)
            if match and value:
                slots[(int(match.group(1)), int(match.group(2)))] = value
        return slots

    def deck_index(self):
        """结晶实例 id → 被编成到的队伍位置（带那个位置上的魔法少女）。"""
        cards = {str(row.get("id")): row for row in self.rows("userCardList")}
        index = {}
        for deck in self.rows("userDeckList"):
            name = deck.get("name") or ("队伍 %s" % deck.get("deckType"))
            for (member, slot), instance in self.deck_piece_slots(deck).items():
                card = cards.get(str(deck.get("userCardId%d" % member))) or {}
                card_id = card.get("cardId")
                master = self.masters.card(card_id) if card_id else None
                chara_id = (master or {}).get("charaNo")
                index.setdefault(str(instance), []).append({
                    "deckType": deck.get("deckType"),
                    "name": name,
                    "member": member,
                    "slot": slot,
                    "charaId": chara_id,
                    "charaName": self.catalog.chara_name(chara_id) if chara_id else None,
                    "userCardId": card.get("id"),
                    "face": ("/assets/faces/%s.png" % card_id) if card_id else None,
                })
        return index

    def decks_view(self):
        """任务编队 4 个可编辑位 + 支援位；支援编成（deckType 20）按 6 个属性槽展示。"""
        cards = {str(row.get("id")): row for row in self.rows("userCardList")}
        pieces = {str(row.get("id")): row for row in self.rows("userPieceList")}
        charas_by_card = {str(row.get("userCardId")): row for row in self.rows("userCharaList")}
        unlocked_doppels = {str(row.get("doppelId")) for row in self.rows("userDoppelList")}
        doppel_by_chara = {}
        for key, value in (self.doppels_catalog().get("charaDoppel") or {}).items():
            doppel_by_chara[str(key)] = value.get("doppelId")
        decks = []
        for deck in self.rows("userDeckList"):
            deck_type = deck.get("deckType")
            formation = self.formation_meta(deck.get("formationSheetId"))
            if not isinstance(deck_type, int):
                kind = "other"
            elif deck_type < 20:
                kind = "quest"
            elif deck_type == 20:
                kind = "support"
            elif deck_type == 21:
                kind = "arena"
            elif deck_type == 22:
                kind = "stage"
            else:
                kind = "other"

            members = []
            for index in range(1, 7 if kind == "support" else 5):
                user_card_id = deck.get("userCardId%d" % index)
                card = cards.get(str(user_card_id)) or {}
                chara_row = charas_by_card.get(str(user_card_id)) or {}
                card_id = card.get("cardId")
                master = self.masters.card(card_id) if card_id else None
                chara_id = (master or {}).get("charaNo")
                chara_meta = self.catalog.chara(chara_id) if chara_id else {}
                attribute = chara_meta.get("attribute")
                piece_rows = []
                piece_total = {"hp": 0, "attack": 0, "defense": 0}
                for slot in range(1, 6):
                    instance = deck.get("userPieceId%02d%d" % (index, slot))
                    if not instance:
                        continue
                    piece = pieces.get(str(instance)) or {}
                    pmeta = self.catalog.piece(piece.get("pieceId"))
                    rank = pmeta.get("rank")
                    piece_rows.append({
                        "slot": slot,
                        "id": instance,
                        "pieceId": piece.get("pieceId"),
                        "name": pmeta.get("name"),
                        "icon": "/assets/pieces/%s.png" % piece.get("pieceId"),
                        "rank": rank,
                        "rankStars": int(str(rank).replace("RANK_", "")) if str(rank).startswith("RANK_") else None,
                        "level": piece.get("level"),
                        "lbCount": piece.get("lbCount"),
                        "hp": piece.get("hp"),
                        "attack": piece.get("attack"),
                        "defense": piece.get("defense"),
                    })
                    for key in ("hp", "attack", "defense"):
                        piece_total[key] += int(piece.get(key) or 0)
                spirit = {
                    "hp": int(chara_row.get("addendHp") or 0),
                    "attack": int(chara_row.get("addendAttack") or 0),
                    "defense": int(chara_row.get("addendDefense") or 0),
                }
                bonus = self.attribute_bonus(attribute)
                capacity = max(1, min(4, 1 + int(card.get("revision") or 0)))
                base = {"hp": int(card.get("hp") or 0),
                        "attack": int(card.get("attack") or 0),
                        "defense": int(card.get("defense") or 0)}
                total = {key: base[key] + piece_total[key] + spirit[key] for key in base}
                doppel_id = doppel_by_chara.get(str(chara_id)) if chara_id else None
                members.append({
                    "slot": index,
                    "position": deck.get("questPositionId%d" % index),
                    "userCardId": user_card_id,
                    "cardId": card_id,
                    "charaId": chara_id,
                    "attribute": attribute,
                    "name": self.catalog.chara_name(chara_id) if chara_id else None,
                    "icon": ("/assets/cards/%s.png" % card_id) if card_id else None,
                    "face": ("/assets/faces/%s.png" % card_id) if card_id else None,
                    "rank": (master or {}).get("rank"),
                    "rankStars": int(str((master or {}).get("rank")).replace("RANK_", ""))
                    if str((master or {}).get("rank")).startswith("RANK_") else None,
                    "level": card.get("level"),
                    "magiaLevel": card.get("magiaLevel"),
                    "revision": card.get("revision"),
                    "doppelId": doppel_id,
                    "doppelUnlocked": bool(doppel_id and str(doppel_id) in unlocked_doppels),
                    "base": base,
                    "pieceBonus": piece_total,
                    "spiritBonus": spirit,
                    "attributeBonus": bonus,
                    "total": total,
                    "pieceCapacity": capacity,
                    "pieces": piece_rows,
                })

            if kind == "support":
                layout = []
                for member in members:
                    position = member.get("position")
                    layout.append(dict(member, kind="member" if member.get("userCardId") else "empty",
                                       attribute_name=self.SUPPORT_POSITIONS.get(position)
                                       if isinstance(position, int) else None))
                decks.append({
                    "deckType": deck_type,
                    "kind": kind,
                    "layout": layout,
                    "name": deck.get("name") or ("支援编成 %s" % deck_type),
                    "formationSheetId": None,
                    "formationName": None,
                    "formationSlots": [],
                    "helperPosition": None,
                    "helper": None,
                    "members": members,
                    "memberCount": sum(1 for member in members if member.get("userCardId")),
                    "pieceCount": sum(len(member["pieces"]) for member in members),
                    "editable": False,
                })
                continue

            def visual_index(slot):
                if not isinstance(slot, int):
                    return 99
                column = (slot - 1) // 3
                band = (slot - 1) % 3
                return (2 - band) * 3 + column

            formation_slots = sorted(formation.get("slots") or [])
            helper_position = deck.get("questPositionHelper")
            by_position = {}
            for member in members:
                position = member.get("position")
                if isinstance(position, int):
                    by_position[position] = member
            layout = []
            for slot in sorted(formation_slots, key=visual_index):
                if slot == helper_position:
                    layout.append({"kind": "helper", "slot": "helper", "position": slot,
                                   "label": "支援（好友）", "pieces": []})
                elif slot in by_position:
                    layout.append(dict(by_position[slot], kind="member"))
                else:
                    layout.append({"kind": "empty", "slot": None, "position": slot,
                                   "name": None, "pieces": []})
            for member in members:
                if member.get("userCardId") and member.get("position") not in formation_slots:
                    layout.append(dict(member, kind="member"))
            if not formation_slots:
                layout = [dict(member, kind="member") for member in members if member.get("userCardId")]
                layout.append({"kind": "helper", "slot": "helper",
                               "position": helper_position, "pieces": []})
            decks.append({
                "deckType": deck_type,
                "kind": kind,
                "layout": layout,
                "name": deck.get("name") or ("队伍 %s" % deck_type),
                "formationSheetId": deck.get("formationSheetId"),
                "formationName": formation.get("name"),
                "formationSlots": formation.get("slots") or [],
                "formationSkills": formation.get("skills") or {},
                "helperPosition": helper_position,
                "helper": {"slot": "helper", "position": helper_position,
                           "label": "支援（好友）"} if helper_position else None,
                "members": members,
                "memberCount": sum(1 for member in members if member.get("userCardId")),
                "pieceCount": sum(len(member["pieces"]) for member in members),
                "editable": kind == "quest",
            })
        decks.sort(key=lambda item: (item["deckType"] if isinstance(item["deckType"], int) else 99))
        return {"decks": decks, "total": len(decks)}
    def piece_catalog(self, query=None, offset=0, limit=50, rank=None, piece_type=None,
                      limit_mode=None, chara_id=None):
        """全部已实装记忆结晶（可添加）。结晶可重复获取，因此只报已有数量，不阻止再添加。"""
        owned_count = {}
        for row in self.rows("userPieceList"):
            key = str(row.get("pieceId"))
            owned_count[key] = owned_count.get(key, 0) + 1
        rows = []
        for key, row in self.catalog.data["pieces"].items():
            name = row.get("name") or key
            if query:
                needle = query.lower()
                if needle not in key and needle not in str(name).lower():
                    continue
            if rank and str(row.get("rank")) != str(rank):
                continue
            if piece_type and str(row.get("type")) != str(piece_type):
                continue
            master = self.masters.piece(key)
            if not master:
                # 缺成长数据的结晶无法添加，也无法推算数值
                continue
            detail = (self.pieces_detail_catalog().get("pieces") or {}).get(str(key)) or {}
            limit_ids = detail.get("limitCharaIds") or []
            if limit_mode == "limited" and not limit_ids:
                continue
            if limit_mode == "free" and limit_ids:
                continue
            if chara_id and limit_ids and str(chara_id) not in [str(item) for item in limit_ids]:
                continue
            rows.append({
                "pieceId": int(key) if key.isdigit() else key,
                "name": name,
                "limitCharaIds": limit_ids,
                "limitCharaNames": [self.catalog.chara_name(cid) for cid in limit_ids],
                "type": row.get("type"),
                "rank": row.get("rank"),
                "attribute": row.get("attribute"),
                "owned": key in owned_count,
                "ownedCount": owned_count.get(key, 0),
                "addable": bool(master),
                "base": master,
                "icon": "/assets/pieces/%s.png" % key,
            })
        rows.sort(key=lambda row: (str(row.get("name")), str(row.get("pieceId"))))

        def rank(item):
            if not query:
                return 0
            needle = query.lower()
            return 0 if str(item["pieceId"]) == needle else 1

        ranks = sorted({str(item["rank"]) for item in rows if item.get("rank")})
        types = sorted({str(item["type"]) for item in rows if item.get("type")})
        rows.sort(key=rank)
        total = len(rows)
        return {"total": total, "offset": offset, "limit": limit, "rows": rows[offset: offset + limit],
                "ranks": ranks, "types": types}

    def quests_view(self, query=None, offset=0, limit=200, only=None):
        sections = {}
        for row in self.rows("userSectionList"):
            sections[str(row.get("sectionId"))] = row
        rows = []
        for row in self.rows("userQuestBattleList"):
            quest_id = str(row.get("questBattleId"))
            section_id = None
            for key in sections:
                if quest_id.startswith(key):
                    section_id = key
                    break
            meta = self.catalog.section(section_id) if section_id else {}
            rows.append({
                "questBattleId": row.get("questBattleId"),
                "sectionId": int(section_id) if section_id and section_id.isdigit() else section_id,
                "sectionTitle": meta.get("title"),
                "questType": meta.get("questType"),
                "cleared": row.get("cleared"),
                "clearCount": row.get("clearCount"),
                "rewardDone": row.get("rewardDone"),
                "maxDamage": row.get("maxDamage"),
                "missions": [row.get("missionStatus1"), row.get("missionStatus2"), row.get("missionStatus3")],
                "path": "state.userQuestBattleList[questBattleId=%s" % quest_id + "]",
            })
        if only == "cleared":
            rows = [row for row in rows if row["cleared"] is True]
        elif only == "uncleared":
            rows = [row for row in rows if row["cleared"] is not True]
        if query:
            needle = query.lower()
            rows = [row for row in rows
                    if needle in str(row["questBattleId"]) or needle in str(row["sectionTitle"] or "").lower()]
        rows.sort(key=lambda row: (row.get("questBattleId") or 0))
        return {"total": len(rows), "offset": offset, "limit": limit, "rows": rows[offset: offset + limit]}

    def sections_view(self, query=None, offset=0, limit=200):
        rows = []
        for row in self.rows("userSectionList"):
            meta = self.catalog.section(row.get("sectionId"))
            rows.append({
                "sectionId": row.get("sectionId"),
                "title": meta.get("title"),
                "area": meta.get("area"),
                "questType": meta.get("questType"),
                "canPlay": row.get("canPlay"),
                "cleared": row.get("cleared"),
                "path": "state.userSectionList[sectionId=%s]" % row.get("sectionId"),
            })
        if query:
            needle = query.lower()
            rows = [row for row in rows
                    if needle in str(row["sectionId"]) or needle in str(row["title"] or "").lower()
                    or needle in str(row["area"] or "").lower()]
        rows.sort(key=lambda row: (row.get("sectionId") or 0))
        return {"total": len(rows), "offset": offset, "limit": limit, "rows": rows[offset: offset + limit]}

    @staticmethod
    def _is_side_chapter(part, chapter_id):
        """第一部支线是 52..60，第二部支线是 71..81（主线是 11..20 / 21..33）。"""
        if part == 1:
            return int(chapter_id or 0) >= 50
        if part == 2:
            return int(chapter_id or 0) >= 70
        return False

    @classmethod
    def _chapter_group(cls, part, chapter_id):
        if part == 1:
            return "第一部 · 支线" if cls._is_side_chapter(part, chapter_id) else "第一部 · 主线"
        if part == 2:
            return "第二部 · 支线" if cls._is_side_chapter(part, chapter_id) else "第二部 · 主线"
        return "其他（外传 / 活动）"

    def chapters_view(self):
        """全部章节（来自 unlocks.json），带小节进度与是否有剧情树布局。"""
        data = self.unlocks_catalog()
        meta_chapters = data.get("chapters") or {}
        state_rows = {str(row.get("chapterId")): row for row in self.rows("userChapterList")}
        section_rows = {str(row.get("sectionId")): row for row in self.rows("userSectionList")}
        rows = []
        for key, meta in meta_chapters.items():
            local = self.catalog.chapter(meta.get("chapterId"))
            sections = meta.get("sections") or []
            required = meta.get("requiredSections") or []
            cleared = sum(1 for sid in sections
                          if (section_rows.get(str(sid)) or {}).get("cleared") is True)
            by_section = data.get("bySection") or {}
            quest_total = sum(len((by_section.get(str(sid)) or {}).get("requiredQuestIds") or [])
                             for sid in sections)
            required_cleared = sum(1 for sid in required
                                   if (section_rows.get(str(sid)) or {}).get("cleared") is True)
            state = state_rows.get(str(meta.get("chapterId"))) or {}
            rows.append({
                "chapterId": meta.get("chapterId"),
                "no": local.get("no") or meta.get("chapterNoForView"),
                "title": local.get("title") or meta.get("title"),
                "questType": local.get("questType") or meta.get("questType"),
                "partNo": meta.get("partNo"),
                "partLabel": self._chapter_group(meta.get("partNo"), meta.get("chapterId")),
                "isSideStory": self._is_side_chapter(meta.get("partNo"), meta.get("chapterId")),
                "chapterType": meta.get("chapterType"),
                "sectionCount": len(sections),
                "requiredCount": len(required),
                "requiredCleared": required_cleared,
                "hasQuests": quest_total > 0,
                "questTotal": quest_total,
                "layoutCount": len(meta.get("layout") or []),
                "present": bool(state),
                "cleared": state.get("cleared") is True,
                "clearedSections": cleared,
                "path": "state.userChapterList[chapterId=%s]" % meta.get("chapterId"),
            })
        rows.sort(key=lambda row: (row.get("partNo") or 0, row.get("chapterId") or 0))
        return {"total": len(rows), "rows": rows}

    def _op_section_clear(self, profile, op, _force, extra):
        """整节通关 / 回退：小节与它包含的任务一起处理。"""
        section_id = _int(op.get("sectionId"), "sectionId", 1)
        cleared = _bool(op.get("cleared"), "cleared")
        data = self.unlocks_catalog()
        rules = (data.get("bySection") or {}).get(str(section_id)) or {}
        stamp = self.stamp()
        changed = self._ensure_section(profile, section_id, cleared, stamp)
        for qid in rules.get("requiredQuestIds") or []:
            if self._ensure_quest(profile, qid, cleared, stamp):
                changed = True
        if not changed:
            raise ValidationError("这一节已经是目标状态", code="already_present")
        self._normalize_story(profile)
        extra.add("state.userSectionList")
        extra.add("state.userQuestBattleList")

    ROOT_SECTIONS = (101101,)

    def _story_graph(self, data):
        by_section = data.get("bySection") or {}
        successors = {}
        for key, row in by_section.items():
            for target in row.get("unlockSections") or []:
                successors.setdefault(str(int(target)), set()).add(int(key))
        return by_section, data.get("prerequisites") or {}, successors

    def _is_branch_section(self, data, section_id):
        rules = (data.get("bySection") or {}).get(str(section_id)) or {}
        chapter = (data.get("chapters") or {}).get(str(rules.get("chapterId"))) or {}
        return chapter.get("chapterType") == "BRANCH"

    def _story_group_roots(self, data):
        """每一套剧情的第一话不能锁定：第一部主线 / 第一部支线 / 第二部主线 /
        第二部支线 各算一套；partNo=3 的每套外传各自算一套。"""
        groups = {}
        for key, chapter in (data.get("chapters") or {}).items():
            chapter_id = int(chapter.get("chapterId") or key)
            part = chapter.get("partNo")
            if part == 1:
                name = "p1-side" if self._is_side_chapter(part, chapter_id) else "p1-main"
            elif part == 2:
                name = "p2-side" if self._is_side_chapter(part, chapter_id) else "p2-main"
            else:
                name = "other-%s" % chapter_id
            groups.setdefault(name, []).append(chapter)
        roots = set(int(x) for x in self.ROOT_SECTIONS)
        for chapters in groups.values():
            sections = []
            for chapter in chapters:
                sections.extend(int(x) for x in chapter.get("sections") or [])
            if sections:
                roots.add(min(sections))
        return roots

    def section_detail(self, section_id):
        """三级页面：一节下面挂的任务（含三星状态）。"""
        data = self.unlocks_catalog()
        rules = (data.get("bySection") or {}).get(str(section_id)) or {}
        section_row = None
        for row in self.rows("userSectionList"):
            if str(row.get("sectionId")) == str(section_id):
                section_row = row
                break
        quest_rows = {str(row.get("questBattleId")): row for row in self.rows("userQuestBattleList")}
        quests = []
        for qid in rules.get("requiredQuestIds") or []:
            row = quest_rows.get(str(qid)) or {}
            quests.append({
                "questBattleId": qid,
                "present": bool(row),
                "cleared": row.get("cleared") is True,
                "clearCount": row.get("clearCount"),
                "missions": [row.get("missionStatus%d" % index) for index in range(1, 4)],
            })
        meta = self.catalog.section(section_id)
        return {
            "sectionId": int(section_id) if str(section_id).isdigit() else section_id,
            "title": meta.get("title") or ((rules.get("title") if isinstance(rules, dict) else None)),
            "area": meta.get("area"),
            "questType": meta.get("questType"),
            "present": bool(section_row),
            "canPlay": (section_row or {}).get("canPlay"),
            "cleared": (section_row or {}).get("cleared") is True,
            "lockable": int(section_id) not in self._story_group_roots(data),
            "quests": quests,
            "requires": [int(x) for x in (data.get("prerequisites") or {}).get(str(section_id)) or []],
            "unlocks": [int(x) for x in rules.get("unlockSections") or []],
        }

    def _normalize_story(self, profile):
        """将剧情/关卡状态收拂到「正常游戏能达到」的范围（只向下回退，不会无端补通关）：
        前置没通关 → 本节不能保持已通关/已解锁；本节已通关 → 它里面的任务补成已通关；
        任务已通关但小节未通关 → 把任务回退。"""
        data = self.unlocks_catalog()
        by_section = data.get("bySection") or {}
        prereq = data.get("prerequisites") or {}
        section_rows = profile["state"].setdefault("userSectionList", [])
        quest_rows = profile["state"].setdefault("userQuestBattleList", [])
        sindex = {str(row.get("sectionId")): row for row in section_rows}
        qindex = {str(row.get("questBattleId")): row for row in quest_rows}
        quest_owner = {}
        for sid, rules in by_section.items():
            for qid in rules.get("requiredQuestIds") or []:
                quest_owner[str(qid)] = sid
        changed = 0
        for _ in range(32):
            round_changed = 0
            for sid, row in list(sindex.items()):
                deps = [str(int(dep)) for dep in prereq.get(sid) or []]
                ready = all((sindex.get(dep) or {}).get("cleared") is True for dep in deps)
                if row.get("cleared") is True and not ready:
                    row["cleared"] = False
                    round_changed += 1
                if deps and row.get("canPlay") is True and not ready:
                    row["canPlay"] = False
                    round_changed += 1
                if row.get("cleared") is True:
                    if row.get("canPlay") is not True:
                        row["canPlay"] = True
                        round_changed += 1
                    for qid in (by_section.get(sid) or {}).get("requiredQuestIds") or []:
                        qrow = qindex.get(str(qid))
                        if qrow is None or qrow.get("cleared") is True:
                            continue
                        qrow["cleared"] = True
                        qrow["clearCount"] = max(1, int(qrow.get("clearCount") or 0))
                        round_changed += 1
            for qid, qrow in list(qindex.items()):
                if qrow.get("cleared") is not True:
                    continue
                sid = quest_owner.get(qid)
                srow = sindex.get(sid) if sid else None
                if srow is not None and srow.get("cleared") is not True:
                    qrow["cleared"] = False
                    qrow["clearCount"] = 0
                    for index in range(1, 4):
                        qrow["missionStatus%d" % index] = MISSION_NOT_CLEAR
                    round_changed += 1
            changed += round_changed
            if not round_changed:
                break
        return changed

    def _op_story_unlock(self, profile, op, _force, extra):
        """解锁这一节：把它之前的所有前置小节一起解锁并通关。"""
        section_id = _int(op.get("sectionId"), "sectionId", 1)
        data = self.unlocks_catalog()
        by_section, prereq, _successors = self._story_graph(data)
        if str(section_id) not in by_section:
            raise ValidationError("没有这一节的解锁数据", code="path_missing")
        needed, stack = set(), [section_id]
        while stack:
            sid = stack.pop()
            for dep in prereq.get(str(sid)) or []:
                dep = int(dep)
                if dep not in needed:
                    needed.add(dep)
                    stack.append(dep)
        stamp = self.stamp()
        order = self._chapter_topology(data, sorted(needed | {section_id}))
        for sid in order:
            self._ensure_section(profile, sid, True, stamp)
            for qid in (by_section.get(str(sid)) or {}).get("requiredQuestIds") or []:
                self._ensure_quest(profile, qid, True, stamp)
        self._normalize_story(profile)
        extra.add("state.userSectionList")
        extra.add("state.userQuestBattleList")

    def _op_story_lock(self, profile, op, _force, extra):
        """锁定这一节：后续全部锁定，并把前一关设为未通关。"""
        section_id = _int(op.get("sectionId"), "sectionId", 1)
        data = self.unlocks_catalog()
        if section_id in self._story_group_roots(data):
            raise ValidationError("每一套剧情的第一话不能锁定", code="root_section_locked")
        by_section, prereq, _successors = self._story_graph(data)
        if str(section_id) not in by_section:
            raise ValidationError("没有这一节的解锁数据", code="path_missing")
        following, stack = set(), [section_id]
        while stack:
            sid = stack.pop()
            for child in (by_section.get(str(sid)) or {}).get("unlockSections") or []:
                child = int(child)
                if child not in following:
                    following.add(child)
                    stack.append(child)
        stamp = self.stamp()
        for sid in sorted(following | {section_id}):
            self._ensure_section(profile, sid, False, stamp, can_play=False)
            for qid in (by_section.get(str(sid)) or {}).get("requiredQuestIds") or []:
                self._ensure_quest(profile, qid, False, stamp)
        for dep in prereq.get(str(section_id)) or []:
            self._ensure_section(profile, int(dep), False, stamp)
        self._normalize_story(profile)
        extra.add("state.userSectionList")
        extra.add("state.userQuestBattleList")

    def unlocks_catalog(self):
        return self._data("unlocks.json", {"bySection": {}, "prerequisites": {}, "chapters": {}})

    def _chapter_topology(self, data, section_ids):
        """按 unlocks.json 的前置关系做拓扑排序；成环时按 sectionId 收尾，不会死循环。"""
        ids = [int(x) for x in section_ids]
        idset = set(ids)
        prereq = data.get("prerequisites") or {}
        order, done, visiting = [], set(), set()

        def visit(sid):
            if sid in done:
                return
            if sid in visiting:
                return
            visiting.add(sid)
            for dep in prereq.get(str(sid)) or []:
                dep = int(dep)
                if dep in idset:
                    visit(dep)
            visiting.discard(sid)
            done.add(sid)
            order.append(sid)

        for sid in sorted(ids):
            visit(sid)
        return order

    def chapter_map(self, chapter_id):
        """分支章的剧情树：坐标 + 连线 + 每节状态（游戏里 MainQuestBranch 用的就是这套数据）。"""
        data = self.unlocks_catalog()
        chapter = (data.get("chapters") or {}).get(str(chapter_id))
        if not chapter:
            raise ValidationError("没有章节 %s 的数据" % chapter_id, code="path_missing")
        layout = chapter.get("layout") or []
        if not layout:
            # 线性章：没有坐标，按小节顺序列出
            for sid in chapter.get("sections") or []:
                meta = self.catalog.section(sid)
                layout.append({"sectionId": sid, "title": meta.get("title"), "x": None, "y": None,
                               "connect": [], "iconId": None, "parameter": None, "secret": False})
        section_rows = {str(row.get("sectionId")): row for row in self.rows("userSectionList")}
        quest_rows = {str(row.get("questBattleId")): row for row in self.rows("userQuestBattleList")}
        by_section = data.get("bySection") or {}
        prereq = data.get("prerequisites") or {}
        required_ids = set(int(x) for x in chapter.get("requiredSections") or [])
        nodes = []
        placed_ids = set()
        for item in layout:
            sid = str(item.get("sectionId"))
            placed_ids.add(sid)
            section_row = section_rows.get(sid) or {}
            rules = by_section.get(sid) or {}
            quests = []
            for qid in rules.get("requiredQuestIds") or []:
                row = quest_rows.get(str(qid)) or {}
                quests.append({"questBattleId": qid, "present": bool(row),
                               "cleared": row.get("cleared") is True})
            local = self.catalog.section(item.get("sectionId"))
            nodes.append({
                "sectionId": item.get("sectionId"),
                "title": (local or {}).get("title") or item.get("title"),
                "x": item.get("x"),
                "y": item.get("y"),
                "connect": item.get("connect") or [],
                "iconId": item.get("iconId"),
                "parameter": item.get("parameter"),
                "secret": bool(item.get("secret")),
                "present": bool(section_row),
                "canPlay": section_row.get("canPlay"),
                "cleared": section_row.get("cleared") is True,
                "questCount": len(quests),
                "questCleared": sum(1 for quest in quests if quest["cleared"]),
                "requires": [int(x) for x in (prereq.get(sid) or [])],
                "unplaced": False,
                "required": int(item.get("sectionId")) in required_ids,
            })
        # 没有地图坐标的小节也要列出来（游戏里它们不在地图上）
        for sid_value in chapter.get("sections") or []:
            sid = str(int(sid_value))
            if sid in placed_ids:
                continue
            section_row = section_rows.get(sid) or {}
            rules = by_section.get(sid) or {}
            quests = []
            for qid in rules.get("requiredQuestIds") or []:
                row = quest_rows.get(str(qid)) or {}
                quests.append({"questBattleId": qid, "present": bool(row),
                               "cleared": row.get("cleared") is True})
            local = self.catalog.section(int(sid_value))
            nodes.append({
                "sectionId": int(sid_value),
                "title": (local or {}).get("title"),
                "x": None,
                "y": None,
                "connect": [],
                "iconId": None,
                "parameter": None,
                "secret": False,
                "present": bool(section_row),
                "canPlay": section_row.get("canPlay"),
                "cleared": section_row.get("cleared") is True,
                "questCount": len(quests),
                "questCleared": sum(1 for quest in quests if quest["cleared"]),
                "requires": [int(x) for x in (prereq.get(sid) or [])],
                "unplaced": True,
                "required": int(sid_value) in required_ids,
            })
        if nodes:
            xs = [node["x"] for node in nodes if isinstance(node.get("x"), int)]
            ys = [node["y"] for node in nodes if isinstance(node.get("y"), int)]
            bounds = {"minX": min(xs) if xs else 0, "maxX": max(xs) if xs else 0,
                      "minY": min(ys) if ys else 0, "maxY": max(ys) if ys else 0,
                      "pixel": (max(xs) > 200 if xs else False)}
        else:
            bounds = {"minX": 0, "maxX": 0, "minY": 0, "maxY": 0, "pixel": False}
        return {
            "chapterId": chapter_id,
            "title": (self.catalog.chapter(chapter_id) or {}).get("title") or chapter.get("title"),
            "chapterNoForView": (self.catalog.chapter(chapter_id) or {}).get("no")
            or chapter.get("chapterNoForView"),
            "chapterType": chapter.get("chapterType"),
            "partNo": chapter.get("partNo"),
            "nodeCount": len(nodes),
            "hasQuests": any(node.get("questCount") for node in nodes),
            "hasLayout": any(isinstance(node.get("x"), int) and isinstance(node.get("y"), int)
                             for node in nodes),
            "nodes": nodes,
            "bounds": bounds,
            "order": self._chapter_topology(data, [item.get("sectionId") for item in layout]),
        }

    def _ensure_section(self, profile, section_id, cleared, stamp, can_play=None):
        if can_play is None:
            can_play = True if cleared else None
        rows = profile["state"].setdefault("userSectionList", [])
        for row in rows:
            if str(row.get("sectionId")) == str(section_id):
                changed = False
                if row.get("cleared") is not cleared:
                    row["cleared"] = cleared
                    changed = True
                if can_play is not None and row.get("canPlay") is not can_play:
                    row["canPlay"] = can_play
                    changed = True
                return changed
        rows.append({
            "userId": profile["state"]["gameUser"]["userId"],
            "sectionId": section_id,
            "canPlay": True,
            "cleared": cleared,
            "createdAt": stamp,
        })
        return True

    def _ensure_quest(self, profile, quest_id, cleared, stamp):
        rows = profile["state"].setdefault("userQuestBattleList", [])
        for row in rows:
            if str(row.get("questBattleId")) == str(quest_id):
                changed = False
                if row.get("cleared") is not cleared:
                    row["cleared"] = cleared
                    changed = True
                want = max(1, int(row.get("clearCount") or 0)) if cleared else 0
                if int(row.get("clearCount") or 0) != want:
                    row["clearCount"] = want
                    changed = True
                for index in range(1, 4):
                    key = "missionStatus%d" % index
                    if row.get(key) in (MISSION_CLEARED, MISSION_NOT_CLEAR):
                        value = MISSION_CLEARED if cleared else MISSION_NOT_CLEAR
                        if row.get(key) != value:
                            row[key] = value
                            changed = True
                return changed
        if not cleared:
            return False
        rows.append({
            "userId": profile["state"]["gameUser"]["userId"],
            "questBattleId": int(quest_id),
            "cleared": True,
            "clearCount": 1,
            "rewardDone": False,
            "maxDamage": 0,
            "missionStatus1": MISSION_CLEARED,
            "missionStatus2": MISSION_CLEARED,
            "missionStatus3": MISSION_CLEARED,
            "createdAt": stamp,
        })
        return True

    def _op_chapter_clear(self, profile, op, _force, extra):
        """整章通关/解锁：按拓扑序处理，通关时把该章之前的前置小节一并解锁。"""
        chapter_id = _int(op.get("chapterId"), "chapterId", 1)
        cleared = _bool(op.get("cleared"), "cleared")
        data = self.unlocks_catalog()
        chapter = (data.get("chapters") or {}).get(str(chapter_id))
        if not chapter:
            raise ValidationError("没有章节 %s 的数据" % chapter_id, code="path_missing")
        by_section = data.get("bySection") or {}
        prereq = data.get("prerequisites") or {}
        chapters_meta = data.get("chapters") or {}
        chapter_prereq = data.get("chapterPrerequisites") or {}
        targets = set(int(x) for x in chapter.get("sections") or [])
        if not targets:
            raise ValidationError("章节 %s 没有小节数据" % chapter_id, code="path_missing")
        needed = set(targets)
        if cleared:
            # 章级前置：下一章要求上一章全通（分支章也一样）
            chapter_queue = [chapter_id]
            seen_chapters = {chapter_id}
            while chapter_queue:
                current = chapter_queue.pop()
                deps = chapter_prereq.get(str(current)) or []
                if not deps:
                    meta = chapters_meta.get(str(current)) or {}
                    part = meta.get("partNo")
                    number = meta.get("chapterNo")
                    if part is not None and number:
                        for other_key, other in chapters_meta.items():
                            if other.get("partNo") == part and other.get("chapterNo") == number - 1:
                                deps = [int(other_key)]
                                break
                for dep in deps:
                    dep = int(dep)
                    if dep in seen_chapters:
                        continue
                    seen_chapters.add(dep)
                    chapter_queue.append(dep)
                    dep_meta = chapters_meta.get(str(dep)) or {}
                    needed |= set(int(x) for x in dep_meta.get("sections") or [])
            stack = list(needed)
        if not cleared:
            stack = list(targets)
        while stack:
            sid = stack.pop()
            if sid in needed:
                pass
            for dep in prereq.get(str(sid)) or []:
                dep = int(dep)
                if dep not in needed:
                    needed.add(dep)
                    stack.append(dep)
        order = self._chapter_topology(data, sorted(needed))
        stamp = self.stamp()
        touched = 0
        for sid in order:
            if self._ensure_section(profile, sid, cleared, stamp):
                touched += 1
            rules = by_section.get(str(sid)) or {}
            for qid in rules.get("requiredQuestIds") or []:
                if self._ensure_quest(profile, qid, cleared, stamp):
                    touched += 1
        if not touched:
            self._ensure_chapter(profile, chapter_id, cleared)
            raise ValidationError("这些小节已经是目标状态", code="already_present")
        self._ensure_chapter(profile, chapter_id, cleared)
        if cleared:
            # 前置章节的小节也一起通关了，章节状态要跟着置为已通
            touched_chapters = {int(chapter_id)}
            for sid in needed:
                owner = (int(sid) // 100) % 100
                if str(owner) in chapters_meta:
                    touched_chapters.add(owner)
            for cid in sorted(touched_chapters):
                self._ensure_chapter(profile, cid, True)
            self._unlock_following_chapters(profile, chapter_id, chapters_meta, chapter_prereq, stamp)
        extra.add("state.userChapterList")
        extra.add("state.userSectionList")
        extra.add("state.userQuestBattleList")

    def _unlock_following_chapters(self, profile, chapter_id, chapters_meta, chapter_prereq, stamp):
        """整章通关后把「以本章为前置」的下一章的入口打开（小节 canPlay=true）。"""
        followers = []
        for key, deps in (chapter_prereq or {}).items():
            if chapter_id in [int(dep) for dep in deps or []]:
                followers.append(int(key))
        if not followers:
            meta = chapters_meta.get(str(chapter_id)) or {}
            part = meta.get("partNo")
            number = meta.get("chapterNo")
            if part is not None and number:
                for other_key, other in chapters_meta.items():
                    if (other.get("partNo") == part and other.get("chapterNo") == number + 1
                            and not self._is_side_chapter(part, int(other_key))):
                        followers.append(int(other_key))
                        break
        for next_id in followers:
            next_meta = chapters_meta.get(str(next_id)) or {}
            sections = sorted(int(x) for x in next_meta.get("sections") or [])
            if not sections:
                continue
            # 只开下一章的第一话（入口），不整章解锁；后续话仍按剧情推进逐话开启
            self._ensure_section(profile, sections[0], False, stamp, can_play=True)
            self._ensure_chapter(profile, next_id, False)

    def _op_chapter_unlock_only(self, profile, op, _force, extra):
        """只解锁不判通关：把该章及前置小节设为 canPlay=true。"""
        chapter_id = _int(op.get("chapterId"), "chapterId", 1)
        data = self.unlocks_catalog()
        chapter = (data.get("chapters") or {}).get(str(chapter_id))
        if not chapter:
            raise ValidationError("没有章节 %s 的数据" % chapter_id, code="path_missing")
        prereq = data.get("prerequisites") or {}
        chapters_meta = data.get("chapters") or {}
        chapter_prereq = data.get("chapterPrerequisites") or {}
        needed = set(int(x) for x in chapter.get("sections") or [])
        # 章级前置章的所有小节都要先通关
        chapter_queue = [chapter_id]
        seen_chapters = {chapter_id}
        while chapter_queue:
            current = chapter_queue.pop()
            deps = chapter_prereq.get(str(current)) or []
            if not deps:
                meta = chapters_meta.get(str(current)) or {}
                part = meta.get("partNo")
                number = meta.get("chapterNo")
                if part is not None and number:
                    for other_key, other in chapters_meta.items():
                        if other.get("partNo") == part and other.get("chapterNo") == number - 1:
                            deps = [int(other_key)]
                            break
            for dep in deps:
                dep = int(dep)
                if dep in seen_chapters:
                    continue
                seen_chapters.add(dep)
                chapter_queue.append(dep)
                dep_meta = chapters_meta.get(str(dep)) or {}
                gate = dep_meta.get("requiredSections") or dep_meta.get("sections") or []
                for sid in gate:
                    needed.add(int(sid))
                    for dep2 in prereq.get(str(sid)) or []:
                        needed.add(int(dep2))
        stack = [int(x) for x in chapter.get("sections") or []]
        while stack:
            sid = stack.pop()
            for dep in prereq.get(str(sid)) or []:
                dep = int(dep)
                if dep not in needed:
                    needed.add(dep)
                    stack.append(dep)
        stamp = self.stamp()
        rows = profile["state"].setdefault("userSectionList", [])
        index = {str(row.get("sectionId")): row for row in rows}
        touched = 0
        for sid in sorted(needed):
            row = index.get(str(sid))
            if row is None:
                rows.append({
                    "userId": profile["state"]["gameUser"]["userId"],
                    "sectionId": sid,
                    "canPlay": True,
                    "cleared": False,
                    "createdAt": stamp,
                })
                touched += 1
            elif row.get("canPlay") is not True:
                row["canPlay"] = True
                touched += 1
        if not touched:
            raise ValidationError("这些小节已经解锁", code="already_present")
        self._ensure_chapter(profile, chapter_id, False)
        extra.add("state.userChapterList")
        extra.add("state.userSectionList")

    CHALLENGE_TABLES = (
        ("userDailyChallengeList", "每日挑战"),
        ("userLimitedChallengeList", "限时挑战"),
        ("userTotalChallengeList", "累计挑战"),
        ("userDoppelChallengeList", "魔女化身挑战"),
    )

    def challenge_catalog(self):
        """数据包里的挑战定义（tools/build_challenge_catalog.py 聚合），count 是官方目标次数。"""
        if getattr(self, "_challenge_catalog_cache", None) is None:
            path = os.path.join(_DATA_DIR, "challenges.json")
            try:
                with open(path, encoding="utf-8") as handle:
                    self._challenge_catalog_cache = json.load(handle).get("challenges") or {}
            except Exception:
                self._challenge_catalog_cache = {}
        return self._challenge_catalog_cache

    def challenges_view(self, include_unknown=False):
        """挑战：目标次数只取官方 challenge master 的 count；没有定义就不显示目标，不做文案解析。"""
        catalog = self.challenge_catalog()
        out = []
        hidden = 0
        without_target = 0
        for table, label in self.CHALLENGE_TABLES:
            rows = []
            for row in self.rows(table):
                challenge_id = row.get("challengeId")
                master = catalog.get(str(challenge_id)) or {}
                text = self.catalog.challenge_text(table, challenge_id) or master.get("displayName")
                if not text and not include_unknown:
                    hidden += 1
                    continue
                target = master.get("count") if isinstance(master.get("count"), int) else None
                if target is None:
                    without_target += 1
                rows.append({
                    "challengeId": challenge_id,
                    "text": text,
                    "label": text or "条件未收录",
                    "known": bool(text),
                    "target": target,
                    "targetSource": "challenge.count" if target is not None else None,
                    "bean": master.get("bean"),
                    "clearedCount": row.get("clearedCount"),
                    "count": row.get("count"),
                    "update": row.get("update"),
                    "receivedAt": row.get("receivedAt"),
                    "clearedAt": row.get("clearedAt"),
                    "path": "state.%s[challengeId=%s]" % (table, challenge_id),
                })
            rows.sort(key=lambda item: (item.get("challengeId") or 0))
            out.append({"table": table, "label": label, "total": len(rows), "rows": rows})
        return {"groups": out, "hiddenUnknown": hidden, "withoutTarget": without_target}

    def _op_battle_clear(self, profile, op, _force, extra):
        """脱战：清掉进行中的战斗会话与待结算的战斗结果 id，不动已结算记录与奖励。

        死档常见原因就是异常退出后 battleSession 与 userQuestBattleResultId 残留，
        游戏每次启动都要求续战或结算。
        """
        session = profile.get("battleSession")
        result_id = (profile.get("state", {}).get("gameUser") or {}).get("userQuestBattleResultId")
        if not session and not result_id:
            raise ValidationError("当前没有进行中的战斗", code="no_pending_battle")
        profile["battleSession"] = None
        extra.add("battleSession")
        if result_id:
            profile["state"]["gameUser"]["userQuestBattleResultId"] = None
            extra.add("state.gameUser.userQuestBattleResultId")
        if op.get("clearReceipts"):
            profile["battleReceipts"] = []
            extra.add("battleReceipts")

    def gifts_view(self):
        """gift 是养成素材：全部已实装素材都列出（未拥有显示 0），按官方 ID 段分组。"""
        owned = {}
        for row in self.rows("userGiftList"):
            owned[str(row.get("giftId"))] = row
        ids = set(self.catalog.data["gifts"].keys()) | set(owned.keys())
        buckets = {}
        for key in ids:
            gift_id = int(key) if str(key).isdigit() else key
            row = owned.get(str(key)) or {}
            label = "未归类"
            for start, end, name in GIFT_GROUPS:
                if isinstance(gift_id, int) and start <= gift_id <= end:
                    label = name
                    break
            buckets.setdefault(label, []).append({
                "giftId": gift_id,
                "name": self.catalog.gift_name(key),
                "quantity": row.get("quantity") if row else 0,
                "total": row.get("total") if row else 0,
                "owned": bool(row),
                "createdAt": row.get("createdAt"),
                "path": "state.userGiftList[giftId=%s]" % gift_id,
                "icon": "/assets/gifts/%s.png" % key,
            })
        order = [name for _s, _e, name in GIFT_GROUPS] + ["未归类"]
        groups = []
        for name in order:
            if name in buckets:
                buckets[name].sort(key=lambda row: (row.get("giftId") or 0))
                groups.append({"label": name, "rows": buckets[name], "total": len(buckets[name])})
        return {"groups": groups, "total": sum(group["total"] for group in groups)}

    def gift_catalog(self, query=None, limit=60):
        owned = {str(row.get("giftId")) for row in self.rows("userGiftList")}
        rows = []
        for key, name in self.catalog.data["gifts"].items():
            if query and query.lower() not in key and query.lower() not in str(name).lower():
                continue
            rows.append({"giftId": int(key) if key.isdigit() else key, "name": name,
                         "owned": key in owned,
                         "icon": "/assets/gifts/%s.png" % key})
            if len(rows) >= limit:
                break
        rows.sort(key=lambda row: str(row.get("giftId")))
        return {"rows": rows}

    def status_view(self):
        rows = []
        for row in self.rows("userStatusList"):
            status_id = row.get("statusId")
            meta = STATUS_META.get(status_id) or {}
            rows.append({
                "statusId": status_id,
                "label": self.catalog.status_label(status_id),
                "point": row.get("point"),
                "checkPeriod": row.get("checkPeriod"),
                "periodicPoint": row.get("periodicPoint"),
                "checkedAt": row.get("checkedAt"),
                "maxOf": meta.get("max"),
                "path": "state.userStatusList[statusId=%s]" % status_id,
            })
        rows.sort(key=lambda row: str(row["statusId"]))
        item_rows = {str(row.get("itemId")): row for row in self.rows("userItemList")}
        medals = []
        for code, meta in sorted((self.catalog.data.get("items") or {}).items()):
            if not (code.startswith("EVENT_GROUPBATTLE_") and code.endswith("_COIN")):
                continue
            row = item_rows.get(code) or {}
            medals.append({
                "itemId": code,
                "name": (meta or {}).get("name") or code,
                "quantity": int(row.get("quantity") or 0),
                "eventId": code.split("_")[2] if len(code.split("_")) > 2 else "",
            })
        user_row = self.state.get("user") or {}
        game_user = self.state.get("gameUser") or {}
        return {"rows": rows, "groupBattleMedals": medals, "player": {
            "loginName": user_row.get("loginName"),
            "comment": game_user.get("comment"),
            "userId": game_user.get("userId"),
            "level": game_user.get("level"),
            "riche": game_user.get("riche"),
        }}

    # ------------------------------------------------------------ 多级分组（部 → 章 → 话 → 任务）

    QUEST_TYPE_LABELS = [
        ("MAIN", "主线剧情"),
        ("SUB", "支线剧情"),
        ("CHARA", "角色剧情"),
        ("COSTUME", "装扮剧情"),
        ("ENHANCEMENT_AROUSAL", "强化剧情"),
        ("COMPOSE", "合成"),
        ("MATERIAL", "素材"),
    ]

    # 部段（sectionId 前 3 位）→ 部标题。
    # 依据：101 段的章 7/8/9 与 chapterList 的 NORMALSECTIONID=101718/101881/101972 对应，
    # 章号→chapterId = 10+N 全部落在「第1章..第10章」；102 段章号 0 的 5 个小节对应 chapterList 21「序章」，
    # 章号 N→chapterId = 21+N 全部落在「第1章..第12章」。103/104 段在 chapterList 里没有对应章号序列，
    # 标签直接标明部段编号，不用推测的篇名。
    STORY_PARTS = {
        "101": {"label": "第1部", "order": 1, "chapter_base": 10},
        "102": {"label": "第2部", "order": 2, "chapter_base": 21},
        "103": {"label": "外传篇（部段 103）", "order": 3, "chapter_base": None},
        "104": {"label": "外传篇（部段 104）", "order": 4, "chapter_base": None},
    }

    def _section_layout(self):
        """部段 + 章号 → 该章首个话的标题，用作没有官方章名时的兜底标签。"""
        if getattr(self, "_layout_cache", None) is not None:
            return self._layout_cache
        layout = {}
        for key, row in self.catalog.data["sections"].items():
            text = str(key)
            if len(text) < 4:
                continue
            prefix, index = text[:3], text[3]
            bucket = layout.setdefault(prefix, {})
            current = bucket.get(index)
            if current is None or int(text) < current[0]:
                bucket[index] = (int(text), row.get("title"))
        self._layout_cache = layout
        return layout

    def _chapter_index(self, section_id):
        text = str(section_id or "")
        if len(text) == 6:
            return int(text[3])
        if len(text) == 7:
            return int(text[3:5])
        return None

    def _locate(self, section_id, quest_type):
        """返回 (part_key, part_label, part_order, chapter_key, chapter_label, chapter_order)。"""
        text = str(section_id or "")
        prefix = text[:3]
        index = self._chapter_index(text)

        spec = self.STORY_PARTS.get(prefix)
        if spec and index is not None:
            base = spec["chapter_base"]
            if base is not None:
                chapter_id = base + index
                meta = self.catalog.chapter(chapter_id)
                if meta.get("title"):
                    label = "%s %s" % (meta.get("no") or ("第%d章" % chapter_id), meta.get("title"))
                    return ("part:" + prefix, spec["label"], spec["order"] * 1000,
                            "part:%s/ch:%d" % (prefix, chapter_id), label, chapter_id)
            layout = self._section_layout().get(prefix, {})
            first = layout.get(str(index))
            if first and first[1]:
                label = "第%d章 · %s" % (index, first[1])
                return ("part:" + prefix, spec["label"], spec["order"] * 1000,
                        "part:%s/ch:%d" % (prefix, index), label, index)

        for position, (type_key, type_label) in enumerate(self.QUEST_TYPE_LABELS):
            if type_key == quest_type:
                return ("type:" + type_key, type_label, 8000 + position, "type:%s/all" % type_key,
                        type_label, position)
        label = "活动 / 其他" if quest_type is None else str(quest_type)
        return ("type:%s" % (quest_type or "EVENT"), label, 8500, "type:%s/all" % (quest_type or "EVENT"),
                label, 0)

    def _section_of(self, quest_id, known):
        text = str(quest_id)
        for length in (7, 6, 5, 4, 3):
            candidate = text[:length]
            if candidate in known:
                return candidate
        return None

    def _section_meta(self, section_id):
        meta = self.catalog.section(section_id) if section_id else {}
        return {
            "sectionId": int(section_id) if section_id and str(section_id).isdigit() else section_id,
            "title": meta.get("title"),
            "area": meta.get("area"),
            "questType": meta.get("questType"),
        }

    def _tree_payload(self, buckets, total, cleared_key="cleared"):
        """把扁平 bucket 组装成 部 → 章 → 话 三级结构。"""
        parts = {}
        for bucket in buckets:
            part = parts.setdefault(bucket["_part"], {
                "key": bucket["_part_key"], "label": bucket["_part_label"],
                "order": bucket["_part_order"], "chapters": {},
                "total": 0, "cleared": 0,
            })
            chapter = part["chapters"].setdefault(bucket["_chapter"], {
                "key": bucket["_chapter_key"], "label": bucket["_chapter_label"],
                "order": bucket["_chapter_order"], "sections": [], "total": 0, "cleared": 0,
            })
            chapter["sections"].append({key: value for key, value in bucket.items()
                                        if not key.startswith("_")})
            chapter["total"] += bucket["total"]
            part["total"] += bucket["total"]
            if cleared_key in bucket:
                chapter["cleared"] += bucket[cleared_key]
                part["cleared"] += bucket[cleared_key]
        out = []
        for part in sorted(parts.values(), key=lambda item: (item["order"], item["label"])):
            chapters = []
            for chapter in sorted(part["chapters"].values(), key=lambda item: (item["order"], item["label"])):
                chapter["sections"].sort(key=lambda item: str(item["sectionId"]))
                chapter["sectionCount"] = len(chapter["sections"])
                chapters.append(chapter)
            out.append({"key": part["key"], "label": part["label"], "chapters": chapters,
                        "total": part["total"], "cleared": part["cleared"],
                        "chapterCount": len(chapters)})
        return {"parts": out, "total": total}

    def quest_catalog(self):
        """数据包里的完整任务表（tools/build_quest_catalog.py 生成）。"""
        if getattr(self, "_quest_catalog_cache", None) is None:
            path = os.path.join(_DATA_DIR, "quests.json")
            try:
                with open(path, encoding="utf-8") as handle:
                    self._quest_catalog_cache = json.load(handle)
            except Exception:
                self._quest_catalog_cache = {"quests": {}, "sections": {}, "counts": {}}
        return self._quest_catalog_cache

    def _catalog_missions(self, meta):
        out = []
        for row in (meta.get("missions") or []):
            code = row.get("code")
            text = row.get("text")
            out.append({"code": code, "text": text or code})
        while len(out) < 3:
            out.append({"code": None, "text": None})
        return out

    def quest_tree(self, query=None, only=None, include_catalog=False):
        """部 → 章 → 话 → 任务。include_catalog 时把数据包里尚未解锁的剧情一起列出。"""
        catalog = self.quest_catalog().get("quests") or {}
        stored = {}
        for row in self.rows("userQuestBattleList"):
            stored[str(row.get("questBattleId"))] = row
        section_state = {}
        for row in self.rows("userSectionList"):
            section_state[str(row.get("sectionId"))] = row

        universe = set(stored)
        if include_catalog:
            universe |= {key for key in catalog if key.isdigit()}

        needle = (query or "").lower()
        buckets = {}
        total = 0
        for qid in universe:
            row = stored.get(qid)
            meta = catalog.get(qid) or {}
            section_id = str(meta.get("sectionId")) if meta.get("sectionId") else self._section_of(qid, section_state)
            clearing = bool(row and row.get("cleared") is True)
            if only == "cleared" and not clearing:
                continue
            if only == "uncleared" and clearing:
                continue
            sec_meta = self._section_meta(section_id)
            part_key, part_label, part_order, chapter_key, chapter_label, chapter_order = self._locate(
                section_id, sec_meta.get("questType"))
            if needle and needle not in str(qid) \
                    and needle not in str(sec_meta.get("title") or "").lower() \
                    and needle not in str(sec_meta.get("area") or "").lower() \
                    and needle not in part_label.lower() and needle not in chapter_label.lower():
                continue
            state = section_state.get(section_id) or {}
            bucket = buckets.setdefault(section_id or "", {
                "sectionId": sec_meta["sectionId"], "title": sec_meta.get("title"),
                "area": sec_meta.get("area"), "questType": sec_meta.get("questType"),
                "canPlay": state.get("canPlay"), "sectionCleared": state.get("cleared"),
                "stored": bool(state), "quests": [], "total": 0, "cleared": 0, "storedCount": 0,
                "_part_key": part_key, "_part_label": part_label, "_part_order": part_order,
                "_part": part_key, "_chapter_key": chapter_key, "_chapter_label": chapter_label,
                "_chapter_order": chapter_order, "_chapter": chapter_key,
            })
            if row is not None:
                missions = [row.get("missionStatus1"), row.get("missionStatus2"), row.get("missionStatus3")]
                bucket["storedCount"] += 1
            else:
                missions = ["MISSING", "MISSING", "MISSING"]
            bucket["quests"].append({
                "questBattleId": int(qid) if qid.isdigit() else qid,
                "cleared": row.get("cleared") if row is not None else None,
                "clearCount": row.get("clearCount") if row is not None else None,
                "rewardDone": row.get("rewardDone") if row is not None else None,
                "missions": missions,
                "missionTexts": [item["text"] for item in self._catalog_missions(meta)],
                "stored": row is not None,
                "bossFlag": bool(meta.get("bossFlag")),
                "questType": meta.get("questType"),
            })
            bucket["total"] += 1
            total += 1
            if clearing:
                bucket["cleared"] += 1
        for bucket in buckets.values():
            bucket["quests"].sort(key=lambda item: str(item["questBattleId"]))
            # 游戏内没有独立任务名：用话内序号 + BOSS 标记做可读标签，ID 退成小字
            for index, quest in enumerate(bucket["quests"], start=1):
                label = "第 %d 战" % index
                if quest.get("bossFlag"):
                    label += " · BOSS"
                quest["label"] = label
        payload = self._tree_payload(list(buckets.values()), total)
        payload["includeCatalog"] = include_catalog
        payload["storedTotal"] = len(stored)
        payload["catalogTotal"] = len(catalog)
        return payload

    def section_tree(self, query=None, include_catalog=False):
        """部 → 章 → 话（小节）。include_catalog 时把数据包里所有小节一起列出。"""
        catalog_sections = self.quest_catalog().get("sections") or {}
        stored = {}
        for row in self.rows("userSectionList"):
            stored[str(row.get("sectionId"))] = row
        universe = set(stored)
        if include_catalog:
            universe |= set(catalog_sections)
            universe |= set(self.catalog.data["sections"].keys())

        needle = (query or "").lower()
        buckets = {}
        total = 0
        for section_id in universe:
            row = stored.get(section_id)
            meta = self._section_meta(section_id)
            part_key, part_label, part_order, chapter_key, chapter_label, chapter_order = self._locate(
                section_id, meta.get("questType"))
            if needle and needle not in section_id \
                    and needle not in str(meta.get("title") or "").lower() \
                    and needle not in str(meta.get("area") or "").lower() \
                    and needle not in part_label.lower() and needle not in chapter_label.lower():
                continue
            buckets[section_id] = {
                "sectionId": meta["sectionId"], "title": meta.get("title"), "area": meta.get("area"),
                "questType": meta.get("questType"),
                "canPlay": row.get("canPlay") if row is not None else None,
                "cleared": row.get("cleared") if row is not None else None,
                "stored": row is not None,
                "questCount": len(catalog_sections.get(section_id) or ()),
                "total": 1,
                "clearedCount": 1 if (row is not None and row.get("cleared") is True) else 0,
                "_part_key": part_key, "_part_label": part_label, "_part_order": part_order,
                "_part": part_key, "_chapter_key": chapter_key, "_chapter_label": chapter_label,
                "_chapter_order": chapter_order, "_chapter": chapter_key,
            }
            total += 1
        payload = self._tree_payload(list(buckets.values()), total, cleared_key="clearedCount")
        for part in payload["parts"]:
            for chapter in part["chapters"]:
                chapter["total"] = len(chapter["sections"])
                chapter["cleared"] = sum(1 for item in chapter["sections"] if item.get("cleared") is True)
                chapter["storedCount"] = sum(1 for item in chapter["sections"] if item.get("stored") is True)
            part["total"] = sum(chapter["total"] for chapter in part["chapters"])
            part["cleared"] = sum(chapter["cleared"] for chapter in part["chapters"])
            part["storedCount"] = sum(chapter.get("storedCount", 0) for chapter in part["chapters"])
        payload["includeCatalog"] = include_catalog
        payload["storedTotal"] = len(stored)
        payload["catalogTotal"] = len(self.catalog.data["sections"])
        return payload

    def raw(self, path=None):
        if path:
            return {"path": path, "value": get_value(self.profile, path)}
        return {"value": self.profile}

    # ------------------------------------------------------------ 编辑事务

    def apply(self, ops, force=False):
        if not isinstance(ops, list) or not ops:
            raise ValidationError("没有要执行的操作", code="empty_ops")
        working = copy.deepcopy(self.profile)
        before = jsjson.dumps(working)
        extra_allowed = set()
        for op in ops:
            name = op.get("op")
            handler = getattr(self, "_op_" + str(name).replace(".", "_"), None)
            if handler is None:
                raise ValidationError("未知操作: %r" % (name,), code="unknown_op")
            handler(working, op, force, extra_allowed)
        self._assert_only_supported(working, extra_allowed)
        self._validate(working)
        after = jsjson.dumps(working)
        if before == after:
            return {"changed": [], "noop": True}
        changed = diff_paths(self.profile, working)
        self.profile = working
        self.dirty = jsjson.dumps(self.profile) != self.original
        return {"changed": changed, "noop": False}

    def _assert_only_supported(self, working, extra_allowed):
        # 逐事务检查：本次修改相对「进入本事务时的内存状态」，推送成立即累计成立。
        changed = diff_paths(self.profile, working)
        blocked = [path for path in changed if path not in extra_allowed and not allowed_path(path)]
        if blocked:
            raise ValidationError(
                "这些字段不在编辑支持范围内，已回滚本次修改: " + ", ".join(blocked[:8]),
                code="unsupported_field_change",
            )

    def _validate(self, profile):
        if not isinstance(profile, dict) or profile.get("formatVersion") != 1:
            raise ValidationError("profile.formatVersion 必须为 1", code="invalid_profile")
        if profile.get("kind") not in protocol.VALID_KINDS:
            raise ValidationError("profile.kind 非法", code="invalid_profile")
        if not isinstance(profile.get("state"), dict):
            raise ValidationError("profile.state 非法", code="invalid_profile")
        if not slot.template_matches_kind(self.archive.template, profile):
            raise ValidationError("存档模板与 profile.kind 不匹配", code="save_slot_template_mismatch")
        jsjson.dumps(profile)

    def _guard(self, path, force):
        if not force:
            for protected in PROTECTED_PATHS:
                if path == protected:
                    raise ValidationError("%s 是身份字段，需 force=true 才能修改" % path,
                                          code="protected_path")

    def _ensure_row(self, profile, table, key_field, key_value, template):
        rows = profile["state"].setdefault(table, [])
        for row in rows:
            if str(row.get(key_field)) == str(key_value):
                return row
        row = dict(template)
        row[key_field] = key_value
        rows.append(row)
        return row

    # ---- raw

    def _op_raw_set(self, profile, op, force, extra):
        path = op.get("path")
        self._guard(path, force)
        try:
            current = get_value(profile, path)
        except ValidationError:
            current = _MISSING
        value = op.get("value")
        if current is not _MISSING and not force:
            self._check_type(current, value, path)
        set_value(profile, path, value)
        extra.add(path)

    def _op_raw_delete(self, profile, op, force, extra):
        path = op.get("path")
        self._guard(path, force)
        parent_path, _, leaf = path.rpartition(".")
        if not parent_path:
            raise ValidationError("不允许删除顶层字段", code="protected_path")
        parent = get_value(profile, parent_path)
        if isinstance(parent, dict):
            parent.pop(leaf, None)
        extra.add(path)

    def _check_type(self, current, value, path):
        if isinstance(current, bool):
            if not isinstance(value, bool):
                raise ValidationError("%s 原值是布尔，新值也必须是布尔（不支持的取值不会被写入）" % path,
                                      code="type_mismatch")
            return
        if isinstance(current, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError("%s 原值是数字，新值必须是数字" % path, code="type_mismatch")
            return
        if isinstance(current, str):
            if not isinstance(value, str):
                raise ValidationError("%s 原值是字符串，新值必须是字符串" % path, code="type_mismatch")
            return
        if isinstance(current, list):
            if not isinstance(value, list):
                raise ValidationError("%s 原值是数组" % path, code="type_mismatch")
            return
        if isinstance(current, dict):
            if not isinstance(value, dict):
                raise ValidationError("%s 原值是对象" % path, code="type_mismatch")

    # ---- 玩家字段

    def _op_user_set(self, profile, op, _force, extra):
        field = op.get("field")
        if not field or field in ("userId", "createdAt"):
            raise ValidationError("不允许修改 gameUser.%s" % field, code="protected_path")
        game_user = profile["state"]["gameUser"]
        value = op.get("value")
        current = game_user.get(field)
        if isinstance(current, bool):
            value = _bool(value, field)
        elif isinstance(current, (int, float)) and not isinstance(current, bool):
            value = _int(value, field, 0, 2 ** 53 - 1)
        elif isinstance(current, str):
            if not isinstance(value, str):
                raise ValidationError("%s 必须是字符串" % field, code="invalid_value")
        game_user[field] = value
        extra.add("state.gameUser." + field)

    def _op_player_set(self, profile, op, _force, extra):
        """昵称在 state.user.loginName，个人简介在 gameUser.comment；ID 之类不允许改。"""
        field = op.get("field")
        value = op.get("value")
        if field == "loginName":
            target = profile["state"].setdefault("user", {})
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("昵称不能为空", code="invalid_value")
            if len(value) > 24:
                raise ValidationError("昵称最长 24 个字符", code="invalid_value")
            target["loginName"] = value.strip()
            extra.add("state.user.loginName")
            return
        if field == "comment":
            target = profile["state"]["gameUser"]
            if not isinstance(value, str):
                raise ValidationError("简介必须是字符串", code="invalid_value")
            if len(value) > 200:
                raise ValidationError("简介最长 200 个字符", code="invalid_value")
            target["comment"] = value
            extra.add("state.gameUser.comment")
            return
        raise ValidationError("不允许修改玩家字段: %r" % (field,), code="protected_path")

    def _op_money_set(self, profile, op, _force, extra):
        kind = op.get("kind")
        amount = _int(op.get("value"), kind, 0, 2 ** 53 - 1)
        if kind == "riche":
            profile["state"]["gameUser"]["riche"] = amount
            extra.add("state.gameUser.riche")
            return
        if kind in ("MONEY", "PRESENTED_MONEY", "YELL", "ARENA_COIN", "PRISM", "GACHA_TICKET",
                    "SCENEZERO_PLAY_TICKET"):
            self._op_item_set(profile, {"itemId": kind, "quantity": amount}, False, extra)
            return
        raise ValidationError("未知货币类型: %r" % (kind,), code="invalid_value")

    # ---- 道具 / 礼物

    def _op_chara_add(self, profile, op, _force, extra):
        """直接获取一名魔法少女：默认取其最低星级卡，同角色不允许重复。"""
        chara_id = op.get("charaId")
        if chara_id is None:
            raise ValidationError("需要 charaId", code="invalid_value")
        master = self.catalog.chara(chara_id)
        if not master.get("name"):
            raise ValidationError("角色主数据里没有 %s" % (chara_id,), code="path_missing")
        card_id = op.get("cardId")
        if card_id is None:
            candidates = []
            for key, row in (self.masters.cards or {}).items():
                if str(row.get("charaNo")) != str(chara_id):
                    continue
                try:
                    rank = growth.card_max_level(row.get("rank"))
                except growth.GrowthError:
                    continue
                candidates.append((rank, row.get("cardId")))
            if not candidates:
                raise ValidationError("角色 %s 没有可用的卡片定义" % (chara_id,),
                                      code="missing_growth_master")
            candidates.sort()
            card_id = candidates[0][1]
        return self._op_card_add(profile, {"charaId": chara_id, "cardId": card_id,
                                          "level": op.get("level", 1)}, _force, extra)

    def chara_catalog(self, query=None, offset=0, limit=60):
        """全部已实装魔法少女（供「添加角色」选择）。"""
        owned = {str(row.get("charaId")) for row in self.rows("userCharaList")}
        cards_by_chara = {}
        for key, row in (self.masters.cards or {}).items():
            chara_no = row.get("charaNo")
            if chara_no is None:
                continue
            try:
                rank = growth.card_max_level(row.get("rank"))
            except growth.GrowthError:
                continue
            cards_by_chara.setdefault(str(chara_no), []).append({
                "cardId": row.get("cardId"), "rank": row.get("rank"), "rankLevel": rank,
                "name": row.get("cardName") or self.catalog.card_name(row.get("cardId")),
            })
        needle = (query or "").lower()
        rows = []
        for key, meta in self.catalog.data["chara"].items():
            name = self.catalog.chara_name(key)
            if needle and needle not in str(key) and needle not in str(name).lower():
                continue
            cards = sorted(cards_by_chara.get(key) or [], key=lambda item: item["rankLevel"])
            if not cards:
                # 没有任何卡片数据（无法生成合法卡）的角色直接不列出
                continue
            rows.append({
                "charaId": int(key) if str(key).isdigit() else key,
                "name": name,
                "title": meta.get("title"),
                "attribute": meta.get("attribute"),
                "attributeLabel": self.catalog.attribute_label(meta.get("attribute")),
                "owned": key in owned,
                "addable": key not in owned and bool(cards),
                "cards": cards[:5],
                "defaultCard": cards[0] if cards else None,
                "icon": "/assets/cards/%s.png" % (cards[0]["cardId"] if cards else ""),
                "face": "/assets/faces/%s.png" % (cards[0]["cardId"] if cards else ""),
            })
        rows.sort(key=lambda item: (str(item["name"])))
        total = len(rows)
        return {"total": total, "offset": offset, "limit": limit, "rows": rows[offset: offset + limit]}

    def _op_item_set(self, profile, op, _force, extra):
        code = op.get("itemId")
        quantity = _int(op.get("quantity"), "quantity", 0, 2 ** 53 - 1)
        row = self._ensure_row(profile, "userItemList", "itemId", code, {
            "userId": profile["state"]["gameUser"]["userId"], "quantity": 0, "total": 0,
            "environmentId": "COMMON", "createdAt": self.stamp()})
        row["quantity"] = quantity
        if op.get("total") is not None:
            row["total"] = _int(op.get("total"), "total", 0, 2 ** 53 - 1)
        extra.add("state.userItemList[itemId=%s].quantity" % code)

    def _op_item_add(self, profile, op, _force, extra):
        code = op.get("itemId")
        delta = _int(op.get("quantity"), "quantity", -2 ** 53 + 1, 2 ** 53 - 1)
        row = self._ensure_row(profile, "userItemList", "itemId", code, {
            "userId": profile["state"]["gameUser"]["userId"], "quantity": 0, "total": 0,
            "environmentId": "COMMON", "createdAt": self.stamp()})
        current = _int(row.get("quantity") or 0, "quantity", 0, 2 ** 53 - 1)
        row["quantity"] = max(0, current + delta)
        extra.add("state.userItemList[itemId=%s].quantity" % code)

    def _op_item_delete(self, profile, op, _force, extra):
        code = op.get("itemId")
        rows = profile["state"].get("userItemList") or []
        keep = [row for row in rows if str(row.get("itemId")) != str(code)]
        if len(keep) == len(rows):
            raise ValidationError("没有该道具: %s" % code, code="path_missing")
        profile["state"]["userItemList"] = keep
        extra.add("state.userItemList")

    def _op_gift_set(self, profile, op, _force, extra):
        gift_id = op.get("giftId")
        quantity = _int(op.get("quantity"), "quantity", 0, 2 ** 53 - 1)
        row = self._ensure_row(profile, "userGiftList", "giftId", gift_id, {
            "userId": profile["state"]["gameUser"]["userId"], "quantity": 0, "total": 0,
            "createdAt": self.stamp()})
        row["quantity"] = quantity
        extra.add("state.userGiftList[giftId=%s].quantity" % gift_id)

    # ---- 状态点

    def _op_status_set(self, profile, op, _force, extra):
        status_id = op.get("statusId")
        point = _int(op.get("point"), "point", 0, 2 ** 53 - 1)
        if not status_id:
            raise ValidationError("statusId 必填", code="invalid_value")
        row = self._ensure_row(profile, "userStatusList", "statusId", status_id, {
            "userId": profile["state"]["gameUser"]["userId"], "point": 0,
            "checkedAt": self.stamp(), "createdAt": self.stamp()})
        row["point"] = point
        extra.add("state.userStatusList[statusId=%s].point" % status_id)
        meta = STATUS_META.get(status_id) or {}
        max_id = op.get("maxStatusId") or meta.get("max")
        if op.get("maxPoint") is not None and max_id:
            max_point = _int(op["maxPoint"], "maxPoint", 0, 2 ** 53 - 1)
            max_row = self._ensure_row(profile, "userStatusList", "statusId", max_id, {
                "userId": profile["state"]["gameUser"]["userId"], "point": 0,
                "checkedAt": self.stamp(), "createdAt": self.stamp()})
            max_row["point"] = max_point
            extra.add("state.userStatusList[statusId=%s].point" % max_id)

    # ---- 卡片（数值只读，按等级推算）

    def _find_card(self, profile, op):
        rows = profile["state"].get("userCardList") or []
        key = op.get("id") or op.get("userCardId")
        if key:
            for row in rows:
                if row.get("id") == key:
                    return row
        if op.get("cardId") is not None:
            hits = [row for row in rows if str(row.get("cardId")) == str(op.get("cardId"))]
            if len(hits) == 1:
                return hits[0]
        raise ValidationError("找不到卡片: %r" % (key or op.get("cardId"),), code="path_missing")

    def _op_card_add(self, profile, op, _force, extra):
        """新增一张卡。同一角色只能有一张卡（不同星级不可并存，且不支持降星级）。"""
        chara_id = op.get("charaId")
        card_id = op.get("cardId")
        master = self.masters.card(card_id)
        if not master:
            raise ValidationError("卡片 %s 没有基础数值，无法生成合法行" % (card_id,),
                                  code="missing_growth_master")
        if chara_id is not None and str(master.get("charaNo")) != str(chara_id):
            raise ValidationError("卡片 %s 属于角色 %s，不是 %s"
                                  % (card_id, master.get("charaNo"), chara_id), code="card_owner_mismatch")
        owner = profile["state"]["gameUser"]["userId"]
        cards = profile["state"].setdefault("userCardList", [])
        charas = profile["state"].setdefault("userCharaList", [])
        target_chara = master.get("charaNo")

        # 先按角色判重：已经持有该角色任何星级都不允许再来一张
        for row in charas:
            if str(row.get("charaId")) != str(target_chara):
                continue
            owned_card = None
            for card in cards:
                if card.get("id") == row.get("userCardId"):
                    owned_card = card
                    break
            owned_master = self.masters.card((owned_card or {}).get("cardId")) or {}
            try:
                owned_rank = growth.card_max_level(owned_master.get("rank"))
                new_rank = growth.card_max_level(master.get("rank"))
            except growth.GrowthError:
                owned_rank = new_rank = None
            if owned_rank is not None and new_rank is not None and new_rank <= owned_rank:
                raise ValidationError("角色 %s 已持有更高或同星级卡（游戏不支持降星级）" % target_chara,
                                      code="chara_downgrade_blocked")
            raise ValidationError("角色 %s 已经持有卡片；升星需要觉醒流程，未在本编辑器实现" % target_chara,
                                  code="chara_already_owned")

        for row in cards:
            if str(row.get("cardId")) == str(card_id):
                raise ValidationError("这张卡已拥有", code="already_present")

        level = _int(op.get("level", 1), "level", 1, growth.card_max_level(master["rank"]))
        stats = growth.card_stats(master, level)
        stamp = self.stamp()
        card_id_value = str(uuid.uuid4())
        cards.append({
            "id": card_id_value,
            "userId": owner,
            "cardId": master.get("cardId"),
            "displayCardId": master.get("cardId"),
            "level": level,
            "experience": 0,
            "magiaLevel": 1,
            "revision": 0,
            "attack": stats["attack"],
            "defense": stats["defense"],
            "hp": stats["hp"],
            "enabled": True,
            "customized1": False,
            "customized2": False,
            "customized3": False,
            "customized4": False,
            "customized5": False,
            "customized6": False,
            "createdAt": stamp,
        })
        charas.append({
            "userId": owner,
            "charaId": target_chara,
            "userCardId": card_id_value,
            "live2dId": "00",
            "lbItemNum": 0,
            "bondsTotalPt": 0,
            "commandVisualType": "CHARA",
            "commandVisualId": master.get("charaNo"),
            "addendAttack": 0,
            "addendDefense": 0,
            "addendHp": 0,
            "addendAccelBonus": 0,
            "addendBlastBonus": 0,
            "addendChargeBonus": 0,
            "visualizable": True,
            "emotionSkillList": [],
            "createdAt": stamp,
        })
        extra.add("state.userCardList")
        extra.add("state.userCharaList")

    def _op_card_swap(self, profile, op, _force, extra):
        """把某个角色当前的卡换成同角色的另一张卡（允许升星，也允许按需求降星）。

        数值按新卡定义与游戏成长公式重算；等级/经验按新卡上限截断。
        """
        card_id = op.get("cardId")
        master = self.masters.card(card_id)
        if not master:
            raise ValidationError("卡片 %s 没有基础数值" % (card_id,), code="missing_growth_master")
        chara_row = None
        for row in profile["state"].get("userCharaList") or []:
            if op.get("userCardId") and row.get("userCardId") == op.get("userCardId"):
                chara_row = row
                break
            if op.get("charaId") is not None and str(row.get("charaId")) == str(op.get("charaId")):
                chara_row = row
                break
        if chara_row is None:
            raise ValidationError("找不到角色", code="path_missing")
        if str(master.get("charaNo")) != str(chara_row.get("charaId")):
            raise ValidationError("卡片 %s 不属于角色 %s" % (card_id, chara_row.get("charaId")),
                                  code="card_owner_mismatch")
        card = None
        for row in profile["state"].get("userCardList") or []:
            if row.get("id") == chara_row.get("userCardId"):
                card = row
                break
        if card is None:
            raise ValidationError("该角色没有卡片行", code="path_missing")
        cap = growth.card_max_level(master["rank"])
        try:
            raw_level = int(card.get("level") or 1)
        except (TypeError, ValueError):
            raw_level = 1
        level = max(1, min(raw_level, cap))   # 降星级时按新上限截断，不报错
        stats = growth.card_stats(master, level)
        card["cardId"] = master.get("cardId")
        card["displayCardId"] = master.get("cardId")
        card["level"] = level
        card["experience"] = 0
        card["attack"] = stats["attack"]
        card["defense"] = stats["defense"]
        card["hp"] = stats["hp"]
        card["magiaLevel"] = min(_int(card.get("magiaLevel") or 1, "magiaLevel", 1, 5), 5)
        card["revision"] = min(_int(card.get("revision") or 0, "revision", 0, 3), 3)
        for key in ("cardId", "displayCardId", "level", "experience", "attack", "defense", "hp",
                    "magiaLevel", "revision"):
            extra.add("state.userCardList[%d].%s" % (profile["state"]["userCardList"].index(card), key))

    def _op_card_set(self, profile, op, _force, extra):
        card = self._find_card(profile, op)
        field = op.get("field")
        if field in ("attack", "defense", "hp"):
            raise ValidationError(
                "attack/defense/hp 由等级推算，不能直接改；请改 level 或 experience",
                code="stat_readonly")
        if field not in ("level", "experience", "magiaLevel", "revision", "displayCardId", "enabled",
                         "customized1", "customized2", "customized3", "customized4",
                         "customized5", "customized6"):
            raise ValidationError("不允许修改卡片字段: %r" % (field,), code="invalid_value")
        if field in ("enabled", "customized1", "customized2", "customized3", "customized4",
                     "customized5", "customized6"):
            card[field] = _bool(op.get("value"), field)
            extra.add("state.userCardList[id=%s].%s" % (card.get("id"), field))
            return
        if field == "displayCardId":
            card[field] = _int(op.get("value"), field, 1, 2 ** 53 - 1)
            extra.add("state.userCardList[id=%s].%s" % (card.get("id"), field))
            return
        if field == "revision":
            card[field] = _int(op.get("value"), field, 0, 3)
            extra.add("state.userCardList[id=%s].revision" % card.get("id"))
            return
        if field == "magiaLevel":
            card[field] = _int(op.get("value"), field, 1, 5)
            extra.add("state.userCardList[id=%s].magiaLevel" % card.get("id"))
            return
        master = self.masters.card(card.get("cardId"))
        if not master:
            raise ValidationError(
                "卡片 %s 没有成长数据（数据包内未收录），无法按游戏逻辑推算数值；"
                "等级与经验保持原样" % card.get("cardId"), code="missing_growth_master")
        if field == "level":
            level = _int(op.get("value"), "level", 1, growth.card_max_level(master["rank"]))
            card["level"] = level
            card["experience"] = 0
            card.update(growth.card_stats(master, level))
        else:
            level = _int(card.get("level"), "level", 1, growth.card_max_level(master["rank"]))
            cap = growth.card_experience_cap(level, master["rank"])
            card["experience"] = _int(op.get("value"), "experience", 0, max(0, cap - 1) if cap else 0)
        for key in ("level", "experience", "attack", "defense", "hp"):
            extra.add("state.userCardList[id=%s].%s" % (card.get("id"), key))

    def _op_card_set_all_levels(self, profile, op, _force, extra):
        level = _int(op.get("level"), "level", 1, 100)
        touched = 0
        skipped = []
        for card in profile["state"].get("userCardList") or []:
            master = self.masters.card(card.get("cardId"))
            if not master:
                skipped.append(card.get("cardId"))
                continue
            cap = growth.card_max_level(master["rank"])
            use = min(level, cap)
            card["level"] = use
            card["experience"] = 0
            card.update(growth.card_stats(master, use))
            for key in ("level", "experience", "attack", "defense", "hp"):
                extra.add("state.userCardList[id=%s].%s" % (card.get("id"), key))
            touched += 1
        if not touched:
            raise ValidationError("没有可推算的卡片（缺少成长数据）", code="missing_growth_master")

    # ---- 角色

    def _op_chara_set(self, profile, op, _force, extra):
        rows = profile["state"].get("userCharaList") or []
        target = None
        for row in rows:
            if op.get("userCardId") and row.get("userCardId") == op.get("userCardId"):
                target = row
                break
            if op.get("charaId") is not None and row.get("charaId") == op.get("charaId"):
                target = row
                break
        if target is None:
            raise ValidationError("找不到角色", code="path_missing")
        field = op.get("field")
        if field in ("addendAttack", "addendDefense", "addendHp", "addendAccelBonus",
                     "addendBlastBonus", "addendChargeBonus"):
            raise ValidationError("角色加成数值不能直接改（由属性强化系统结算）", code="stat_readonly")
        if field not in ("lbItemNum", "bondsTotalPt", "live2dId", "emotionSkillList"):
            raise ValidationError("不允许修改角色字段: %r" % (field,), code="invalid_value")
        if field == "live2dId" or field == "emotionSkillList":
            target[field] = op.get("value")
        else:
            target[field] = _int(op.get("value"), field, 0, 2 ** 53 - 1)
        extra.add("state.userCharaList[userCardId=%s].%s" % (target.get("userCardId"), field))

# ---- 剧情与衣装

    def _story_meta(self, section_id):
        sections = self.stories_catalog().get("sections") or {}
        meta = sections.get(str(section_id))
        if not meta:
            raise ValidationError("没有剧情段 %s" % section_id, code="path_missing")
        return meta

    def _op_story_set(self, profile, op, _force, extra):
        """整段剧情通关 / 回退：连段内任务一起，必要时补出存档里缺的行。"""
        chara_row = self._find_chara_row(profile, op.get("charaId"))
        cleared = _bool(op.get("cleared"), "cleared")
        section_id = _int(op.get("sectionId"), "sectionId", 1)
        meta = self._story_meta(section_id)
        owner_id = meta.get("charaId")
        if owner_id and str(owner_id) != str(chara_row.get("charaId")):
            raise ValidationError("剧情段 %s 属于角色 %s" % (section_id, owner_id),
                                  code="story_owner_mismatch")
        owner = profile["state"]["gameUser"]["userId"]
        stamp = self.stamp()
        sections = profile["state"].setdefault("userSectionList", [])
        target = None
        for row in sections:
            if str(row.get("sectionId")) == str(section_id):
                target = row
                break
        if target is None:
            target = {"userId": owner, "sectionId": section_id, "canPlay": True,
                      "cleared": cleared, "createdAt": stamp}
            sections.append(target)
            extra.add("state.userSectionList")
        elif target.get("cleared") is not cleared:
            target["cleared"] = cleared
            extra.add("state.userSectionList[sectionId=%s].cleared" % section_id)
        quests = profile["state"].setdefault("userQuestBattleList", [])
        index = {}
        for row in quests:
            index[str(row.get("questBattleId"))] = row
        for qid in meta.get("questBattleIds") or []:
            row = index.get(str(qid))
            if row is None:
                if not cleared:
                    continue
                quests.append({
                    "userId": owner,
                    "questBattleId": int(qid),
                    "cleared": True,
                    "clearCount": 1,
                    "rewardDone": False,
                    "maxDamage": 0,
                    "missionStatus1": MISSION_NOT_CLEAR,
                    "missionStatus2": MISSION_NOT_CLEAR,
                    "missionStatus3": MISSION_NOT_CLEAR,
                    "createdAt": stamp,
                })
                index[str(qid)] = quests[-1]
                extra.add("state.userQuestBattleList")
                continue
            base = "state.userQuestBattleList[questBattleId=%s]" % qid
            if row.get("cleared") is not cleared:
                row["cleared"] = cleared
                extra.add(base + ".cleared")
            want = max(1, int(row.get("clearCount") or 0)) if cleared else 0
            if int(row.get("clearCount") or 0) != want:
                row["clearCount"] = want
                extra.add(base + ".clearCount")
            for index_no in range(1, 4):
                key = "missionStatus%d" % index_no
                if row.get(key) in (MISSION_CLEARED, MISSION_NOT_CLEAR):
                    value = MISSION_CLEARED if cleared else MISSION_NOT_CLEAR
                    if row.get(key) != value:
                        row[key] = value
                        extra.add(base + "." + key)

    def _costume_entry(self, chara_id, live2d_id):
        rows = (self.costumes_catalog().get("charaCostumes") or {}).get(str(chara_id)) or []
        for row in rows:
            if str(row.get("live2dId")) == str(live2d_id):
                return row
        return None

    def _op_doppel_set(self, profile, op, _force, extra):
        """魔女化身解锁开关：只写 userDoppelList，不改卡片与 magia 等级。"""
        chara_row = self._find_chara_row(profile, op.get("charaId"))
        unlocked = _bool(op.get("unlocked"), "unlocked")
        meta = (self.doppels_catalog().get("charaDoppel") or {}).get(str(chara_row.get("charaId"))) or {}
        doppel_id = meta.get("doppelId")
        if not doppel_id:
            raise ValidationError("角色 %s 没有魔女化身" % chara_row.get("charaId"), code="path_missing")
        rows = profile["state"].setdefault("userDoppelList", [])
        hits = [row for row in rows if str(row.get("doppelId")) == str(doppel_id)]
        if unlocked:
            if hits:
                raise ValidationError("这个魔女化身已经解锁了", code="already_present")
            rows.append({
                "userId": profile["state"]["gameUser"]["userId"],
                "doppelId": int(doppel_id),
                "createdAt": self.stamp(),
            })
            extra.add("state.userDoppelList")
        else:
            if not hits:
                raise ValidationError("这个魔女化身还没解锁", code="path_missing")
            for row in hits:
                rows.remove(row)
            extra.add("state.userDoppelList")

    def _op_costume_add(self, profile, op, _force, extra):
        chara_row = self._find_chara_row(profile, op.get("charaId"))
        live2d_id = str(op.get("live2dId"))
        if self._costume_entry(chara_row.get("charaId"), live2d_id) is None:
            raise ValidationError("角色 %s 没有衣装 %s" % (chara_row.get("charaId"), live2d_id),
                                  code="path_missing")
        master_ids = self.live2d_ids(chara_row.get("charaId"))
        if master_ids is not None and live2d_id not in master_ids:
            raise ValidationError(
                "衣装 %s 不在游戏主数据里（客户端读取会失败），不能添加" % live2d_id,
                code="costume_unknown_live2d")
        rows = profile["state"].setdefault("userLive2dList", [])
        for row in rows:
            if (str(row.get("charaId")) == str(chara_row.get("charaId"))
                    and str(row.get("live2dId")) == live2d_id):
                raise ValidationError("这件衣装已经拥有了", code="already_present")
        rows.append({
            "userId": profile["state"]["gameUser"]["userId"],
            "charaId": chara_row.get("charaId"),
            "live2dId": live2d_id,
        })
        extra.add("state.userLive2dList")

    def _op_costume_clean(self, profile, op, _force, extra):
        """删掉 userLive2dList 里主数据不存在的衣装（这份存档进不去游戏的常见原因）。"""
        master = self.live2d_master().get("chara") or {}
        rows = profile["state"].get("userLive2dList") or []
        removed = []
        for row in list(rows):
            chara_id = str(row.get("charaId"))
            allowed = master.get(chara_id)
            if allowed is None:
                continue
            if str(row.get("live2dId")) not in allowed:
                rows.remove(row)
                removed.append((row.get("charaId"), row.get("live2dId")))
        if not removed:
            raise ValidationError("没有主数据里不存在的衣装", code="already_present")
        extra.add("state.userLive2dList")

    def _op_costume_remove(self, profile, op, _force, extra):
        chara_row = self._find_chara_row(profile, op.get("charaId"))
        live2d_id = str(op.get("live2dId"))
        entry = self._costume_entry(chara_row.get("charaId"), live2d_id) or {}
        if live2d_id == "00" or entry.get("default"):
            raise ValidationError("默认服装不能设为未获取（战斗与剧情都要用它）",
                                  code="costume_default_locked")
        if str(chara_row.get("live2dId")) == live2d_id:
            raise ValidationError("这件衣装正在穿着，先换成别的再取消获取", code="costume_in_use")
        rows = profile["state"].get("userLive2dList") or []
        target = None
        for row in rows:
            if (str(row.get("charaId")) == str(chara_row.get("charaId"))
                    and str(row.get("live2dId")) == live2d_id):
                target = row
                break
        if target is None:
            raise ValidationError("这件衣装还没获取", code="path_missing")
        rows.remove(target)
        extra.add("state.userLive2dList")

    def _op_costume_equip(self, profile, op, _force, extra):
        chara_row = self._find_chara_row(profile, op.get("charaId"))
        live2d_id = str(op.get("live2dId"))
        entry = self._costume_entry(chara_row.get("charaId"), live2d_id)
        if entry is None:
            raise ValidationError("角色 %s 没有衣装 %s" % (chara_row.get("charaId"), live2d_id),
                                  code="path_missing")
        owned = any(str(row.get("charaId")) == str(chara_row.get("charaId"))
                    and str(row.get("live2dId")) == live2d_id
                    for row in self.rows("userLive2dList"))
        if not owned and not (live2d_id == "00" or entry.get("default")):
            raise ValidationError("还没获取这件衣装", code="costume_not_owned")
        if str(chara_row.get("live2dId")) == live2d_id:
            raise ValidationError("已经是这件衣装", code="already_present")
        chara_row["live2dId"] = live2d_id
        extra.add("state.userCharaList[charaId=%s].live2dId" % chara_row.get("charaId"))

    # ---- 记忆结晶

    def _find_piece(self, profile, op):
        rows = profile["state"].get("userPieceList") or []
        if op.get("id"):
            for row in rows:
                if row.get("id") == op.get("id"):
                    return row
        if op.get("pieceId") is not None:
            hits = [row for row in rows if str(row.get("pieceId")) == str(op.get("pieceId"))]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise ValidationError("该结晶有多枚，请用 id 指定", code="path_ambiguous")
        raise ValidationError("找不到记忆结晶", code="path_missing")

    def _op_piece_set(self, profile, op, _force, extra):
        piece = self._find_piece(profile, op)
        field = op.get("field")
        if field in ("attack", "defense", "hp"):
            raise ValidationError(
                "attack/defense/hp 由等级与突破推算，不能直接改；请改 level / lbCount",
                code="stat_readonly")
        if field not in ("level", "lbCount", "experience", "protect", "archive"):
            raise ValidationError("不允许修改结晶字段: %r" % (field,), code="invalid_value")
        if field == "protect":
            piece[field] = _bool(op.get("value"), field)
            extra.add("state.userPieceList[id=%s].protect" % piece.get("id"))
            return
        if field == "archive":
            piece[field] = _int(op.get("value"), field, 0, 2 ** 53 - 1)
            extra.add("state.userPieceList[id=%s].archive" % piece.get("id"))
            return
        master = self.masters.piece(piece.get("pieceId"))
        if not master:
            raise ValidationError("结晶 %s 没有成长数据，无法推算数值" % piece.get("pieceId"),
                                  code="missing_growth_master")
        lb_count = piece.get("lbCount") or 0
        if field == "lbCount":
            lb_count = _int(op.get("value"), "lbCount", 0, 4)
            piece["lbCount"] = lb_count
        level = piece.get("level")
        cap = growth.piece_max_level(master["rank"], lb_count)
        if field == "level":
            level = _int(op.get("value"), "level", 1, cap)
            piece["level"] = level
            piece["experience"] = 0
        elif field == "experience":
            level = _int(level, "level", 1, cap)
            cap_exp = growth.piece_experience_cap(level, master["rank"], lb_count)
            piece["experience"] = _int(op.get("value"), "experience", 0, max(0, cap_exp - 1) if cap_exp else 0)
        else:
            level = min(_int(level, "level", 1, cap), cap)
            piece["level"] = level
        piece.update(growth.piece_stats(master, piece["level"], lb_count))
        for key in ("level", "experience", "lbCount", "attack", "defense", "hp"):
            extra.add("state.userPieceList[id=%s].%s" % (piece.get("id"), key))

    def _op_piece_set_all_levels(self, profile, op, _force, extra):
        level = _int(op.get("level"), "level", 1, 50)
        touched = 0
        for piece in profile["state"].get("userPieceList") or []:
            master = self.masters.piece(piece.get("pieceId"))
            if not master:
                continue
            lb = piece.get("lbCount") or 0
            cap = growth.piece_max_level(master["rank"], lb)
            use = min(level, cap)
            piece["level"] = use
            piece["experience"] = 0
            piece.update(growth.piece_stats(master, use, lb))
            for key in ("level", "experience", "attack", "defense", "hp"):
                extra.add("state.userPieceList[id=%s].%s" % (piece.get("id"), key))
            touched += 1
        if not touched:
            raise ValidationError("没有可推算的结晶（缺少成长数据）", code="missing_growth_master")

    def _op_piece_add(self, profile, op, _force, extra):
        piece_id = op.get("pieceId")
        master = self.masters.piece(piece_id)
        if not master:
            raise ValidationError("结晶 %s 没有官方基础数值，无法生成合法行" % (piece_id,),
                                  code="missing_growth_master")
        rows = profile["state"].setdefault("userPieceList", [])
        # 记忆结晶可以重复获取：同一 pieceId 允许多枚，每枚有独立的 id 与等级/突破。
        lb_count = _int(op.get("lbCount", 0), "lbCount", 0, 4)
        cap = growth.piece_max_level(master["rank"], lb_count)
        level = _int(op.get("level", 1), "level", 1, cap)
        stats = growth.piece_stats(master, level, lb_count)
        owner = profile["state"]["gameUser"]["userId"]
        row = {
            "id": str(uuid.uuid4()),
            "userId": owner,
            "pieceId": int(piece_id) if str(piece_id).isdigit() else piece_id,
            "level": level,
            "experience": 0,
            "lbCount": lb_count,
            "attack": stats["attack"],
            "defense": stats["defense"],
            "hp": stats["hp"],
            "protect": False,
            "archive": 0,
            "createdAt": self.stamp(),
        }
        rows.append(row)
        extra.add("state.userPieceList")
        return row

    def _op_piece_delete(self, profile, op, _force, extra):
        rows = profile["state"].get("userPieceList") or []
        target = self._find_piece(profile, op)
        rows.remove(target)
        extra.add("state.userPieceList")

    # ---- 队伍编成

    def _deck_target(self, profile, op):
        deck_type = op.get("deckType")
        for deck in profile["state"].get("userDeckList") or []:
            if str(deck.get("deckType")) == str(deck_type):
                return deck
        raise ValidationError("找不到队伍: %r" % (deck_type,), code="path_missing")

    def _op_formation_add(self, profile, op, _force, extra):
        formation_id = _int(op.get("formationSheetId"), "formationSheetId", 1)
        if not self.formation_meta(formation_id):
            raise ValidationError("没有阵型 %s" % formation_id, code="path_missing")
        rows = profile["state"].setdefault("userFormationSheetList", [])
        for row in rows:
            if str(row.get("formationSheetId")) == str(formation_id):
                raise ValidationError("这个阵型已经获取了", code="already_present")
        rows.append({
            "userId": profile["state"]["gameUser"]["userId"],
            "formationSheetId": formation_id,
            "createdAt": self.stamp(),
        })
        extra.add("state.userFormationSheetList")

    def _op_formation_remove(self, profile, op, _force, extra):
        formation_id = _int(op.get("formationSheetId"), "formationSheetId", 1)
        for deck in profile["state"].get("userDeckList") or []:
            if str(deck.get("formationSheetId")) == str(formation_id):
                raise ValidationError("阵型 %s 正被「%s」使用，先换掉再取消获取"
                                      % (formation_id, deck.get("name") or deck.get("deckType")),
                                      code="formation_in_use")
        rows = profile["state"].get("userFormationSheetList") or []
        target = None
        for row in rows:
            if str(row.get("formationSheetId")) == str(formation_id):
                target = row
                break
        if target is None:
            raise ValidationError("这个阵型还没获取", code="path_missing")
        rows.remove(target)
        extra.add("state.userFormationSheetList")

    def _op_deck_formation(self, profile, op, _force, extra):
        deck = self._deck_target(profile, op)
        formation_id = _int(op.get("formationSheetId"), "formationSheetId", 1)
        meta = self.formation_meta(formation_id)
        if not meta:
            raise ValidationError("没有阵型 %s" % formation_id, code="path_missing")
        if not self.formation_owned(profile, formation_id):
            raise ValidationError("阵型 %s 还没获取，先获取再用" % formation_id, code="formation_not_owned")
        slots = sorted(meta.get("slots") or [])
        if len(slots) < 5:
            raise ValidationError("阵型 %s 的格子不足 5 个" % formation_id, code="formation_slots_invalid")
        deck["formationSheetId"] = formation_id
        for index in range(1, 5):
            deck["questPositionId%d" % index] = slots[index - 1]
        deck["questPositionHelper"] = slots[4]
        extra.add("state.userDeckList[deckType=%s]" % deck.get("deckType"))

    def _op_deck_helper(self, profile, op, _force, extra):
        deck = self._deck_target(profile, op)
        position = _int(op.get("position"), "position", 1, 9)
        formation_id = deck.get("formationSheetId")
        meta = self.formation_meta(formation_id)
        slots = sorted(meta.get("slots") or [])
        if slots and position not in slots:
            raise ValidationError("阵型 %s 没有第 %s 个格子" % (formation_id, position),
                                  code="deck_position_invalid")
        used = {deck.get("questPositionId%d" % index) for index in range(1, 5)}
        if position in used:
            raise ValidationError("第 %s 个格子已经有上阵角色了" % position, code="deck_position_taken")
        deck["questPositionHelper"] = position
        extra.add("state.userDeckList[deckType=%s]" % deck.get("deckType"))

    @staticmethod
    def _sync_quest_deck_leader(profile, deck, extra):
        # A quest leader must belong to the active team, not the separate support position.
        if not isinstance(deck.get("deckType"), int) or deck["deckType"] >= 20:
            return
        members = [deck["userCardId%d" % i] for i in range(1, 11)
                   if deck.get("userCardId%d" % i)
                   and deck.get("questPositionId%d" % i) in range(1, 10)]
        if deck.get("questEpisodeUserCardId") not in members:
            row = profile["state"]["userDeckList"].index(deck)
            extra.add("state.userDeckList[%d].questEpisodeUserCardId" % row)
            if members:
                deck["questEpisodeUserCardId"] = members[0]
            else:
                deck.pop("questEpisodeUserCardId", None)

    def _op_deck_member(self, profile, op, _force, extra):
        """队伍第 1..4 位可编辑；支援位由好友填充，不在这里。"""
        deck = self._deck_target(profile, op)
        deck_type = deck.get("deckType")
        if not isinstance(deck_type, int) or deck_type >= 20:
            raise ValidationError("这个编成不是玩家队伍，不能编辑", code="deck_not_editable")
        slot = _int(op.get("slot"), "slot", 1, 4)
        user_card_id = op.get("userCardId")
        card = None
        for row in profile["state"].get("userCardList") or []:
            if str(row.get("id")) == str(user_card_id):
                card = row
                break
        if card is None:
            raise ValidationError("找不到卡片: %r" % (user_card_id,), code="path_missing")
        for index in range(1, 5):
            if index != slot and str(deck.get("userCardId%d" % index)) == str(user_card_id):
                raise ValidationError("这张卡已经在队伍里了", code="already_present")
        deck["userCardId%d" % slot] = user_card_id
        for piece_slot in range(1, 6):
            deck.pop("userPieceId%02d%d" % (slot, piece_slot), None)
        position = op.get("position")
        if position is not None:
            position = _int(position, "position", 1, 9)
            formation = self.formation_meta(deck.get("formationSheetId"))
            slots_available = formation.get("slots") or []
            if slots_available and position not in slots_available:
                raise ValidationError("阵型 %s 没有第 %s 个格子"
                                      % (deck.get("formationSheetId"), position),
                                      code="deck_position_invalid")
            for index in range(1, 5):
                if index != slot and deck.get("questPositionId%d" % index) == position:
                    raise ValidationError("第 %s 个格子已经有角色了" % position,
                                          code="deck_position_taken")
            deck["questPositionId%d" % slot] = position
        self._sync_quest_deck_leader(profile, deck, extra)
        extra.add("state.userDeckList[deckType=%s]" % deck_type)

    def _op_deck_removeMember(self, profile, op, _force, extra):
        deck = self._deck_target(profile, op)
        slot = _int(op.get("slot"), "slot", 1, 4)
        if not deck.get("userCardId%d" % slot):
            raise ValidationError("这个位置没有角色", code="path_missing")
        deck.pop("userCardId%d" % slot, None)
        for piece_slot in range(1, 6):
            deck.pop("userPieceId%02d%d" % (slot, piece_slot), None)
        self._sync_quest_deck_leader(profile, deck, extra)
        extra.add("state.userDeckList[deckType=%s]" % deck.get("deckType"))

    def _op_deck_swap(self, profile, op, _force, extra):
        """换位：交换两个槽位在九宫格里的位置（a / b 取 1-4 或 "helper"）。"""
        deck = self._deck_target(profile, op)

        def locate(slot):
            if str(slot).lower() == "helper":
                return "questPositionHelper", None
            index = _int(slot, "slot", 1, 4)
            return "questPositionId%d" % index, index

        key_a, member_a = locate(op.get("a"))
        key_b, member_b = locate(op.get("b"))
        if key_a == key_b:
            raise ValidationError("同一个位置不用换", code="invalid_value")
        for member in (member_a, member_b):
            if member is not None and not deck.get("userCardId%d" % member):
                raise ValidationError("第 %s 位还没有角色" % member,
                                      code="deck_member_missing")
        value_a, value_b = deck.get(key_a), deck.get(key_b)
        deck[key_a], deck[key_b] = value_b, value_a
        extra.add("state.userDeckList[deckType=%s]" % deck.get("deckType"))

    def piece_capacity(self, profile, user_card_id):
        """记忆结晶槽位：初始 1，每次魔力解放（card.revision）+1，最多 4。

        魔力解放消耗命运宝石（userCharaList.lbItemNum），提升的是 card.revision；
        lbItemNum 只是宝石持有数，不是已解放次数。
        """
        card = None
        for row in profile["state"].get("userCardList") or []:
            if str(row.get("id")) == str(user_card_id):
                card = row
                break
        released = int((card or {}).get("revision") or 0)
        return max(1, min(4, 1 + released))

    def _op_deck_piece(self, profile, op, _force, extra):
        deck = self._deck_target(profile, op)
        member = _int(op.get("member"), "member", 1, 4)
        slot = _int(op.get("slot"), "slot", 1, 5)
        instance = op.get("pieceInstanceId") or op.get("id")
        piece = None
        for row in profile["state"].get("userPieceList") or []:
            if str(row.get("id")) == str(instance):
                piece = row
                break
        if piece is None:
            raise ValidationError("找不到记忆结晶: %r" % (instance,), code="path_missing")
        if not deck.get("userCardId%d" % member):
            raise ValidationError("这个位置还没有角色", code="deck_member_missing")
        capacity = self.piece_capacity(profile, deck.get("userCardId%d" % member))
        if slot > capacity:
            raise ValidationError("这个角色最多装备 %d 枚结晶（魔力解放 %d 次），槽 %d 不可用"
                                  % (capacity, capacity - 1, slot), code="piece_slot_locked")
        for other in profile["state"].get("userDeckList") or []:
            for (other_member, other_slot), value in self.deck_piece_slots(other).items():
                if str(value) == str(instance) and not (
                        other is deck and other_member == member and other_slot == slot):
                    raise ValidationError("这枚结晶已经编成在别的队伍或位置",
                                          code="piece_already_equipped")
        deck["userPieceId%02d%d" % (member, slot)] = instance
        extra.add("state.userDeckList[deckType=%s]" % deck.get("deckType"))

    def _op_deck_removePiece(self, profile, op, _force, extra):
        deck = self._deck_target(profile, op)
        member = _int(op.get("member"), "member", 1, 4)
        slot = _int(op.get("slot"), "slot", 1, 5)
        key = "userPieceId%02d%d" % (member, slot)
        if key not in deck:
            raise ValidationError("这个槽位没有结晶", code="path_missing")
        deck.pop(key, None)
        extra.add("state.userDeckList[deckType=%s]" % deck.get("deckType"))

    # ---- 任务 / 章节 / 小节

    def _op_quest_set(self, profile, op, _force, extra):
        quest_id = op.get("questBattleId")
        target = None
        for row in profile["state"].get("userQuestBattleList") or []:
            if str(row.get("questBattleId")) == str(quest_id):
                target = row
                break
        if target is None:
            raise ValidationError("找不到任务: %r" % (quest_id,), code="path_missing")
        base = "state.userQuestBattleList[questBattleId=%s]" % quest_id
        if "cleared" in op:
            target["cleared"] = _bool(op["cleared"], "cleared")
            extra.add(base + ".cleared")
        if "clearCount" in op:
            target["clearCount"] = _int(op["clearCount"], "clearCount", 0, 2 ** 53 - 1)
            extra.add(base + ".clearCount")
        if "rewardDone" in op:
            target["rewardDone"] = _bool(op["rewardDone"], "rewardDone")
            extra.add(base + ".rewardDone")
        if "missions" in op:
            for index in range(1, 4):
                if isinstance(op["missions"], dict):
                    raw = op["missions"].get(str(index))
                elif isinstance(op["missions"], (list, tuple)):
                    raw = op["missions"][index - 1]
                else:
                    raw = op["missions"]
                status = _mission_status(raw, "missionStatus%d" % index)
                target["missionStatus%d" % index] = status
                extra.add(base + ".missionStatus%d" % index)

    def _op_quest_bulk(self, profile, op, _force, extra):
        cleared = _bool(op.get("cleared"), "cleared")
        missions = op.get("missions")
        only_uncleared = op.get("onlyUncleared", True)
        section_ids = op.get("sectionIds")
        prefix = [str(sid) for sid in section_ids] if section_ids else None
        touched = 0
        for row in profile["state"].get("userQuestBattleList") or []:
            if only_uncleared and cleared and row.get("cleared") is True:
                continue
            if prefix and not any(str(row.get("questBattleId")).startswith(p) for p in prefix):
                continue
            row["cleared"] = cleared
            row["clearCount"] = max(1, row.get("clearCount") or 0) if cleared else 0
            base = "state.userQuestBattleList[questBattleId=%s]" % row.get("questBattleId")
            extra.add(base + ".cleared")
            extra.add(base + ".clearCount")
            if missions:
                for index in range(1, 4):
                    key = "missionStatus%d" % index
                    current = row.get(key)
                    # 只覆盖明确的达成/未达成；NONE（无此条件）与不认识的历史取值保持原样
                    if current not in (MISSION_CLEARED, MISSION_NOT_CLEAR):
                        continue
                    row[key] = MISSION_CLEARED if cleared else MISSION_NOT_CLEAR
                    extra.add(base + "." + key)
            touched += 1
        if not touched:
            raise ValidationError("没有匹配的任务行", code="path_missing")

    def _op_section_set(self, profile, op, _force, extra):
        """逐个小节调节可玩 / 已通。"""
        section_id = op.get("sectionId")
        target = None
        for row in profile["state"].get("userSectionList") or []:
            if str(row.get("sectionId")) == str(section_id):
                target = row
                break
        if target is None:
            raise ValidationError("找不到小节: %r" % (section_id,), code="path_missing")
        base = "state.userSectionList[sectionId=%s]" % section_id
        if "canPlay" in op:
            target["canPlay"] = _bool(op["canPlay"], "canPlay")
            extra.add(base + ".canPlay")
        if "cleared" in op:
            target["cleared"] = _bool(op["cleared"], "cleared")
            extra.add(base + ".cleared")
        if "canPlay" not in op and "cleared" not in op:
            raise ValidationError("canPlay / cleared 至少要给一个", code="invalid_value")

    def _ensure_chapter(self, profile, chapter_id, cleared):
        rows = profile["state"].setdefault("userChapterList", [])
        for row in rows:
            if str(row.get("chapterId")) == str(chapter_id):
                row["cleared"] = cleared
                if cleared and row.get("canPlay") is not True:
                    row["canPlay"] = True
                return row
        row = {
            "userId": profile["state"]["gameUser"]["userId"],
            "chapterId": int(chapter_id),
            "cleared": cleared,
            "canPlay": True,
            "createdAt": self.stamp(),
        }
        rows.append(row)
        return row

    def _op_chapter_set(self, profile, op, _force, extra):
        chapter_id = op.get("chapterId")
        if chapter_id is None:
            raise ValidationError("需要 chapterId", code="invalid_argument")
        self._ensure_chapter(profile, chapter_id, _bool(op.get("cleared"), "cleared"))
        extra.add("state.userChapterList")

    def _op_challenge_set(self, profile, op, _force, extra):
        table = op.get("table")
        groups = {
            "userDailyChallengeList": "challengeId",
            "userLimitedChallengeList": "challengeId",
            "userTotalChallengeList": "challengeId",
            "userDoppelChallengeList": "challengeId",
        }
        if table not in groups:
            raise ValidationError("未知挑战表: %r" % (table,), code="invalid_value")
        key = groups[table]
        challenge_id = op.get("challengeId")
        target = None
        for row in profile["state"].get(table) or []:
            if str(row.get(key)) == str(challenge_id):
                target = row
                break
        if target is None:
            raise ValidationError("找不到挑战记录: %r" % (challenge_id,), code="path_missing")
        base = "state.%s[%s=%s]" % (table, key, challenge_id)
        if "clearedCount" in op:
            target["clearedCount"] = _int(op["clearedCount"], "clearedCount", 0, 2 ** 53 - 1)
            extra.add(base + ".clearedCount")
        if "count" in op:
            target["count"] = _int(op["count"], "count", 0, 2 ** 53 - 1)
            extra.add(base + ".count")
        if "clearedCount" not in op and "count" not in op:
            raise ValidationError("clearedCount / count 至少要给一个", code="invalid_value")
        if target.get("clearedCount") and "clearedAt" not in target:
            target["clearedAt"] = self.stamp()
            extra.add(base + ".clearedAt")

    def _op_quest_mission(self, profile, op, _force, extra):
        """任务行里的单条 mission 标记切换。"""
        quest_id = op.get("questBattleId")
        target = None
        for row in profile["state"].get("userQuestBattleList") or []:
            if str(row.get("questBattleId")) == str(quest_id):
                target = row
                break
        if target is None:
            raise ValidationError("找不到任务: %r" % (quest_id,), code="path_missing")
        index = _int(op.get("index"), "index", 1, 3)
        status = _mission_status(op.get("status"))
        key = "missionStatus%d" % index
        target[key] = status
        extra.add("state.userQuestBattleList[questBattleId=%s].%s" % (quest_id, key))

    def _op_quest_ensure(self, profile, op, _force, extra):
        """把数据包里已实装、但存档还没收录的任务补成合法行。"""
        catalog = self.quest_catalog().get("quests") or {}
        ids = op.get("questBattleIds") or ([op.get("questBattleId")] if op.get("questBattleId") is not None else [])
        if not ids:
            raise ValidationError("需要 questBattleId 或 questBattleIds", code="invalid_value")
        cleared = op.get("cleared")
        rows = profile["state"].setdefault("userQuestBattleList", [])
        existing = {str(row.get("questBattleId")) for row in rows}
        owner = profile["state"]["gameUser"]["userId"]
        stamp = self.stamp()
        added = 0
        for raw_id in ids:
            qid = str(raw_id)
            if qid in existing:
                continue
            meta = catalog.get(qid) or {}
            missions = self._catalog_missions(meta)
            status = []
            for item in missions:
                if not item["code"]:
                    status.append(MISSION_NONE)
                elif cleared is True:
                    status.append(MISSION_CLEARED)
                elif cleared is False:
                    status.append(MISSION_NOT_CLEAR)
                else:
                    status.append(MISSION_NOT_CLEAR)
            rows.append({
                "userId": owner,
                "questBattleId": int(qid) if qid.isdigit() else qid,
                "cleared": cleared is True,
                "clearCount": 1 if cleared is True else 0,
                "rewardDone": False,
                "maxDamage": 0,
                "missionStatus1": status[0],
                "missionStatus2": status[1],
                "missionStatus3": status[2],
                "createdAt": stamp,
            })
            existing.add(qid)
            added += 1
        if not added:
            raise ValidationError("这些任务已经在存档里了", code="already_present")
        extra.add("state.userQuestBattleList")

    def _op_section_ensure(self, profile, op, _force, extra):
        """把数据包里已实装、但存档还没收录的小节补成合法行。"""
        ids = op.get("sectionIds") or ([op.get("sectionId")] if op.get("sectionId") is not None else [])
        if not ids:
            raise ValidationError("需要 sectionId 或 sectionIds", code="invalid_value")
        can_play = op.get("canPlay")
        cleared = op.get("cleared")
        rows = profile["state"].setdefault("userSectionList", [])
        existing = {str(row.get("sectionId")) for row in rows}
        owner = profile["state"]["gameUser"]["userId"]
        stamp = self.stamp()
        added = 0
        for raw_id in ids:
            sid = str(raw_id)
            if sid in existing:
                continue
            rows.append({
                "userId": owner,
                "sectionId": int(sid) if sid.isdigit() else sid,
                "canPlay": True if can_play is None else _bool(can_play, "canPlay"),
                "cleared": True if cleared is True else False,
                "createdAt": stamp,
            })
            existing.add(sid)
            added += 1
        if not added:
            raise ValidationError("这些小節已经在存档里了", code="already_present")
        extra.add("state.userSectionList")

    def _op_section_bulk(self, profile, op, _force, extra):
        can_play = _bool(op.get("canPlay"), "canPlay")
        cleared = op.get("cleared")
        touched = 0
        for row in profile["state"].get("userSectionList") or []:
            row["canPlay"] = can_play
            extra.add("state.userSectionList[sectionId=%s].canPlay" % row.get("sectionId"))
            if cleared is not None:
                row["cleared"] = _bool(cleared, "cleared")
                extra.add("state.userSectionList[sectionId=%s].cleared" % row.get("sectionId"))
            touched += 1
        if not touched:
            raise ValidationError("没有小节行", code="path_missing")

    def _op_chapter_bulk(self, profile, op, _force, extra):
        cleared = _bool(op.get("cleared"), "cleared")
        touched = 0
        for row in profile["state"].get("userChapterList") or []:
            row["cleared"] = cleared
            extra.add("state.userChapterList[chapterId=%s].cleared" % row.get("chapterId"))
            touched += 1
        if not touched:
            raise ValidationError("没有章节行", code="path_missing")

    def _op_challenge_clear(self, profile, op, _force, extra):
        table = op.get("table")
        groups = {
            "userDailyChallengeList": "challengeId",
            "userLimitedChallengeList": "challengeId",
            "userTotalChallengeList": "challengeId",
            "userDoppelChallengeList": "challengeId",
        }
        if table not in groups:
            raise ValidationError("未知挑战表: %r" % (table,), code="invalid_value")
        key = groups[table]
        by_target = bool(op.get("byTarget"))
        catalog = self.challenge_catalog()
        count = op.get("clearedCount")
        count = 1 if count is None else _int(count, "clearedCount", 0, 2 ** 53 - 1)
        touched = 0
        for row in profile["state"].get(table) or []:
            value = count
            if by_target:
                master = catalog.get(str(row.get(key))) or {}
                target = master.get("count")
                if not isinstance(target, int):
                    continue
                value = target
            row["clearedCount"] = value
            extra.add("state.%s[%s=%s].clearedCount" % (table, key, row.get(key)))
            if count > 0 and "clearedAt" not in row:
                row["clearedAt"] = self.stamp()
                extra.add("state.%s[%s=%s].clearedAt" % (table, key, row.get(key)))
            touched += 1
            extra.add("state.%s[%s=%s].clearedCount" % (table, key, row.get(key)))
        if not touched:
            raise ValidationError("没有挑战行", code="path_missing")

    # ---- 扭蛋

    def _op_gacha_set(self, profile, op, _force, extra):
        field = op.get("field")
        if field not in ("totalCount", "dailyCount", "weeklyCount", "monthlyCount", "count", "paid"):
            raise ValidationError("不允许修改扭蛋字段: %r" % (field,), code="invalid_value")
        value = _int(op.get("value"), field, 0, 2 ** 53 - 1)
        touched = 0
        if op.get("gachaKindId") is not None:
            for row in profile["state"].get("userGachaKindList") or []:
                if str(row.get("gachaKindId")) == str(op["gachaKindId"]):
                    row[field] = value
                    extra.add("state.userGachaKindList[%d].%s" % (profile["state"]["userGachaKindList"].index(row), field))
                    touched += 1
        if op.get("gachaGroupId") is not None:
            for row in profile["state"].get("userGachaGroupList") or []:
                if str(row.get("gachaGroupId")) == str(op["gachaGroupId"]):
                    row[field] = value
                    extra.add("state.userGachaGroupList[%d].%s" % (profile["state"]["userGachaGroupList"].index(row), field))
                    touched += 1
        if op.get("all"):
            for row in profile["state"].get("userGachaKindList") or []:
                row[field] = value
                extra.add("state.userGachaKindList[%d].%s" % (profile["state"]["userGachaKindList"].index(row), field))
                touched += 1
            for row in profile["state"].get("userGachaGroupList") or []:
                if field in row:
                    row[field] = value
                    extra.add("state.userGachaGroupList[%d].%s" % (profile["state"]["userGachaGroupList"].index(row), field))
                    touched += 1
        if not touched:
            raise ValidationError("没有匹配的扭蛋行", code="path_missing")

    # ---- 容量

    def _op_capacity_set(self, profile, op, _force, extra):
        value = _int(op.get("value"), "cardCapacity", 1, 9999)
        profile["state"]["gameUser"]["cardCapacity"] = value
        extra.add("state.gameUser.cardCapacity")

    # ------------------------------------------------------------ 保存

    def save(self, out_path, label=None):
        return self.archive.save(out_path, profile=self.profile, label=label)

    def diff_summary(self):
        before = jsjson.dumps(self.archive.profile)
        after = jsjson.dumps(self.profile)
        return {"changed": before != after, "beforeBytes": jsjson.utf8_len(before),
                "afterBytes": jsjson.utf8_len(after),
                "paths": diff_paths(self.archive.profile, self.profile)[:200]}


_MISSING = object()
