"""本地 Web 服务：把存档编辑器暴露成浏览器界面。

用法：
    python -m magiasave.server --archive "H:/magia_server/magia-save-chino-1794718180238.zip" --port 8765

只监听 127.0.0.1；不访问网络。所有写操作都通过 /api/save 显式落盘，且不会覆盖原存档。
"""

import argparse
import copy
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import archive as archive_mod
from . import jsjson
from . import paths
from .catalog import Catalog
from .errors import ArchiveError, ValidationError
from .masters import Masters
from .model import Editor

ROOT_DIR = paths.bundle_root()
WEB_DIR = paths.web_dir()
ASSETS_DIR = paths.assets_dir()
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Session:
    def __init__(self, archive_path=None):
        self.lock = threading.RLock()
        self.archive = None
        self.editor = None
        self.catalog = Catalog()
        self.masters = Masters()
        self.undo_stack = []
        self.undo_limit = 25
        if archive_path:
            self.open(archive_path)

    def open(self, path):
        arc = archive_mod.SlotArchive.load(path)
        self.archive = arc
        self.editor = Editor(arc, self.catalog, self.masters)
        self.undo_stack = []
        return arc

    def require(self):
        if self.editor is None:
            raise ValidationError("尚未打开存档", code="no_archive")
        return self.editor

    def snapshot(self):
        self.undo_stack.append(copy.deepcopy(self.editor.profile))
        if len(self.undo_stack) > self.undo_limit:
            self.undo_stack.pop(0)

    def undo(self):
        if not self.undo_stack:
            raise ValidationError("没有可撤销的操作", code="undo_empty")
        editor = self.editor
        editor.profile = self.undo_stack.pop()
        editor.dirty = jsjson.dumps(editor.profile) != editor.original
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "MagiaSaveEditor/1.0"
    session = None  # 由 main 注入

    # ------------------------------------------------------------ 基础

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload, ensure_ascii=False))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValidationError("请求体不是合法 JSON: %s" % exc, code="invalid_json")

    # ------------------------------------------------------------ 路由

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            if path == "/" or path == "/index.html":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path.startswith("/assets/"):
                return self._asset(path[len("/assets/"):])
            if path == "/api/status":
                return self._json(self.api_status())
            if path == "/api/overview":
                return self._json(self.api_overview(include_diff=query.get("diff") != "0"))
            if path.startswith("/api/view/"):
                return self._json(self.api_view(path[len("/api/view/"):], query))
            if path == "/api/chara":
                return self._json(self.api_view("chara", query))
            if path == "/api/catalog/items":
                return self._json(self.api_catalog_items(query))
            if path == "/api/raw":
                return self._json(self.api_raw(query))
            if path == "/api/health":
                return self._json({"ok": True})
            return self._json({"ok": False, "error": "未知接口: %s" % path}, 404)
        except ArchiveError as exc:
            self._json({"ok": False, "error": str(exc), "code": exc.code}, 400)
        except Exception as exc:
            traceback.print_exc()
            self._json({"ok": False, "error": str(exc), "code": "internal"}, 500)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            body = self._read_json()
            if path == "/api/open":
                return self._json(self.api_open(body))
            if path == "/api/edit":
                return self._json(self.api_edit(body))
            if path == "/api/undo":
                return self._json(self.api_undo())
            if path == "/api/discard":
                return self._json(self.api_discard())
            if path == "/api/save":
                return self._json(self.api_save(body))
            if path == "/api/raw":
                return self._json(self.api_raw_post(body))
            if path == "/api/pick":
                return self._json(self.api_pick(body))
            return self._json({"ok": False, "error": "未知接口: %s" % path}, 404)
        except ArchiveError as exc:
            self._json({"ok": False, "error": str(exc), "code": exc.code}, 400)
        except Exception as exc:
            traceback.print_exc()
            self._json({"ok": False, "error": str(exc), "code": "internal"}, 500)

    # ------------------------------------------------------------ 静态文件

    def _static(self, rel):
        safe = os.path.normpath(rel).lstrip("\\")
        target = os.path.join(WEB_DIR, safe)
        if not os.path.abspath(target).startswith(os.path.abspath(WEB_DIR)):
            return self._json({"ok": False, "error": "非法路径"}, 400)
        if not os.path.isfile(target):
            return self._json({"ok": False, "error": "文件不存在: %s" % rel}, 404)
        with open(target, "rb") as handle:
            blob = handle.read()
        ext = os.path.splitext(target)[1].lower()
        self._send(200, blob, MIME.get(ext, "application/octet-stream"))

    def _asset(self, rel):
        safe = os.path.normpath(rel).lstrip("\\")
        target = os.path.join(ASSETS_DIR, safe)
        if not os.path.abspath(target).startswith(os.path.abspath(ASSETS_DIR)):
            return self._json({"ok": False, "error": "非法路径"}, 400)
        if not os.path.isfile(target):
            return self._json({"ok": False, "error": "素材不存在: %s" % rel}, 404)
        with open(target, "rb") as handle:
            blob = handle.read()
        ext = os.path.splitext(target)[1].lower()
        self._send(200, blob, MIME.get(ext, "image/png"))

    # ------------------------------------------------------------ API

    def api_status(self):
        session = self.session
        with session.lock:
            if session.editor is None:
                return {"ok": True, "loaded": False, "catalog": session.catalog.stats()}
            arc = session.archive
            return {
                "ok": True,
                "loaded": True,
                "archive": arc.path,
                "summary": arc.summary(),
                "dirty": session.editor.dirty,
                "undoDepth": len(session.undo_stack),
                "catalog": session.catalog.stats(),
            }

    def api_overview(self, include_diff=True):
        with self.session.lock:
            editor = self.session.require()
            data = editor.overview()
            data["ok"] = True
            data["dirty"] = editor.dirty
            if include_diff:
                data["diff"] = editor.diff_summary()
            return data

    def api_view(self, name, query):
        with self.session.lock:
            editor = self.session.require()
            text = query.get("q") or None
            offset = int(query.get("offset") or 0)
            limit = min(int(query.get("limit") or 100), 500)
            if name == "items":
                return {"ok": True, **editor.items_view(text, offset, limit,
                                                        query.get("sort") or "id",
                                                        query.get("group"), query.get("family"),
                                                        query.get("scope") or "player")}
            if name == "chara":
                return {"ok": True, **editor.chara_detail(query.get("userCardId"), query.get("charaId"))}
            if name == "chara-catalog":
                return {"ok": True, **editor.chara_catalog(text, offset, limit)}
            if name == "items-families":
                return {"ok": True, **editor.item_families(query.get("scope") or "player")}
            if name == "items-groups":
                return {"ok": True, **editor.item_groups(query.get("family"),
                                                         query.get("scope") or "player")}
            if name == "charas":
                return {"ok": True, **editor.charas_view(text, offset, limit)}
            if name == "pieces":
                return {"ok": True, **editor.pieces_view(text, offset, limit,
                                                         query.get("rank"), query.get("type"),
                                                         query.get("limitMode"), query.get("charaId"))}
            if name == "piece":
                return {"ok": True, **editor.piece_detail(query.get("id"))}
            if name == "materials":
                return {"ok": True, **editor.materials_view()}
            if name == "piece-master":
                return {"ok": True, **editor.piece_master_view(query.get("pieceId"))}
            if name == "decks":
                return {"ok": True, **editor.decks_view()}
            if name == "formations":
                return {"ok": True, **editor.formations_view()}
            if name == "quests":
                return {"ok": True, **editor.quests_view(text, offset, limit, query.get("only"))}
            if name == "sections":
                return {"ok": True, **editor.sections_view(text, offset, limit)}
            if name == "chapters":
                return {"ok": True, **editor.chapters_view()}
            if name == "chapter-map":
                return {"ok": True, **editor.chapter_map(query.get("chapterId"))}
            if name == "section":
                return {"ok": True, **editor.section_detail(query.get("sectionId"))}
            if name == "challenges":
                return {"ok": True, **editor.challenges_view()}
            if name == "gifts":
                return {"ok": True, **editor.gifts_view()}
            if name == "quests-tree":
                return {"ok": True, **editor.quest_tree(text, query.get("only"),
                                                        query.get("catalog") == "1")}
            if name == "sections-tree":
                return {"ok": True, **editor.section_tree(text, query.get("catalog") == "1")}
            if name == "pieces-catalog":
                return {"ok": True, **editor.piece_catalog(text, offset, limit,
                                                           query.get("rank"), query.get("type"),
                                                           query.get("limitMode"), query.get("charaId"))}
            if name == "gifts-catalog":
                return {"ok": True, **editor.gift_catalog(text, limit)}
            if name == "status":
                return {"ok": True, **editor.status_view()}
            if name == "attribute":
                return {"ok": True, **editor.attribute_view(query.get("charaId"))}
            if name == "awakening":
                return {"ok": True, **editor.awakening_view(query.get("userCardId"),
                                                           query.get("charaId"))}
            return {"ok": False, "error": "未知视图: %s" % name}

    def api_catalog_items(self, query):
        catalog = self.session.catalog
        needle = (query.get("q") or "").lower()
        limit = min(int(query.get("limit") or 50), 300)
        rows = []
        for code, row in catalog.data["items"].items():
            name = row.get("name") or code
            if needle and needle not in code.lower() and needle not in name.lower():
                continue
            rows.append({"itemId": code, "name": name, "type": row.get("type"), "unit": row.get("unit")})

        def rank(item):
            code = item["itemId"].lower()
            name = (item["name"] or "").lower()
            if not needle:
                return (3, code)
            if code == needle:
                return (0, code)
            if name == needle:
                return (1, code)
            if code.startswith(needle):
                return (2, code)
            return (3, code)

        rows.sort(key=rank)
        total = len(rows)
        return {"ok": True, "rows": rows[:limit], "total": total}

    def api_raw(self, query):
        with self.session.lock:
            editor = self.session.require()
            return {"ok": True, **editor.raw(query.get("path") or None)}

    def api_raw_post(self, body):
        return self.api_edit({"ops": [{"op": "raw.set", "path": body.get("path"), "value": body.get("value")}],
                              "force": body.get("force")})

    def api_open(self, body):
        path = body.get("path")
        if not path or not os.path.isfile(path):
            raise ValidationError("存档不存在: %s" % path, code="save_archive_missing")
        with self.session.lock:
            self.session.open(path)
            return {"ok": True, "summary": self.session.archive.summary()}

    def api_edit(self, body):
        ops = body.get("ops")
        force = bool(body.get("force"))
        with self.session.lock:
            editor = self.session.require()
            self.session.snapshot()
            try:
                result = editor.apply(ops, force=force)
            except Exception:
                self.session.undo_stack.pop()
                raise
            result["ok"] = True
            result["dirty"] = editor.dirty
            return result

    def api_undo(self):
        with self.session.lock:
            editor = self.session.require()
            self.session.undo()
            return {"ok": True, "dirty": editor.dirty, "undoDepth": len(self.session.undo_stack)}

    def api_discard(self):
        with self.session.lock:
            editor = self.session.require()
            editor.reset()
            self.session.undo_stack = []
            return {"ok": True, "dirty": False}

    def api_pick(self, body):
        # 原生文件对话框：mode=open/save。取消或没有 tkinter 时 path 为 null。
        from . import dialogs
        mode = body.get("mode") or "open"
        path = dialogs.pick(mode, body.get("initial"), body.get("suggest"))
        return {"ok": True, "path": path, "available": dialogs.available()}

    def api_save(self, body):
        out_path = body.get("path")
        label = body.get("label")
        if not out_path:
            raise ValidationError("必须指定输出路径", code="invalid_path")
        with self.session.lock:
            editor = self.session.require()
            info = editor.archive.save(out_path, profile=editor.profile, label=label)
            self.session.open(out_path)
            return {"ok": True, "saved": info}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Magia☆Record 离线存档编辑器（本地 Web）")
    parser.add_argument("--archive", help="启动时打开的存档 zip")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    session = Session(args.archive)
    Handler.session = session
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("存档编辑器已启动: %s" % url)
    if args.archive:
        print("已打开: %s" % args.archive)
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
