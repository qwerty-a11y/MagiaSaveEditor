"""Run against an isolated copy of a real save: MAGIA_SAMPLE=... python tests/test_overview_modes.py."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from magiasave.server import Handler, Session


class OverviewModes(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("MAGIA_SAMPLE"), "set MAGIA_SAMPLE to an existing save ZIP")
    def test_read_edit_undo_and_save_as(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.zip"
            shutil.copyfile(os.environ["MAGIA_SAMPLE"], source)
            digest = hashlib.sha256(source.read_bytes()).digest()

            class Local(Handler):
                session = Session(str(source))

                def log_message(self, *_args):
                    pass

            with ThreadingHTTPServer(("127.0.0.1", 0), Local) as server:
                t = threading.Thread(target=server.serve_forever, daemon=True)
                t.start()
                try:
                    def call(path, payload=None):
                        data = None if payload is None else json.dumps(payload).encode("utf-8")
                        req = Request("http://127.0.0.1:%s%s" % (server.server_port, path), data=data,
                                      headers={"Content-Type": "application/json"})
                        with urlopen(req, timeout=60) as response:
                            return json.load(response)

                    def compare(dirty):
                        full = call("/api/overview")
                        self.assertEqual(call("/api/overview?diff=1"), full)
                        self.assertEqual(call("/api/overview?diff=other"), full)
                        diff = full.pop("diff")
                        self.assertEqual(diff["changed"], dirty)
                        self.assertEqual(full["dirty"], dirty)
                        # Validate omission and no hidden diff computation.
                        with patch.object(Local.session.editor, "diff_summary",
                                          side_effect=AssertionError("unexpected comparison")):
                            self.assertEqual(call("/api/overview?diff=0"), full)
                        return full["currency"]["riche"], diff

                    old, clean = compare(False)
                    value = 0 if old else 1
                    edit = {"ops": [{"op": "money.set", "kind": "riche", "value": value}]}
                    call("/api/edit", edit)
                    gold, changed = compare(True)
                    self.assertEqual(gold, value)
                    self.assertEqual(changed["beforeBytes"], clean["beforeBytes"])
                    self.assertIn("state.gameUser.riche", changed["paths"])
                    call("/api/undo", {})
                    self.assertEqual(compare(False), (old, clean))
                    call("/api/edit", edit)
                    out = Path(tmp) / "edited.zip"
                    call("/api/save", {"path": str(out), "label": "overview-test"})
                    self.assertTrue(out.exists())
                    self.assertEqual(compare(False)[0], value)
                    call("/api/open", {"path": str(source)})
                    self.assertEqual(compare(False), (old, clean))
                    status = call("/api/status")
                    self.assertFalse(status["dirty"])
                    self.assertEqual(status["undoDepth"], 0)
                    self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), digest)
                finally:
                    server.shutdown()
                    t.join()


if __name__ == "__main__":
    unittest.main()
