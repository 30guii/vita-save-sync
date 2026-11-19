"""PS Vita Save Sync — janela (pywebview) que liga a interface em ui/ ao sync_engine."""

import ctypes
import json
import logging
import os
import shutil
import socket
import sys
import threading

import webview

import backups
import vita_net
from sync_engine import (SaveSync, SyncError, SyncState, detect_ppsspp_savedata, games_info,
                         save_details, state_scope)

LANGUAGES = ("pt", "en", "es", "fr", "zh")   # "" = idioma do sistema
APPEARANCES = ("system", "light", "dark")
# cor da barra de título = cor da barra de ferramentas da interface (ui/index.html: --window)
TITLE_BAR = {"light": ((0xF2, 0xF2, 0xF4), (0x1D, 0x1D, 0x1F)), "dark": ((0x1C, 0x1C, 0x1E), (0xF5, 0xF5, 0xF7))}

if getattr(sys, "frozen", False):
    # .exe (PyInstaller): a interface vem embutida; os dados ficam ao lado do .exe
    # (e não na pasta temporária onde o PyInstaller descompacta, que é apagada ao fechar).
    UI_DIR = os.path.join(sys._MEIPASS, "ui")
    DATA_DIR = os.path.dirname(sys.executable)
else:
    UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
STATE_FILE = os.path.join(DATA_DIR, "sync_state.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
LOG_FILE = os.path.join(DATA_DIR, "app.log")
VITA_CACHE_DIR = os.path.join(DATA_DIR, "cache", "vita")

DEFAULT_VITA_DIR = "/ux0:/pspemu/PSP/SAVEDATA"
BG_COLOR = "#f2f2f4"

# Ao abrir com duplo clique (pythonw) não há consola: os erros ficam em app.log.
logging.basicConfig(filename=LOG_FILE, level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
log = logging.getLogger("vitasync")


def default_backup_dir(pc_dir: str) -> str:
    # ...\memstick\PSP\SAVEDATA -> ...\Saves Backups (ao lado da pasta memstick)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(pc_dir))), "Saves Backups")


_detect_tried = False


def load_config() -> dict:
    cfg = {"ip": "", "port": "1337", "mode": "auto", "pc_dir": "", "backup_dir": "",
           "vita_dir": DEFAULT_VITA_DIR, "excluded": [], "keep_backups": 10,
           "confirm": "checked",   # checked | unchecked | direct (ver a opção "Antes de copiar")
           "language": "",         # "" = idioma do sistema
           "appearance": "system"} # system | light | dark (modo noturno)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    global _detect_tried
    if not cfg["pc_dir"] and not _detect_tried:  # primeira vez: procura o PPSSPP sozinho
        _detect_tried = True
        found = detect_ppsspp_savedata()
        if found:
            cfg["pc_dir"] = found[0]["path"]
            save_config(cfg)
    if cfg["pc_dir"] and not cfg["backup_dir"]:
        cfg["backup_dir"] = default_backup_dir(cfg["pc_dir"])
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)


def _backup_error(e: Exception) -> dict:
    """Erros de backups -> chave de tradução para a interface."""
    if isinstance(e, FileNotFoundError):
        return {"ok": False, "key": "err_backup_missing"}
    if isinstance(e, ValueError):
        return {"ok": False, "key": "err_backup_invalid"}
    log.exception("Erro nos backups")
    return {"ok": False, "key": "err_generic", "args": {"detail": str(e)}}


class Api:
    """Métodos chamados a partir do JavaScript (window.pywebview.api.*)."""

    def __init__(self):
        self._window = None
        self._busy = False

    def _emit(self, event: dict) -> None:
        if self._window:
            self._window.evaluate_js(f"window.onSyncEvent({json.dumps(event)})")

    def get_initial(self):
        cfg = load_config()
        state = SyncState(STATE_FILE, state_scope(cfg["pc_dir"], cfg["vita_dir"])) if cfg["pc_dir"] else None
        return {
            "config": cfg,
            "games": games_info(cfg["pc_dir"], VITA_CACHE_DIR) if cfg["pc_dir"] else [],
            "last_sync": state.last_sync if state else None,
            "pc_dir_ok": bool(cfg["pc_dir"]) and os.path.isdir(cfg["pc_dir"]),
        }

    # -- definições
    def detect_ppsspp(self):
        return detect_ppsspp_savedata()

    def browse_folder(self, start=""):
        result = self._window.create_file_dialog(
            webview.FileDialog.FOLDER, directory=start if start and os.path.isdir(start) else "")
        return result[0] if result else None

    def save_settings(self, pc_dir, backup_dir, vita_dir, keep_backups=10, confirm="checked"):
        pc_dir, backup_dir, vita_dir = pc_dir.strip(), backup_dir.strip(), vita_dir.strip()
        if not os.path.isdir(pc_dir):
            return {"ok": False, "field": "pc_dir", "key": "set_err_pc_dir"}
        if not vita_dir.startswith("/"):
            return {"ok": False, "field": "vita_dir", "key": "set_err_vita_dir"}
        try:
            keep_backups = max(0, int(keep_backups))
        except (TypeError, ValueError):
            return {"ok": False, "field": "keep_backups", "key": "set_err_keep"}
        cfg = load_config()
        changed = (os.path.normcase(cfg["pc_dir"]) != os.path.normcase(pc_dir)) or cfg["vita_dir"] != vita_dir
        cfg.update(pc_dir=pc_dir, backup_dir=backup_dir or default_backup_dir(pc_dir),
                   vita_dir=vita_dir.rstrip("/") or "/", keep_backups=keep_backups,
                   confirm=confirm if confirm in ("checked", "unchecked", "direct") else "checked")
        save_config(cfg)
        if changed:  # a cache de saves "só na Vita" pertence às pastas antigas
            shutil.rmtree(VITA_CACHE_DIR, ignore_errors=True)
        return {"ok": True, **self.get_initial()}

    def set_language(self, lang):
        cfg = load_config()
        cfg["language"] = lang if lang in LANGUAGES else ""
        save_config(cfg)
        return cfg["language"]

    def set_appearance(self, mode):
        cfg = load_config()
        cfg["appearance"] = mode if mode in APPEARANCES else "system"
        save_config(cfg)
        paint_title_bar(self._window, cfg["appearance"])
        return cfg["appearance"]

    def set_excluded(self, save_id, excluded):
        cfg = load_config()
        s = set(cfg.get("excluded", []))
        (s.add if excluded else s.discard)(save_id)
        cfg["excluded"] = sorted(s)
        save_config(cfg)
        return cfg["excluded"]

    # -- ligação
    def test_connection(self, ip, port):
        return vita_net.test_connection(ip, port or 1337, load_config()["vita_dir"])

    def find_vita(self, port):
        return vita_net.find_vitas(port or 1337)

    # -- sincronização
    def start_sync(self, ip, port, mode, only_games=None, dry_run=False, expected_bytes=0,
                   overrides=None, remember_mode=True):
        if self._busy:
            return False
        cfg = load_config()
        cfg["ip"], cfg["port"] = str(ip).strip(), str(port).strip()
        if remember_mode:
            cfg["mode"] = mode
        save_config(cfg)
        self._busy = True
        threading.Thread(target=self._run,
                         args=(cfg, mode, only_games, bool(dry_run), int(expected_bytes or 0), overrides or {}),
                         daemon=True).start()
        return True

    def _run(self, cfg, mode, only_games, dry_run, expected_bytes, overrides):
        history = backups.History(HISTORY_FILE)
        engine = SaveSync(cfg["pc_dir"], cfg["vita_dir"], cfg["backup_dir"], STATE_FILE,
                          emit=self._emit, vita_cache_dir=VITA_CACHE_DIR, history=history)
        try:
            summary = engine.run(cfg["ip"], cfg["port"], mode, only_games=only_games, dry_run=dry_run,
                                 excluded=cfg.get("excluded", []), expected_bytes=expected_bytes,
                                 overrides=overrides)
            if not dry_run:
                removed = backups.prune_backups(cfg["backup_dir"], int(cfg.get("keep_backups", 10)))
                if removed:
                    engine.log("log_pruned", n=len(removed), keep=cfg["keep_backups"])
            summary["all_games"] = games_info(cfg["pc_dir"], VITA_CACHE_DIR)
            self._emit({"type": "done", "summary": summary})
        except SyncError as e:
            self._emit({"type": "error", "key": e.key, "args": e.params})
        except Exception as e:  # mostra qualquer falha na interface em vez de morrer em silêncio
            log.exception("Erro na sincronização")
            detail = str(e) or e.__class__.__name__
            key = "err_connect" if isinstance(e, (ConnectionError, TimeoutError, socket.gaierror)) else "err_generic"
            self._emit({"type": "error", "key": key, "args": {"detail": detail}})
        finally:
            self._busy = False

    # -- detalhes, histórico e backups
    def get_save_details(self, save_ids):
        cfg = load_config()
        details = save_details(cfg["pc_dir"], VITA_CACHE_DIR, list(save_ids))
        history = backups.History(HISTORY_FILE)
        all_backups = backups.list_backups(cfg["backup_dir"])
        for sid, d in details.items():
            d["history"] = history.get(sid)
            d["backups"] = [{"session": s["id"], "time": s["time"], "legacy": s["legacy"], **e}
                            for s in all_backups for e in s["entries"] if e["save"] == sid]
        return details

    def list_backups(self):
        cfg = load_config()
        return {"sessions": backups.list_backups(cfg["backup_dir"]), "keep": cfg.get("keep_backups", 10)}

    def restore_backup(self, session, side, save_id):
        if self._busy:
            return {"ok": False, "key": "err_busy"}
        cfg = load_config()
        try:
            backups.restore_backup(cfg["backup_dir"], session, side, save_id, cfg["pc_dir"])
        except Exception as e:
            return _backup_error(e)
        history = backups.History(HISTORY_FILE)
        history.add(save_id, "restore")
        history.save()
        return {"ok": True, "games": games_info(cfg["pc_dir"], VITA_CACHE_DIR)}

    def delete_backup(self, session):
        try:
            backups.delete_backup(load_config()["backup_dir"], session)
            return {"ok": True}
        except Exception as e:
            return _backup_error(e)

    # -- pastas
    def open_save_folder(self, save_id):
        path = os.path.join(load_config()["pc_dir"], os.path.basename(save_id))
        if os.path.isdir(path):
            os.startfile(path)

    def open_folder(self, which):
        cfg = load_config()
        path = {"pc": cfg["pc_dir"], "backup": cfg["backup_dir"]}.get(which)
        if path:
            if which == "backup":
                os.makedirs(path, exist_ok=True)
            if os.path.isdir(path):
                os.startfile(path)


def windows_uses_dark() -> bool:
    """O Windows está em modo escuro para as apps? (Definições > Personalização > Cores)"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return value == 0
    except OSError:
        return False


def paint_title_bar(window, appearance: str = "system"):
    """Barra de título nativa (para poder redimensionar/encaixar), pintada com a cor da app
    no tema claro ou no escuro."""
    try:
        dark = appearance == "dark" or (appearance == "system" and windows_uses_dark())
        (bg, fg) = TITLE_BAR["dark" if dark else "light"]
        hwnd = window.native.Handle.ToInt32()
        dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute

        def set_attr(attr, value):
            v = ctypes.c_int(value)
            dwm(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        rgb = lambda c: (c[2] << 16) | (c[1] << 8) | c[0]   # COLORREF = 0x00BBGGRR
        set_attr(20, 1 if dark else 0)   # botões da janela claros/escuros
        set_attr(35, rgb(bg))            # cor da barra (Windows 11)
        set_attr(36, rgb(fg))            # cor do texto do título
    except Exception:
        pass  # Windows 10 ignora a cor; não é motivo para falhar


def main():
    api = Api()
    window = webview.create_window(
        "PS Vita Save Sync",
        os.path.join(UI_DIR, "index.html"),
        js_api=api,
        width=1100,
        height=720,
        min_size=(760, 540),
        resizable=True,
        background_color=BG_COLOR,
    )
    api._window = window
    window.events.shown += lambda: paint_title_bar(window, load_config().get("appearance", "system"))
    webview.start()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("A app fechou com um erro")
        raise
