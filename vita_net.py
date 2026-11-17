"""Ligação à Vita: testar o FTP e procurá-la na rede local."""

from __future__ import annotations

import ftplib
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor


def test_connection(ip: str, port: int | str, vita_dir: str, timeout: float = 5) -> dict:
    """Devolve {ok, key, args}; a interface traduz `key` (ver ui/i18n.js)."""
    ip = str(ip).strip()
    if not ip:
        return {"ok": False, "key": "vn_need_ip", "args": {}}
    ftp = ftplib.FTP()
    try:
        banner = ftp.connect(ip, int(port), timeout=timeout)
        ftp.login()
    except (socket.timeout, TimeoutError):
        return {"ok": False, "key": "vn_timeout", "args": {}}
    except ConnectionRefusedError:
        return {"ok": False, "key": "vn_refused", "args": {}}
    except ValueError:
        return {"ok": False, "key": "vn_bad_port", "args": {}}
    except (OSError, ftplib.Error) as e:
        return {"ok": False, "key": "vn_failed", "args": {"detail": str(e)}}
    try:
        try:
            ftp.cwd(vita_dir)
            lines: list[str] = []
            ftp.retrlines("LIST", lines.append)
            saves = sum(1 for ln in lines if ln.startswith("d") and not ln.rstrip().endswith((" .", " ..")))
            key = "vn_ok"
        except ftplib.error_perm:
            saves, key = 0, "vn_ok_nodir"
        return {"ok": True, "key": key, "args": {"n": saves}, "saves": saves, "banner": banner}
    finally:
        try:
            ftp.quit()
        except (OSError, ftplib.Error):
            ftp.close()


def _local_ipv4s() -> list[str]:
    ips = set()
    try:  # IP da placa de rede principal (não envia nada: UDP só escolhe a rota)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return [ip for ip in ips if ipaddress.ip_address(ip).is_private and not ip.startswith("127.")]


def _probe(ip: str, port: int) -> dict | None:
    try:
        with socket.create_connection((ip, port), timeout=0.5) as s:
            s.settimeout(1.5)
            try:
                banner = s.recv(200).decode("ascii", "replace").strip()
            except OSError:
                banner = ""
        return {"ip": ip, "banner": banner, "vita": "vita" in banner.lower()}
    except OSError:
        return None


def find_vitas(port: int | str = 1337) -> list[dict]:
    """Procura na rede local (/24) aparelhos com a porta do FTP aberta. Vitas primeiro."""
    port = int(port)
    mine = _local_ipv4s()
    targets = []
    for ip in mine:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        targets += [str(h) for h in net.hosts() if str(h) not in mine]
    with ThreadPoolExecutor(max_workers=128) as pool:
        found = [r for r in pool.map(lambda h: _probe(h, port), targets) if r]
    found.sort(key=lambda r: (not r["vita"], tuple(int(x) for x in r["ip"].split("."))))
    return found
