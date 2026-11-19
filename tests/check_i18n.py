"""Checks that every translation key used by the UI and the Python backend exists in every language.

Run:  python tests/check_i18n.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


js = read("ui", "i18n.js")
html = read("ui", "index.html")
py = "".join(read(f) for f in ("sync_engine.py", "PSVitaSaveSync.py", "vita_net.py"))

# keys defined per language: split the I18N object into blocks "xx: { ... },"
langs = {}
for m in re.finditer(r"^  (\w\w): \{\n(.*?)^  \},", js, re.S | re.M):
    langs[m.group(1)] = set(re.findall(r'(?:^\s*|, )"?([\w?]+)"?: "', m.group(2), re.M))

used = set()
used |= set(re.findall(r'data-i18n(?:-html|-ph|-title|-aria)?="(\w+)"', html))
used |= set(re.findall(r'\bt\("(\w+)"', html))
plural = set(re.findall(r'\btn\("(\w+)"', html))
used |= {"st_" + s for s in ("synced", "downloaded", "uploaded", "conflict", "error", "busy", "excluded",
                             "plan_down", "plan_up")}
used |= {"act_down", "act_up", "act_restore", "side_PC", "side_Vita", "side_?"}
used |= {"vn_need_ip", "vn_timeout", "vn_refused", "vn_bad_port", "vn_failed", "vn_ok", "vn_ok_nodir"}
used |= set(re.findall(r'(?:self\.log|engine\.log|self\.status)\("(\w+)"', py))
used |= set(re.findall(r'"key":\s*"(\w+)"', py))
used |= set(re.findall(r'SyncError\("(\w+)"', py))
used |= {"err_connect", "err_generic", "analyzing", "syncing", "st_syncing", "conn_hint"}
used |= set(re.findall(r'connMsg\("(\w+)"', html)) | set(re.findall(r'setStatus\("(\w+)"', html))
used |= set(re.findall(r'"(found_one|found_many|empty_\w+)"', html))
used = {k for k in used if not k.endswith("_")}   # prefixos como t("st_" + estado)

problems = 0
for lang, keys in langs.items():
    for k in sorted(used):
        if k not in keys:
            print(f"[{lang}] missing: {k}")
            problems += 1
    for k in sorted(plural):
        if k + "_other" not in keys:
            print(f"[{lang}] missing plural: {k}_other")
            problems += 1
    extra = keys - used - {k + s for k in plural for s in ("_one", "_other")}
    for k in sorted(extra):
        print(f"[{lang}] unused: {k}")

print(f"\n{len(langs)} languages, {len(used) + len(plural)} keys used, {problems} missing")
sys.exit(1 if problems or len(langs) != 5 else 0)
