"""Motor de sincronização de saves PSP entre a PS Vita (FTP do VitaShell) e o PPSSPP.

Em vez de comparar datas (o FTP da Vita só dá a hora ao minuto e o relógio dela
pode estar noutro fuso horário), o motor guarda um "estado" da última
sincronização em sync_state.json:

    ficheiro -> hash do conteúdo + impressão digital do FTP (tamanho e data como texto)

Na sincronização seguinte, cada lado é comparado com esse estado:
  * só mudou o PC   -> envia para a Vita
  * só mudou a Vita -> copia para o PC
  * mudaram os dois -> conflito (não se mexe em nada; o utilizador escolhe)

Como depois de cada cópia o estado fica com o hash e a nova impressão digital,
um save acabado de enviar já não volta a ser descarregado.
"""

from __future__ import annotations

import base64
import datetime
import ftplib
import hashlib
import io
import json
import os
import re
import shutil
import struct
import time
from dataclasses import dataclass
from typing import Callable

from backups import History, new_backup_root

MODES = ("auto", "vita_to_pc", "pc_to_vita")


class SyncError(Exception):
    """Erro para mostrar ao utilizador. A interface traduz `key` (ver ui/i18n.js) com `params`."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params


# ---------------------------------------------------------------- utilitários

@dataclass
class RemoteEntry:
    name: str
    is_dir: bool
    fingerprint: str  # tamanho + data exatamente como o servidor FTP os mostra


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def write_atomic(path: str, data: bytes) -> None:
    tmp = path + ".synctmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def local_fingerprint(path: str) -> str:
    """Tamanho + data de modificação (ns). Se não mudou, o hash guardado continua válido."""
    st = os.stat(path)
    return f"{st.st_size}:{st.st_mtime_ns}"


def remote_size(entry: RemoteEntry) -> int:
    try:
        return int(entry.fingerprint.split(" ", 1)[0])
    except ValueError:
        return 0


_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_FILE_BYTES = 512 * 1024 * 1024   # nenhum ficheiro de save PSP chega perto disto


def safe_name(name: str) -> bool:
    """Um nome vindo do FTP só é usado se for um nome simples de ficheiro/pasta.

    Sem isto, um servidor FTP malicioso na rede podia responder com nomes como
    "..\\..\\Startup\\x" e a app escreveria ficheiros fora da pasta de saves.
    """
    return (bool(name) and name not in (".", "..") and not _UNSAFE_CHARS.search(name)
            and name == name.rstrip(" ."))   # o Windows ignora pontos/espaços no fim


def parse_list_line(line: str) -> RemoteEntry | None:
    # ex: "-rw-rw-rw- 1 vita vita 123456 Nov 17 14:03 DATA.BIN"
    parts = line.split(maxsplit=8)
    if len(parts) < 9:
        return None
    name = parts[8].strip()
    if not safe_name(name):
        return None
    return RemoteEntry(name, parts[0].startswith("d"), " ".join(parts[4:8]))


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def ftp_date_to_ts(fingerprint: str) -> float | None:
    """Data aproximada (só para mostrar ao utilizador, nunca para decidir cópias)."""
    try:
        _size, mon, day, t = fingerprint.split(" ")
        now = datetime.datetime.now()
        if ":" in t:
            h, mi = map(int, t.split(":"))
            dt = datetime.datetime(now.year, _MONTHS[mon[:3]], int(day), h, mi)
            if dt > now + datetime.timedelta(days=1):
                dt = dt.replace(year=now.year - 1)
        else:
            dt = datetime.datetime(int(t), _MONTHS[mon[:3]], int(day))
        return dt.timestamp()
    except (ValueError, KeyError):
        return None


# ---------------------------------------------------------------- estado

class SyncState:
    def __init__(self, path: str, scope: str = ""):
        """scope identifica o par de pastas (PC + Vita). Se mudares de pastas, o estado
        antigo não serve e começa do zero, em vez de comparar com saves de outra pasta."""
        self.path = path
        self.scope = scope
        self.files: dict[str, dict] = {}
        self.last_sync: str | None = None
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not scope or data.get("scope", scope) == scope:
                    self.files = data.get("files", {})
                    self.last_sync = data.get("last_sync")
            except (OSError, ValueError):
                pass  # estado corrompido -> começa do zero (tudo vira "desconhecido")

    def get(self, key: str) -> dict | None:
        return self.files.get(key)

    def set(self, key: str, file_hash: str, remote_fp: str, local_fp: str | None = None) -> None:
        self.files[key] = {"hash": file_hash, "remote": remote_fp, "local": local_fp}

    def save(self) -> None:
        data = {"version": 1, "scope": self.scope, "last_sync": self.last_sync, "files": self.files}
        write_atomic(self.path, json.dumps(data, indent=1).encode("utf-8"))


# ---------------------------------------------------------------- info dos jogos

def read_sfo(path: str) -> dict:
    """Lê um PARAM.SFO e devolve {chave: valor}."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\0PSF":
        return {}
    key_start, data_start, count = struct.unpack_from("<III", data, 8)
    out = {}
    for i in range(count):
        k_off, fmt, length, _max_len, d_off = struct.unpack_from("<HHIII", data, 20 + i * 16)
        k_end = data.index(b"\0", key_start + k_off)
        key = data[key_start + k_off:k_end].decode("ascii", "replace")
        raw = data[data_start + d_off:data_start + d_off + length]
        if fmt == 0x0404:
            out[key] = struct.unpack("<I", raw[:4])[0]
        elif fmt in (0x0204, 0x0004):
            out[key] = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    return out


def state_scope(pc_dir: str, vita_dir: str) -> str:
    return os.path.normcase(os.path.abspath(pc_dir)) + "|" + vita_dir.rstrip("/")


def games_info(pc_dir: str, vita_cache_dir: str | None = None) -> list[dict]:
    """Saves do PC + saves que só existem na Vita (cujo ícone/nome ficou em cache)."""
    games = []
    if os.path.isdir(pc_dir):
        for name in sorted(os.listdir(pc_dir)):
            folder = os.path.join(pc_dir, name)
            if os.path.isdir(folder):
                games.append(_folder_info(name, folder, "pc"))
    if vita_cache_dir and os.path.isdir(vita_cache_dir):
        have = {g["id"] for g in games}
        for name in sorted(os.listdir(vita_cache_dir)):
            folder = os.path.join(vita_cache_dir, name)
            if os.path.isdir(folder) and name not in have:
                info = _folder_info(name, folder, "vita")
                info["modified"] = None
                games.append(info)
    return games


_GAME_CODE = re.compile(r"^[A-Z]{4}\d{5}")


def game_code(save_id: str) -> str:
    """ULUS10311GHOST04 -> ULUS10311 (o código do jogo; o resto identifica o save)."""
    m = _GAME_CODE.match(save_id)
    return m.group(0) if m else save_id


def _clean(text: str) -> str:
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    return "\n".join(ln for ln in lines if ln and ln != "-")


def _folder_info(name: str, folder: str, where: str) -> dict:
    info = {"id": name, "game": game_code(name), "title": "", "subtitle": "", "detail": "",
            "icon": None, "modified": None, "where": where}
    sfo = os.path.join(folder, "PARAM.SFO")
    if os.path.isfile(sfo):
        try:
            p = read_sfo(sfo)
            info["title"] = (p.get("TITLE") or "").strip()
            info["subtitle"] = _clean(p.get("SAVEDATA_TITLE") or "")
            info["detail"] = _clean(p.get("SAVEDATA_DETAIL") or "")
        except (OSError, ValueError, struct.error):
            pass
    icon = os.path.join(folder, "ICON0.PNG")
    if os.path.isfile(icon):
        with open(icon, "rb") as f:
            info["icon"] = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    mtimes = [os.path.getmtime(os.path.join(d, f)) for d, _, fs in os.walk(folder) for f in fs]
    if mtimes:
        info["modified"] = max(mtimes)
    return info


def save_details(pc_dir: str, vita_cache_dir: str | None, save_ids: list[str]) -> dict:
    """Informação extra para a página de um jogo: imagem de fundo e lista de ficheiros."""
    out = {}
    for sid in save_ids:
        if not safe_name(sid):
            continue
        folder = os.path.join(pc_dir, sid)
        on_pc = os.path.isdir(folder)
        if not on_pc and vita_cache_dir:
            folder = os.path.join(vita_cache_dir, sid)
        d = {"files": None, "size": None, "pic": None}
        if on_pc:
            files = []
            for dp, _, fs in os.walk(folder):
                for f in fs:
                    full = os.path.join(dp, f)
                    files.append({"name": os.path.relpath(full, folder).replace(os.sep, "/"),
                                  "size": os.path.getsize(full)})
            files.sort(key=lambda x: x["name"])
            d["files"], d["size"] = files, sum(f["size"] for f in files)
        pic = os.path.join(folder, "PIC1.PNG")
        if os.path.isfile(pic):
            with open(pic, "rb") as f:
                d["pic"] = "data:image/png;base64," + base64.b64encode(f.read()).decode()
        out[sid] = d
    return out


# ---------------------------------------------------------------- deteção do PPSSPP

def _documents_dirs() -> list[str]:
    home = os.path.expanduser("~")
    dirs = []
    try:  # pasta "Documentos" real (pode estar redirecionada para o OneDrive)
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
            dirs.append(buf.value)
    except (AttributeError, OSError):
        pass
    for d in ("Documents", "Documentos", r"OneDrive\Documents", r"OneDrive\Documentos"):
        dirs.append(os.path.join(home, d))
    return dirs


def detect_ppsspp_savedata() -> list[dict]:
    """Procura pastas PSP/SAVEDATA do PPSSPP. Devolve [{path, saves}] da mais cheia para a mais vazia."""
    home = os.path.expanduser("~")
    roots: list[str] = []
    for docs in _documents_dirs():
        roots.append(os.path.join(docs, "PPSSPP"))          # versão instalada
        roots.append(os.path.join(docs, "PPSSPP", "memstick"))
    bases = _documents_dirs() + [os.path.join(home, d) for d in ("Desktop", "Downloads")] + [
        r"C:\Games", r"C:\Program Files", r"C:\Program Files (x86)", r"D:\Games", "D:\\"]
    for base in bases:                                        # versões portáteis: <pasta>\memstick
        try:
            for entry in os.scandir(base):
                if entry.is_dir() and "ppsspp" in entry.name.lower():
                    roots.append(os.path.join(entry.path, "memstick"))
                    roots.append(entry.path)
                    installed = os.path.join(entry.path, "installed.txt")
                    if os.path.isfile(installed):             # PPSSPP instalado com pasta personalizada
                        with open(installed, encoding="utf-8", errors="ignore") as f:
                            line = f.readline().strip()
                        if line:
                            roots.append(line)
        except OSError:
            continue

    found, seen = [], set()
    for root in roots:
        path = os.path.join(root, "PSP", "SAVEDATA")
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not os.path.isdir(path):
            continue
        seen.add(key)
        saves = sum(1 for e in os.scandir(path) if e.is_dir())
        found.append({"path": os.path.abspath(path), "saves": saves})
    found.sort(key=lambda x: -x["saves"])
    return found


# ---------------------------------------------------------------- motor

class SaveSync:
    def __init__(self, pc_dir: str, vita_dir: str, backup_dir: str, state_path: str,
                 emit: Callable[[dict], None] | None = None, vita_cache_dir: str | None = None,
                 history: History | None = None):
        self.pc_dir = pc_dir
        self.vita_dir = vita_dir.rstrip("/")
        self.backup_dir = backup_dir
        self.state_path = state_path
        self.vita_cache_dir = vita_cache_dir
        self.history = history
        self._emit_cb = emit or (lambda ev: print(ev))
        self._backup_root = ""
        self._cwd: str | None = None
        self.dry_run = False
        # contagem de bytes transferidos, para a velocidade e o tempo restante
        self._bytes = 0
        self._expected = 0
        self._t0 = 0.0
        self._last_emit = 0.0

    # -- velocidade
    def _count(self, n: int, name: str) -> None:
        self._bytes += n
        now = time.monotonic()
        if now - self._last_emit < 0.25:
            return
        self._last_emit = now
        elapsed = max(now - self._t0, 0.001)
        speed = self._bytes / elapsed
        eta = None
        if self._expected and speed > 0:
            eta = max(self._expected - self._bytes, 0) / speed
        self.emit("transfer", file=name, bytes=self._bytes, expected=self._expected,
                  speed=speed, eta=eta)

    # -- eventos para a interface
    # Os textos não são escritos aqui: envia-se uma chave + parâmetros e a interface traduz.
    def emit(self, type_: str, **data) -> None:
        self._emit_cb({"type": type_, **data})

    def status(self, key: str, progress: float, **params) -> None:
        self.emit("status", key=key, args=params, progress=progress)

    def log(self, key: str, level: str = "info", **params) -> None:
        self.emit("log", key=key, args=params, level=level,
                  time=datetime.datetime.now().strftime("%H:%M:%S"))

    # -- FTP
    # Os ficheiros de um save são identificados pelo caminho relativo à pasta do save,
    # sempre com "/" (ex: "DATA.BIN" ou "SLOTS/slot1.bin"), dos dois lados.

    def _chdir(self, ftp: ftplib.FTP, path: str) -> None:
        if self._cwd != path:
            self._cwd = None
            ftp.cwd(path)
            self._cwd = path

    def _list(self, ftp: ftplib.FTP, path: str) -> dict[str, RemoteEntry]:
        self._chdir(ftp, path)
        lines: list[str] = []
        ftp.retrlines("LIST", lines.append)
        entries = {}
        for line in lines:
            e = parse_list_line(line)
            if e:
                entries[e.name] = e
        return entries

    def _list_tree(self, ftp: ftplib.FTP, root: str, prefix: str = "") -> dict[str, RemoteEntry]:
        """Todos os ficheiros dentro de root, incluindo os que estão em subpastas."""
        files = {}
        for name, e in self._list(ftp, root).items():
            rel = prefix + name
            if e.is_dir:
                files.update(self._list_tree(ftp, f"{root}/{name}", rel + "/"))
            else:
                files[rel] = e
        return files

    def _split(self, root: str, rel: str) -> tuple[str, str]:
        folder, _, name = rel.rpartition("/")
        return (f"{root}/{folder}" if folder else root), name

    def _download(self, ftp: ftplib.FTP, root: str, rel: str) -> bytes:
        folder, name = self._split(root, rel)
        self._chdir(ftp, folder)
        buf = io.BytesIO()

        def take(chunk: bytes) -> None:
            buf.write(chunk)
            if buf.tell() > MAX_FILE_BYTES:      # servidor a enviar dados sem fim
                raise ftplib.error_perm(f"550 {rel}: file too large")
            self._count(len(chunk), rel)

        ftp.retrbinary(f"RETR {name}", take)
        return buf.getvalue()

    def _upload(self, ftp: ftplib.FTP, root: str, rel: str, local_path: str) -> None:
        folder, name = self._split(root, rel)
        try:
            self._chdir(ftp, folder)
        except ftplib.error_perm:
            self._ensure_remote_dir(ftp, folder)
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {name}", f, callback=lambda chunk: self._count(len(chunk), rel))

    def _ensure_remote_dir(self, ftp: ftplib.FTP, path: str) -> None:
        self._cwd = None
        ftp.cwd("/")
        for seg in [s for s in path.split("/") if s]:
            try:
                ftp.cwd(seg)
            except ftplib.error_perm:
                ftp.mkd(seg)
                ftp.cwd(seg)
        self._cwd = path

    def _backup(self, side: str, game: str, rel: str, data: bytes) -> None:
        path = os.path.join(self._backup_root, side, game, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    @staticmethod
    def _local_tree(folder: str) -> dict[str, str]:
        files = {}
        if os.path.isdir(folder):
            for dirpath, _dirs, names in os.walk(folder):
                for n in names:
                    if n.endswith(".synctmp"):
                        continue
                    full = os.path.join(dirpath, n)
                    files[os.path.relpath(full, folder).replace(os.sep, "/")] = full
        return files

    # -- ciclo principal
    def run(self, ip: str, port: int | str, mode: str = "auto",
            only_games: list[str] | None = None, timeout: float = 10,
            dry_run: bool = False, excluded: list[str] | tuple = (),
            expected_bytes: int = 0, overrides: dict[str, str] | None = None) -> dict:
        """Sincroniza (ou, com dry_run=True, só calcula o que seria copiado: lê da Vita mas não
        escreve nada, nem no PC, nem na Vita, nem no estado).

        only_games: só estes saves.  excluded: saves a ignorar sempre.
        overrides: {save: modo} força a direção de saves específicos (ex: um conflito
        resolvido na pré-visualização com "Vita" -> {"ULUS10311GHOST05": "vita_to_pc"}).
        expected_bytes: total estimado (da pré-visualização), para o tempo restante.
        """
        overrides = {k: v for k, v in (overrides or {}).items() if v in MODES}
        if mode not in MODES:
            raise ValueError(f"Modo inválido: {mode}")
        if not os.path.isdir(self.pc_dir):
            raise SyncError("err_pc_dir_missing", path=self.pc_dir)

        self.dry_run = dry_run
        self._bytes, self._expected = 0, expected_bytes
        self._t0 = self._last_emit = time.monotonic()
        excluded = set(excluded)
        state = SyncState(self.state_path, state_scope(self.pc_dir, self.vita_dir))
        self._backup_root = new_backup_root(self.backup_dir)
        summary = {"dry_run": dry_run, "mode": mode, "downloaded": 0, "uploaded": 0, "synced": 0,
                   "excluded": 0, "conflicts": [], "errors": [], "games": {},
                   "plan_bytes": 0}

        self.status("st_connecting", 0.02)
        self.log("log_connecting", ip=ip, port=str(port), mode=mode, preview=dry_run)
        ftp = ftplib.FTP()
        self._cwd = None
        ftp.connect(ip, int(port), timeout=timeout)
        ftp.login()
        self.log("log_connected")

        try:
            try:
                remote_games = {n for n, e in self._list(ftp, self.vita_dir).items() if e.is_dir}
            except ftplib.error_perm:
                remote_games = set()
                if mode != "vita_to_pc" and not dry_run:
                    self._ensure_remote_dir(ftp, self.vita_dir)
                    self.log("log_created_dir", path=self.vita_dir)

            local_games = {d for d in os.listdir(self.pc_dir)
                           if os.path.isdir(os.path.join(self.pc_dir, d))}
            games = sorted(local_games | remote_games)
            if only_games is not None:
                games = [g for g in games if g in set(only_games)]

            for i, game in enumerate(games):
                self.status("st_analyzing" if dry_run else "st_syncing",
                            0.05 + 0.95 * i / max(len(games), 1), save=game)
                if game in excluded:
                    result = {"status": "excluded", "count": 0}
                else:
                    try:
                        read_before = self._bytes
                        result = self._sync_game(ftp, game, overrides.get(game, mode), state,
                                                 game in remote_games)
                        result["read"] = self._bytes - read_before   # lido só para comparar
                    except (ftplib.error_perm, ftplib.error_temp) as e:
                        # erro só neste save (ex: ficheiro bloqueado); os outros continuam
                        result = {"status": "error", "count": 0, "detail": str(e)}
                        self.log("log_ftp_error", "error", save=game, error=str(e))
                        self._cwd = None
                    if not dry_run:
                        state.save()

                st = result["status"]
                summary["games"][game] = result
                summary["plan_bytes"] += result.get("bytes", 0)
                if st in ("downloaded", "plan_down"):      # contagens em saves, não ficheiros
                    summary["downloaded"] += 1
                elif st in ("uploaded", "plan_up"):
                    summary["uploaded"] += 1
                elif st == "conflict":
                    summary["conflicts"].append(game)
                elif st == "error":
                    summary["errors"].append(game)
                elif st == "excluded":
                    summary["excluded"] += 1
                else:
                    summary["synced"] += 1
                if not dry_run:
                    self.emit("game", id=game, **result)

            if not dry_run:
                state.last_sync = datetime.datetime.now().isoformat(timespec="seconds")
                state.save()
                if self.history:
                    self.history.save()
            if only_games is None:   # só lê da Vita, por isso também corre na pré-visualização
                self._refresh_vita_cache(ftp, remote_games)
        finally:
            try:
                ftp.quit()
            except (ftplib.Error, OSError):
                ftp.close()
            if os.path.isdir(self._backup_root) and not os.listdir(self._backup_root):
                os.rmdir(self._backup_root)

        # Na sincronização a seguir à pré-visualização, as leituras para comparar repetem-se,
        # por isso o total esperado = o que já se leu agora + o que falta copiar.
        summary["expected_bytes"] = self._bytes + summary["plan_bytes"]
        summary["last_sync"] = state.last_sync
        summary["backup_dir"] = self._backup_root if os.path.isdir(self._backup_root) else None
        self.status("st_done", 1.0)
        return summary

    def _refresh_vita_cache(self, ftp: ftplib.FTP, remote_games: set[str]) -> None:
        """Guarda ícone e nome dos saves que só existem na Vita, para a interface os mostrar."""
        if not self.vita_cache_dir:
            return
        os.makedirs(self.vita_cache_dir, exist_ok=True)
        vita_only = {g for g in remote_games if not os.path.isdir(os.path.join(self.pc_dir, g))}
        for name in os.listdir(self.vita_cache_dir):          # limpa o que já não é "só na Vita"
            if name not in vita_only:
                shutil.rmtree(os.path.join(self.vita_cache_dir, name), ignore_errors=True)
        for game in sorted(vita_only):
            folder = os.path.join(self.vita_cache_dir, game)
            if os.path.isfile(os.path.join(folder, "PARAM.SFO")):
                continue          # já em cache (uma tentativa falhada volta a ser feita)
            os.makedirs(folder, exist_ok=True)
            for meta in ("PARAM.SFO", "ICON0.PNG", "PIC1.PNG"):
                try:
                    data = self._download(ftp, f"{self.vita_dir}/{game}", meta)
                    write_atomic(os.path.join(folder, meta), data)
                except ftplib.error_perm:
                    pass

    def _sync_game(self, ftp: ftplib.FTP, game: str, mode: str,
                   state: SyncState, exists_remote: bool) -> dict:
        local_dir = os.path.join(self.pc_dir, game)
        remote_dir = f"{self.vita_dir}/{game}"

        local_files = self._local_tree(local_dir)
        remote_files = self._list_tree(ftp, remote_dir) if exists_remote else {}

        local_hash: dict[str, str] = {}
        local_fp: dict[str, str] = {}
        cache: dict[str, bytes] = {}   # ficheiros da Vita já descarregados nesta passagem
        ups, downs, conflicts = [], [], []

        for name in sorted(set(local_files) | set(remote_files)):
            key = f"{game}/{name}"
            known = state.get(key)
            lp = local_files.get(name)
            remote = remote_files.get(name)
            if lp:
                local_fp[name] = local_fingerprint(lp)
                if known and known.get("local") == local_fp[name]:
                    local_hash[name] = known["hash"]   # o PC não mudou: não é preciso reler
                else:
                    local_hash[name] = md5_file(lp)

            if lp and remote:
                lh = local_hash[name]
                if known and known["remote"] == remote.fingerprint:
                    rh = known["hash"]          # a Vita não mudou desde a última vez
                else:
                    cache[name] = self._download(ftp, remote_dir, name)
                    rh = md5_bytes(cache[name])
                if lh == rh:
                    state.set(key, lh, remote.fingerprint, local_fp[name])
                    continue
                if mode == "vita_to_pc":
                    downs.append(name)
                elif mode == "pc_to_vita":
                    ups.append(name)
                elif known is None:
                    conflicts.append(name)
                else:
                    pc_changed = lh != known["hash"]
                    vita_changed = rh != known["hash"]
                    if pc_changed and not vita_changed:
                        ups.append(name)
                    elif vita_changed and not pc_changed:
                        downs.append(name)
                    else:
                        conflicts.append(name)
            elif lp:
                if mode != "vita_to_pc":
                    ups.append(name)
            elif remote:
                if mode != "pc_to_vita":
                    downs.append(name)

        # Um save é um conjunto (DATA.BIN + PARAM.SFO + ...): nunca misturar versões.
        if mode == "auto" and (conflicts or (ups and downs)):
            pc_m = max((os.path.getmtime(p) for p in local_files.values()), default=None)
            vita_m = max((t for t in (ftp_date_to_ts(e.fingerprint) for e in remote_files.values())
                          if t is not None), default=None)
            if not self.dry_run:
                self.log("log_conflict", "warn", save=game)
            return {"status": "conflict", "count": 0, "pc_date": pc_m, "vita_date": vita_m,
                    "size_pc": sum(os.path.getsize(p) for p in local_files.values()),
                    "size_vita": sum(remote_size(e) for e in remote_files.values())}

        if self.dry_run:
            if downs:
                files = [{"name": n, "size": remote_size(remote_files[n])} for n in downs]
                to_read = sum(f["size"] for f in files if f["name"] not in cache)
                return {"status": "plan_down", "count": len(downs), "files": files, "bytes": to_read}
            if ups:
                files = [{"name": n, "size": os.path.getsize(local_files[n])} for n in ups]
                to_move = sum(f["size"] for f in files) + sum(
                    remote_size(remote_files[n]) for n in ups if n in remote_files and n not in cache)
                return {"status": "plan_up", "count": len(ups), "files": files, "bytes": to_move}
            return {"status": "synced", "count": 0}

        if downs:
            for name in downs:
                data = cache.get(name)
                if data is None:
                    data = self._download(ftp, remote_dir, name)
                lp = os.path.join(local_dir, *name.split("/"))
                os.makedirs(os.path.dirname(lp), exist_ok=True)
                if os.path.exists(lp):
                    with open(lp, "rb") as f:
                        self._backup("PC", game, name, f.read())
                write_atomic(lp, data)
                state.set(f"{game}/{name}", md5_bytes(data), remote_files[name].fingerprint,
                          local_fingerprint(lp))
                self.log("log_down", path=f"{game}/{name}")
            if self.history:
                self.history.add(game, "down", len(downs))
            return {"status": "downloaded", "count": len(downs)}

        if ups:
            for name in ups:
                if name in remote_files:
                    data = cache.get(name)
                    if data is None:
                        data = self._download(ftp, remote_dir, name)
                    self._backup("Vita", game, name, data)
                self._upload(ftp, remote_dir, name, local_files[name])
                self.log("log_up", path=f"{game}/{name}")
            # Regista a impressão digital nova, para não voltar a descarregar o mesmo save.
            fresh = self._list_tree(ftp, remote_dir)
            for name in ups:
                if name in fresh:
                    state.set(f"{game}/{name}", local_hash[name], fresh[name].fingerprint, local_fp[name])
            if self.history:
                self.history.add(game, "up", len(ups))
            return {"status": "uploaded", "count": len(ups)}

        return {"status": "synced", "count": 0}
