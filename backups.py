"""Backups e histórico dos saves.

Estrutura dos backups (cada sincronização que substitui ficheiros cria uma pasta):

    <pasta de backups>/2026-11-17_18-31-02/PC/<save>/...    versão do PC antes de ser substituída
    <pasta de backups>/2026-11-17_18-31-02/Vita/<save>/...  versão da Vita antes de ser substituída

A app antiga usava  <pasta>/17-11-2026_18-31/<save>/...  sem indicar o lado. Esses
aparecem como "antigos" e nunca são apagados automaticamente.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil

NEW_FMT = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
OLD_FMT = re.compile(r"^\d{2}-\d{2}-\d{4}_\d{2}-\d{2}$")
SIDES = ("PC", "Vita")


def new_backup_root(backup_dir: str) -> str:
    """Pasta para uma sessão de backup nova, com um nome que ainda não exista.

    Duas operações no mesmo segundo (ex: resolver dois conflitos seguidos) dariam o mesmo
    nome e uma escreveria por cima dos backups da outra; nesse caso avança-se um segundo.
    """
    t = datetime.datetime.now().replace(microsecond=0)
    while True:
        path = os.path.join(backup_dir, t.strftime("%Y-%m-%d_%H-%M-%S"))
        if not os.path.exists(path):
            return path
        t += datetime.timedelta(seconds=1)


def _session_time(name: str) -> float | None:
    try:
        if NEW_FMT.match(name):
            return datetime.datetime.strptime(name, "%Y-%m-%d_%H-%M-%S").timestamp()
        if OLD_FMT.match(name):
            return datetime.datetime.strptime(name, "%d-%m-%Y_%H-%M").timestamp()
    except ValueError:
        pass
    return None


def _tree_size(folder: str) -> tuple[int, int]:
    size = count = 0
    for dp, _, fs in os.walk(folder):
        for f in fs:
            size += os.path.getsize(os.path.join(dp, f))
            count += 1
    return size, count


def list_backups(backup_dir: str) -> list[dict]:
    """Todas as sessões de backup, da mais recente para a mais antiga."""
    sessions = []
    if not os.path.isdir(backup_dir):
        return sessions
    for name in os.listdir(backup_dir):
        root = os.path.join(backup_dir, name)
        t = _session_time(name)
        if t is None or not os.path.isdir(root):
            continue
        legacy = bool(OLD_FMT.match(name))
        entries = []
        if legacy:
            for save in sorted(os.listdir(root)):
                p = os.path.join(root, save)
                if os.path.isdir(p):
                    size, count = _tree_size(p)
                    entries.append({"save": save, "side": "?", "size": size, "files": count})
        else:
            for side in SIDES:
                sd = os.path.join(root, side)
                if not os.path.isdir(sd):
                    continue
                for save in sorted(os.listdir(sd)):
                    p = os.path.join(sd, save)
                    if os.path.isdir(p):
                        size, count = _tree_size(p)
                        entries.append({"save": save, "side": side, "size": size, "files": count})
        sessions.append({"id": name, "time": t, "legacy": legacy, "entries": entries,
                         "size": sum(e["size"] for e in entries)})
    sessions.sort(key=lambda s: -s["time"])
    return sessions


def _entry_path(backup_dir: str, session: str, side: str, save: str) -> str:
    # só nomes simples, nunca caminhos (evita escrever fora das pastas)
    for part in (session, side, save):
        if not part or os.path.basename(part) != part or part in (".", ".."):
            raise ValueError("invalid backup")
    if _session_time(session) is None:
        raise ValueError("invalid backup")
    root = os.path.join(backup_dir, session)
    path = os.path.join(root, save) if side == "?" else os.path.join(root, side, save)
    if not os.path.isdir(path):
        raise FileNotFoundError("backup no longer exists")
    return path


def restore_backup(backup_dir: str, session: str, side: str, save: str, pc_dir: str) -> str:
    """Repõe no PC a versão guardada num backup.

    A versão atual do PC é antes guardada num backup novo, por isso o restauro também
    se pode desfazer. Depois, na sincronização Automática, o PC conta como "alterado"
    e a versão restaurada é enviada para a Vita.
    """
    src = _entry_path(backup_dir, session, side, save)
    dst = os.path.join(pc_dir, save)
    root = new_backup_root(backup_dir)
    if os.path.isdir(dst):
        shutil.copytree(dst, os.path.join(root, "PC", save))
    os.makedirs(dst, exist_ok=True)
    # Os backups só guardam os ficheiros que foram substituídos; os restantes ficam como estão.
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return os.path.basename(root)


def delete_backup(backup_dir: str, session: str) -> None:
    if _session_time(session) is None or os.path.basename(session) != session:
        raise ValueError("invalid backup")
    shutil.rmtree(os.path.join(backup_dir, session))


def prune_backups(backup_dir: str, keep: int) -> list[str]:
    """Mantém só as `keep` sessões mais recentes criadas por esta app (0 = guardar tudo)."""
    if keep <= 0 or not os.path.isdir(backup_dir):
        return []
    mine = sorted((n for n in os.listdir(backup_dir)
                   if NEW_FMT.match(n) and os.path.isdir(os.path.join(backup_dir, n))), reverse=True)
    removed = []
    for name in mine[keep:]:
        shutil.rmtree(os.path.join(backup_dir, name), ignore_errors=True)
        removed.append(name)
    return removed


# ---------------------------------------------------------------- histórico

class History:
    """Registo do que aconteceu a cada save: {save: [{t, action, files}]} (mais recente primeiro)."""
    MAX_PER_SAVE = 30

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, list] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            pass

    def add(self, save: str, action: str, files: int = 0) -> None:
        entry = {"t": datetime.datetime.now().timestamp(), "action": action, "files": files}
        self.data[save] = ([entry] + self.data.get(save, []))[: self.MAX_PER_SAVE]

    def get(self, save: str) -> list:
        return self.data.get(save, [])

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)
