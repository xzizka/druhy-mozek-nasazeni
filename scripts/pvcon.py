#!/usr/bin/env python3
"""
Minimalni klient Proxmox termproxy konzole pres WebSocket.

Duvod existence: Proxmox API neumi spustit prikaz v LXC (guest agent je jen
pro VM) a kontejner 201 sedi na vmbr1 (10.20.0.0/24), kam z teto stanice
neexistuje routa. Konzole je jediny kanal, ktery na siti nezavisi.

Pouziva se zamerne minimalne - jen k instalaci a pripojeni Tailscale.
Potom uz vse jde po SSH pres Tailscale.

Protokol (stejny, jaky pouziva xterm.js v PVE UI):
  - prvni zprava po handshaku:  "<user>:<ticket>\n"
  - vstup:                      "0:<bytelen>:<data>"
  - resize:                     "1:<cols>:<rows>:"
  - ping:                       "2"

Zadna knihovna websockets neni k dispozici, RFC6455 ramce se tvori rucne.
"""
import base64
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
import urllib.parse
import urllib.request

HOST = "192.168.88.1"
PORT = 8006
NODE = "pve1"
VMID = "201"
TOKEN_ID = "cli@pam!t2"
TOKEN_SECRET = os.environ["PVE_TOKEN"]
AUTH = f"PVEAPIToken={TOKEN_ID}={TOKEN_SECRET}"

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def api(method, path, data=None):
    url = f"https://{HOST}:{PORT}/api2/json{path}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", AUTH)
    with urllib.request.urlopen(req, context=_ctx, timeout=30) as r:
        return json.load(r)["data"]


class WS:
    def __init__(self, sock):
        self.s = sock
        self.buf = b""

    def send(self, text, opcode=0x1):
        payload = text.encode() if isinstance(text, str) else text
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.s.sendall(hdr + mask + masked)

    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("socket zavren serverem")
            self.buf += chunk

    def recv_frame(self):
        self._fill(2)
        b0, b1 = self.buf[0], self.buf[1]
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        off = 2
        if ln == 126:
            self._fill(4)
            ln = struct.unpack(">H", self.buf[2:4])[0]
            off = 4
        elif ln == 127:
            self._fill(10)
            ln = struct.unpack(">Q", self.buf[2:10])[0]
            off = 10
        if masked:
            self._fill(off + 4)
            mask = self.buf[off:off + 4]
            off += 4
        self._fill(off + ln)
        payload = self.buf[off:off + ln]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.buf = self.buf[off + ln:]
        return opcode, payload


def connect():
    tp = api("POST", f"/nodes/{NODE}/lxc/{VMID}/termproxy")
    ticket, port, user = tp["ticket"], tp["port"], tp["user"]

    raw = socket.create_connection((HOST, PORT), timeout=30)
    sock = _ctx.wrap_socket(raw, server_hostname=HOST)

    key = base64.b64encode(os.urandom(16)).decode()
    q = urllib.parse.urlencode({"port": port, "vncticket": ticket})
    req = (
        f"GET /api2/json/nodes/{NODE}/lxc/{VMID}/vncwebsocket?{q} HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: binary\r\n"
        f"Authorization: {AUTH}\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())

    resp = b""
    while b"\r\n\r\n" not in resp:
        c = sock.recv(4096)
        if not c:
            raise ConnectionError("handshake: server zavrel spojeni")
        resp += c
    head, _, rest = resp.partition(b"\r\n\r\n")
    if b"101" not in head.split(b"\r\n")[0]:
        raise ConnectionError("handshake selhal:\n" + head.decode(errors="replace"))

    ws = WS(sock)
    ws.buf = rest
    ws.send(f"{user}:{ticket}\n")
    time.sleep(0.5)
    ws.send("1:200:50:")          # resize, aby se dlouhe radky nelamaly
    return ws


def drain(ws, until=None, timeout=60):
    """Cte dokud nenarazi na sentinel `until` nebo nevyprsi timeout."""
    out = []
    deadline = time.time() + timeout
    ws.s.settimeout(2)
    while time.time() < deadline:
        try:
            opcode, payload = ws.recv_frame()
        except socket.timeout:
            if until is None:
                break
            continue
        except ConnectionError:
            break
        if opcode in (0x8,):
            break
        if opcode in (0x9,):
            ws.send(b"", 0xA)
            continue
        if opcode in (0xA,):
            continue
        text = payload.decode("utf-8", errors="replace")
        out.append(text)
        if until and until in "".join(out):
            break
    return "".join(out)


ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")


def run(ws, cmd, timeout=int(os.environ.get("PVCON_TIMEOUT", "60")), echo=True):
    """Posle prikaz a precte vystup az po sentinel.

    Sentinel se sklada za behu ze dvou casti ($M), takze se literal NIKDY
    neobjevi v echu prikazu. Bez toho drain skonci uz na echu a vystupy se
    posunou o jeden prikaz.
    """
    n = int(time.time() * 1000 % 10**9)
    marker = f"__DONE_{n}__"
    full = f'M="__DO""NE_{n}__"; {cmd}; printf "%srr=%s\\n" "$M" $?\n'
    ws.send(f"0:{len(full.encode())}:{full}")
    out = drain(ws, until=marker + "rr=", timeout=timeout)
    out = ANSI.sub("", out)
    m = re.search(re.escape(marker) + r"rr=(\d+)", out)
    rc = int(m.group(1)) if m else -1
    # zahodit vse do konce echa prikazu a sentinel radek
    lines = []
    for line in out.splitlines():
        if marker in line or '__DO""NE_' in line:
            continue
        lines.append(line.rstrip())
    out = "\n".join(lines).strip()
    if echo:
        print(f"$ {cmd}")
        for line in out.splitlines():
            if line.strip() and not line.strip().startswith("root@brain:"):
                print("   " + line)
        print(f"   [rc={rc}]")
    return rc, out


if __name__ == "__main__":
    ws = connect()
    drain(ws, timeout=3)
    cmds = sys.argv[1:]
    if not cmds:
        cmds = ["hostname", "cat /etc/debian_version", "ip -4 -br addr"]
    worst = 0
    for c in cmds:
        rc, _ = run(ws, c)
        worst = worst or rc
    sys.exit(0 if worst == 0 else 1)
