"""End-to-end tests for the sync engine against a local fake "Vita" FTP server.

Run:  pip install pyftpdlib  &&  python tests/test_sync.py
"""
import glob
import json
import logging
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyftpdlib.authorizers import DummyAuthorizer  # noqa: E402
from pyftpdlib.handlers import FTPHandler  # noqa: E402
from pyftpdlib.servers import FTPServer  # noqa: E402

import backups as B  # noqa: E402
import sync_engine as SE  # noqa: E402
import vita_net  # noqa: E402
from sync_engine import SaveSync, SyncState, games_info  # noqa: E402

logging.disable(logging.CRITICAL)
VITA_DIR = "/pspemu/PSP/SAVEDATA"

root = tempfile.mkdtemp()
vita_root = os.path.join(root, "vita")
pc = os.path.join(root, "pc")
bk = os.path.join(root, "bk")
vsave = os.path.join(vita_root, "pspemu", "PSP", "SAVEDATA")
for d in (vsave, pc):
    os.makedirs(d)
state = os.path.join(root, "state.json")

auth = DummyAuthorizer()
auth.add_anonymous(vita_root, perm="elradfmwMT")
FTPHandler.authorizer = auth
srv = FTPServer(("127.0.0.1", 0), FTPHandler)
port = srv.socket.getsockname()[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def w(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def r(path):
    with open(path, "rb") as f:
        return f.read()


eng = SaveSync(pc, VITA_DIR, bk, state, emit=lambda e: None)


def run(mode="auto", only=None):
    s = eng.run("127.0.0.1", port, mode, only_games=only)
    return {g: v["status"] for g, v in s["games"].items()}


failures = 0


def check(name, got, exp):
    global failures
    good = got == exp
    failures += not good
    print(("PASS " if good else "FAIL ") + name + ("" if good else f"  got={got!r} expected={exp!r}"))


# ---------------------------------------------------------------- basic sync
w(os.path.join(pc, "GAMEA", "DATA.BIN"), b"pc-a-1")
w(os.path.join(pc, "GAMEA", "PARAM.SFO"), b"sfo")
w(os.path.join(vsave, "GAMEB", "DATA.BIN"), b"vita-b-1")
w(os.path.join(pc, "GAMEC", "DATA.BIN"), b"same")
w(os.path.join(vsave, "GAMEC", "DATA.BIN"), b"same")

check("first sync", run(), {"GAMEA": "uploaded", "GAMEB": "downloaded", "GAMEC": "synced"})
check("files copied", (r(os.path.join(vsave, "GAMEA", "DATA.BIN")), r(os.path.join(pc, "GAMEB", "DATA.BIN"))),
      (b"pc-a-1", b"vita-b-1"))
check("no round-trip on second sync", run(), {"GAMEA": "synced", "GAMEB": "synced", "GAMEC": "synced"})

w(os.path.join(pc, "GAMEA", "DATA.BIN"), b"pc-a-2")
check("PC changed -> upload", run(), {"GAMEA": "uploaded", "GAMEB": "synced", "GAMEC": "synced"})

w(os.path.join(vsave, "GAMEB", "DATA.BIN"), b"vita-b-2-longer")
check("Vita changed -> download", run(), {"GAMEA": "synced", "GAMEB": "downloaded", "GAMEC": "synced"})
check("Vita content on PC", r(os.path.join(pc, "GAMEB", "DATA.BIN")), b"vita-b-2-longer")

# Wrong Vita clock: very old FTP date but new content -> still detected (dates are never trusted)
p = os.path.join(vsave, "GAMEC", "DATA.BIN")
w(p, b"vita-c-new")
os.utime(p, (0, 0))
check("Vita change with bogus clock", run(), {"GAMEA": "synced", "GAMEB": "synced", "GAMEC": "downloaded"})

# ---------------------------------------------------------------- conflicts and forced modes
w(os.path.join(pc, "GAMEA", "DATA.BIN"), b"pc-a-3")
w(os.path.join(vsave, "GAMEA", "DATA.BIN"), b"vita-a-3xx")
check("both changed -> conflict", run(), {"GAMEA": "conflict", "GAMEB": "synced", "GAMEC": "synced"})
check("conflict touches nothing",
      (r(os.path.join(pc, "GAMEA", "DATA.BIN")), r(os.path.join(vsave, "GAMEA", "DATA.BIN"))),
      (b"pc-a-3", b"vita-a-3xx"))
check("resolve: keep Vita", run("vita_to_pc", ["GAMEA"]), {"GAMEA": "downloaded"})
check("in sync after resolving", run(), {"GAMEA": "synced", "GAMEB": "synced", "GAMEC": "synced"})

w(os.path.join(pc, "GAMEB", "DATA.BIN"), b"pc-forced")
check("forced Vita -> PC overwrites PC", run("vita_to_pc"),
      {"GAMEA": "synced", "GAMEB": "downloaded", "GAMEC": "synced"})
check("forced content", r(os.path.join(pc, "GAMEB", "DATA.BIN")), b"vita-b-2-longer")
check("backups were made", len([f for _, _, fs in os.walk(bk) for f in fs]) >= 4, True)

# ---------------------------------------------------------------- Vita-only saves (icon cache)
cache = os.path.join(root, "cache")
eng2 = SaveSync(pc, VITA_DIR, bk, state, emit=lambda e: None, vita_cache_dir=cache)
w(os.path.join(vsave, "GAMED", "DATA.BIN"), b"d")
w(os.path.join(vsave, "GAMED", "PARAM.SFO"), b"not-a-real-sfo")
w(os.path.join(vsave, "GAMED", "ICON0.PNG"), b"\x89PNGfake")
eng2.run("127.0.0.1", port, "pc_to_vita")          # pc_to_vita does not bring GAMED to the PC
info = {g["id"]: g for g in games_info(pc, cache)}
check("Vita-only save listed", info.get("GAMED", {}).get("where"), "vita")
check("Vita-only save has icon", bool(info["GAMED"]["icon"]), True)
check("PC save marked as PC", info["GAMEA"]["where"], "pc")
eng2.run("127.0.0.1", port, "auto")                # now it comes to the PC -> leaves the cache
check("cache pruned", os.path.exists(os.path.join(cache, "GAMED")), False)

# ---------------------------------------------------------------- subfolders
w(os.path.join(pc, "GAMEE", "PARAM.SFO"), b"e")
w(os.path.join(pc, "GAMEE", "SLOTS", "deep", "slot1.bin"), b"pc-slot-1")
w(os.path.join(vsave, "GAMEF", "SUB", "x.bin"), b"vita-x")
res = run()
check("subfolders first sync", (res["GAMEE"], res["GAMEF"]), ("uploaded", "downloaded"))
check("nested file uploaded", r(os.path.join(vsave, "GAMEE", "SLOTS", "deep", "slot1.bin")), b"pc-slot-1")
check("nested file downloaded", r(os.path.join(pc, "GAMEF", "SUB", "x.bin")), b"vita-x")
check("subfolders: no round-trip", set(run().values()), {"synced"})
w(os.path.join(vsave, "GAMEE", "SLOTS", "deep", "slot1.bin"), b"vita-slot-1-new")
check("nested Vita change", run()["GAMEE"], "downloaded")
check("nested content", r(os.path.join(pc, "GAMEE", "SLOTS", "deep", "slot1.bin")), b"vita-slot-1-new")
check("nested backup keeps folders", any(dp.endswith(os.path.join("GAMEE", "SLOTS", "deep"))
                                         for dp, _, fs in os.walk(bk) if "slot1.bin" in fs), True)

check("changing folders resets state", len(SyncState(state, "other|folders").files), 0)

# ---------------------------------------------------------------- preview (dry run)
hist_path = os.path.join(root, "history.json")
eng3 = SaveSync(pc, VITA_DIR, bk, state, emit=lambda e: None, history=B.History(hist_path))
eng3.run("127.0.0.1", port, "auto")


def snapshot():
    return {os.path.join(dp, f): r(os.path.join(dp, f))
            for base in (pc, vita_root) for dp, _, fs in os.walk(base) for f in fs}


w(os.path.join(vsave, "GAMEB", "DATA.BIN"), b"vita-b-preview-change")
w(os.path.join(pc, "GAMEA", "DATA.BIN"), b"pc-a-preview-change")
before, state_before = snapshot(), r(state)
bk_before = sorted(glob.glob(os.path.join(bk, "*")))
s = eng3.run("127.0.0.1", port, "auto", dry_run=True)
check("preview plans", (s["games"]["GAMEB"]["status"], s["games"]["GAMEA"]["status"]), ("plan_down", "plan_up"))
check("preview file list", [f["name"] for f in s["games"]["GAMEB"]["files"]], ["DATA.BIN"])
check("preview changes no files", snapshot() == before, True)
check("preview keeps state", r(state) == state_before, True)
check("preview makes no backups", sorted(glob.glob(os.path.join(bk, "*"))) == bk_before, True)
check("preview estimates bytes", s["expected_bytes"] > 0, True)

real = eng3.run("127.0.0.1", port, "auto", expected_bytes=s["expected_bytes"])
check("real sync after preview", (real["games"]["GAMEB"]["status"], real["games"]["GAMEA"]["status"]),
      ("downloaded", "uploaded"))

with open(hist_path) as f:
    h = json.load(f)
check("history: download", h["GAMEB"][0]["action"], "down")
check("history: upload", h["GAMEA"][0]["action"], "up")

# ---------------------------------------------------------------- excluded saves
w(os.path.join(vsave, "GAMEB", "DATA.BIN"), b"vita-b-excluded-change")
ex = eng3.run("127.0.0.1", port, "auto", excluded=["GAMEB"])
check("excluded save skipped", ex["games"]["GAMEB"]["status"], "excluded")
check("excluded save untouched", r(os.path.join(pc, "GAMEB", "DATA.BIN")), b"vita-b-preview-change")

# ---------------------------------------------------------------- backups
w(os.path.join(bk, "01-07-2026_10-00", "GAMEA", "DATA.BIN"), b"legacy-a")   # old-app backup format
sessions = B.list_backups(bk)
check("backups listed", len(sessions) >= 2, True)
check("legacy backup recognised", any(x["legacy"] for x in sessions), True)
session, _entry = next((x, e) for x in sessions if not x["legacy"]
                       for e in x["entries"] if e["save"] == "GAMEB" and e["side"] == "PC")
B.restore_backup(bk, session["id"], "PC", "GAMEB", pc)
check("restore brings old version back", r(os.path.join(pc, "GAMEB", "DATA.BIN")), b"vita-b-2-longer")
check("restore keeps current version as backup",
      any(r(p) == b"vita-b-preview-change" for p in glob.glob(os.path.join(bk, "*", "PC", "GAMEB", "DATA.BIN"))), True)
B.restore_backup(bk, "01-07-2026_10-00", "?", "GAMEA", pc)
check("restore legacy backup", r(os.path.join(pc, "GAMEA", "DATA.BIN")), b"legacy-a")
try:
    B.restore_backup(bk, "..", "PC", "GAMEA", pc)
    check("rejects path traversal", False, True)
except (ValueError, FileNotFoundError):
    check("rejects path traversal", True, True)
n_new = len([x for x in B.list_backups(bk) if not x["legacy"]])
removed = B.prune_backups(bk, 1)
check("prune keeps newest", len([x for x in B.list_backups(bk) if not x["legacy"]]), 1)
check("prune removes the rest", len(removed), n_new - 1)
check("prune never touches legacy backups", os.path.isdir(os.path.join(bk, "01-07-2026_10-00")), True)
B.delete_backup(bk, "01-07-2026_10-00")
check("delete backup on request", os.path.isdir(os.path.join(bk, "01-07-2026_10-00")), False)
r1 = B.new_backup_root(bk)
os.makedirs(r1)
check("backup folders never collide", r1 != B.new_backup_root(bk), True)

# ---------------------------------------------------------------- choosing saves in the preview
eng3.run("127.0.0.1", port, "auto")
w(os.path.join(vsave, "GAMEC", "DATA.BIN"), b"vita-c-pick")          # only Vita changed
w(os.path.join(vsave, "GAMEF", "SUB", "x.bin"), b"vita-f-pick")      # only Vita changed
w(os.path.join(pc, "GAMEE", "PARAM.SFO"), b"pc-e-conflict")           # both changed
w(os.path.join(vsave, "GAMEE", "PARAM.SFO"), b"vita-e-conflict!")
pv = eng3.run("127.0.0.1", port, "auto", dry_run=True)
st = {k: v["status"] for k, v in pv["games"].items()}
check("preview statuses", (st["GAMEC"], st["GAMEF"], st["GAMEE"]), ("plan_down", "plan_down", "conflict"))
check("conflict reports both sizes", pv["games"]["GAMEE"]["size_vita"] > 0 and pv["games"]["GAMEE"]["size_pc"] > 0, True)
# user ticks only GAMEC and picks "Vita" for the GAMEE conflict (GAMEF left unticked)
res = eng3.run("127.0.0.1", port, "auto", only_games=["GAMEC", "GAMEE"], overrides={"GAMEE": "vita_to_pc"})
check("only selected saves ran", sorted(res["games"]), ["GAMEC", "GAMEE"])
check("selected save copied", r(os.path.join(pc, "GAMEC", "DATA.BIN")), b"vita-c-pick")
check("conflict resolved as Vita", r(os.path.join(pc, "GAMEE", "PARAM.SFO")), b"vita-e-conflict!")
check("unselected save untouched", r(os.path.join(pc, "GAMEF", "SUB", "x.bin")), b"vita-x")
again = eng3.run("127.0.0.1", port, "auto", dry_run=True)
check("unselected save pending next time", again["games"]["GAMEF"]["status"], "plan_down")
check("resolved conflict now in sync", again["games"]["GAMEE"]["status"], "synced")
bad = eng3.run("127.0.0.1", port, "auto", only_games=["GAMEA"], overrides={"GAMEA": "rm -rf"})
check("invalid override ignored", bad["games"]["GAMEA"]["status"] in ("synced", "uploaded", "downloaded", "conflict"), True)

# ---------------------------------------------------------------- unchanged PC files are not re-hashed
calls = []
orig_md5 = SE.md5_file
SE.md5_file = lambda p: (calls.append(p), orig_md5(p))[1]
eng3.run("127.0.0.1", port, "auto")
calls.clear()
conflicted = set(eng3.run("127.0.0.1", port, "auto")["conflicts"])
check("unchanged PC files not re-hashed",
      [c for c in calls if os.path.basename(os.path.dirname(c)) not in conflicted], [])
w(os.path.join(pc, "GAMEC", "DATA.BIN"), b"pc-c-changed")
calls.clear()
res = eng3.run("127.0.0.1", port, "auto")
check("changed PC file re-hashed", any("GAMEC" in c for c in calls), True)
check("changed PC file uploaded", res["games"]["GAMEC"]["status"], "uploaded")
check("summary counts saves", res["uploaded"], 1)
SE.md5_file = orig_md5

# ---------------------------------------------------------------- malicious FTP names are ignored
for evil in ("..", ".", "..\\..\\Startup\\evil.exe", "../../evil", "C:evil", "a/b", "evil.", "nul\x00x"):
    line = f"-rw-rw-rw- 1 vita vita 10 Nov 17 14:03 {evil}"
    check(f"unsafe FTP name rejected: {evil!r}", SE.parse_list_line(line), None)
check("normal FTP name accepted", SE.parse_list_line("drw-rw-rw- 1 vita vita 0 Nov 17 14:03 ULUS10311GHOST04").name,
      "ULUS10311GHOST04")
check("save_details ignores unsafe ids", SE.save_details(pc, None, ["..", "..\\x", "GAMEA"]).keys() == {"GAMEA"}, True)

# ---------------------------------------------------------------- connection test
check("connection test ok", vita_net.test_connection("127.0.0.1", port, VITA_DIR)["ok"], True)
check("connection refused reported", vita_net.test_connection("127.0.0.1", 1, "/x")["ok"], False)
check("probe sees the FTP server", vita_net._probe("127.0.0.1", port) is not None, True)

srv.close_all()
print(f"\n{'ALL TESTS PASSED' if not failures else f'{failures} TEST(S) FAILED'}")
sys.exit(1 if failures else 0)
