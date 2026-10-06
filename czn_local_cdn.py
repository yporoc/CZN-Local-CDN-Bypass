import datetime
import http.server
import os
import shutil
import socket
import ssl
import subprocess
import sys

# 冻结成 exe 时，可写文件（证书/ini）放 exe 旁，而非解包临时目录
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable))
           if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))
DOMAIN = "czn-live-down.game.playstove.com"
MARKER = "# czn-manifest-responder"
PORT = 443
HOSTS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     r"System32\drivers\etc\hosts")
CERT = os.path.join(APP_DIR, "responder.pem")        # 叶子+CA+私钥
CA_CER = os.path.join(APP_DIR, "responder_ca.cer")   # CA 证书
CONFIG = os.path.join(APP_DIR, "czn_cdn.ini")

# 成功抓包实测的响应头模板，逐字复刻，勿增删
REAL_HEADERS = {
    "Content-Type":  "application/octet-stream",
    "Accept-Ranges": "bytes",
    "Server":        "AmazonS3",
    "Last-Modified": "Thu, 01 Oct 2026 06:36:46 GMT",
    "x-amz-version-id": "S8iXRdlxz0tSwzoVHd13v4fUtAar444S",
    "x-amz-server-side-encryption": "AES256",
    "X-Cache":       "Hit from cloudfront",
    "X-Amz-Cf-Pop":  "NRT20-P9",
    "Age":           "23379",
}

GAMERES = None
log_callback = print    # 引擎日志出口，GUI 会替换成队列投递
ACTIVE_IP = None        # 本次成功绑定的回环 IP（hosts 收敛目标）
_MANIFEST = None
_ETAGFILE = None


def set_gameres(path):
    global GAMERES, _MANIFEST, _ETAGFILE
    if path and os.path.isdir(path):
        GAMERES = path
        _MANIFEST = os.path.join(path, "manifest.ssra")
        _ETAGFILE = os.path.join(path, "manifest.ssra.etag")
        return True
    return False


def detect_gameres():
    """定位顺序：环境变量 CZN_GAMERES → czn_cdn.ini。"""
    env = os.environ.get("CZN_GAMERES")
    if env and set_gameres(env):
        return GAMERES
    if os.path.exists(CONFIG):
        try:
            for line in open(CONFIG, encoding="utf-8", errors="replace"):
                line = line.strip()
                if line.lower().startswith("gameres"):
                    _, _, v = line.partition("=")
                    if set_gameres(v.strip().strip('"')):
                        return GAMERES
        except Exception:
            pass
    return None


def load_etag():
    try:
        for line in open(_ETAGFILE, encoding="utf-8", errors="replace").read().splitlines():
            line = line.strip()
            if line.startswith('"') and line.endswith('"') and len(line) > 4:
                return line
    except Exception:
        pass
    return '"local"'


def load_body():
    try:
        return open(_MANIFEST, "rb").read()
    except Exception:
        return b""


# ---------------------------------------------------------------- hosts
def hosts_install(ip=None):
    """收敛 hosts 到活动回环 IP：重写本工具标记行，只留当前 IP 这一条。"""
    ip = ip or ACTIVE_IP or "127.0.0.1"
    txt = open(HOSTS, encoding="utf-8", errors="replace").read()
    lines = txt.splitlines()
    new_line = "%s %s %s" % (ip, DOMAIN, MARKER)
    if any(l.strip() == new_line for l in lines):
        log_callback("[hosts] 条目已指向 %s，跳过" % ip)
        return
    if not txt.endswith("\n"):
        txt += "\r\n"
    bak = HOSTS + ".responder-backup"
    if not os.path.exists(bak):
        shutil.copyfile(HOSTS, bak)
        log_callback("[hosts] 原文件已备份: " + bak)
    keep = [l for l in lines if MARKER not in l]
    open(HOSTS, "w", encoding="utf-8").write(
        "\r\n".join(keep + [new_line]) + "\r\n")
    old = [l.split()[0] for l in lines if MARKER in l]
    if old:
        log_callback("[hosts] 条目已更新: %s → %s" % ("、".join(old), ip))
    else:
        log_callback("[hosts] 条目已写入（%s）" % ip)


def hosts_uninstall():
    try:
        lines = open(HOSTS, encoding="utf-8", errors="replace").read().splitlines()
    except OSError as e:
        log_callback("[hosts] 读取失败: %s" % e)
        return
    keep = [l for l in lines if MARKER not in l]
    if len(keep) == len(lines):
        log_callback("[hosts] 无本工具条目，跳过")
        return
    open(HOSTS, "w", encoding="utf-8").write("\r\n".join(keep) + "\r\n")
    log_callback("[hosts] 已移除 %d 条条目，hosts 已还原" % (len(lines) - len(keep)))


def hosts_status():
    return DOMAIN in open(HOSTS, encoding="utf-8", errors="replace").read()


# ---------------------------------------------------------------- cert
def ensure_cert():
    """缺失则生成：自签 CA → CA 签发站点证书（CN/SAN=游戏域名）。"""
    if os.path.exists(CERT) and os.path.exists(CA_CER):
        return True
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        log_callback("[cert] 缺少 cryptography（pip install cryptography）")
        return False
    now = datetime.datetime.now(datetime.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                            "czn-manifest-responder CA")])
    ca = (x509.CertificateBuilder()
          .subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                         critical=True)
          .add_extension(x509.KeyUsage(
              digital_signature=True, key_cert_sign=True, crl_sign=True,
              content_commitment=False, key_encipherment=False,
              data_encipherment=False, key_agreement=False,
              encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DOMAIN)])
    leaf = (x509.CertificateBuilder()
            .subject_name(leaf_name).issuer_name(ca.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(DOMAIN)]),
                           critical=False)
            .add_extension(x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    key_pem = leaf_key.private_bytes(
        pem, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption())
    # ssl.load_cert_chain 要求顺序：叶子 → CA 链 → 私钥
    open(CERT, "wb").write(
        leaf.public_bytes(pem) + ca.public_bytes(pem) + key_pem)
    open(CA_CER, "wb").write(ca.public_bytes(pem))
    log_callback("[cert] 已生成 CA + 站点证书（CA 指纹 %s…，有效期 10 年）"
                 % cert_thumb()[:16])
    return True


def cert_thumb():
    """CA 证书的 SHA1 指纹。"""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        ca = x509.load_pem_x509_certificate(open(CA_CER, "rb").read())
        return ca.fingerprint(hashes.SHA1()).hex().upper()
    except Exception:
        return None


def _certutil(args, timeout=20):
    # certutil 输出是 GBK，严格 UTF-8 解码会炸掉读取线程；
    # 指纹本身是 ASCII，容错解码后的乱码不影响比对
    try:
        p = subprocess.run(["certutil"] + args, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout,
                           creationflags=0x08000000)   # CREATE_NO_WINDOW
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))
    except Exception as e:
        return -1, str(e)


def cert_in_store():
    """CA 是否已装入受信任根。certutil 指纹过滤不可靠（不存在也 rc=0），枚举全库精确比对。"""
    thumb = cert_thumb()
    if not thumb:
        return False
    rc, out = _certutil(["-store", "Root"])
    if rc != 0:
        log_callback("[cert] 枚举受信任根失败 rc=%d" % rc)
        return False
    return thumb.lower() in out.lower().replace(" ", "")


def cert_install():
    if not ensure_cert() or not os.path.exists(CA_CER):
        log_callback("[cert] ✘ 证书文件不可用")
        return False
    if cert_in_store():
        log_callback("[cert] CA 已在受信任根中，跳过安装")
        return True
    rc, out = _certutil(["-addstore", "-f", "Root", CA_CER])
    if rc == 0:
        log_callback("[cert] CA 已装入受信任根（“停止并还原”会自动移除）")
        return True
    tail = out.strip().splitlines()[-1] if out.strip() else "certutil 无输出"
    log_callback("[cert] ✘ 装入失败 rc=%d：%s（需要管理员权限）" % (rc, tail))
    return False


def cert_uninstall():
    thumb = cert_thumb()
    if not thumb:
        log_callback("[cert] 无证书可移除")
        return False
    rc, out = _certutil(["-delstore", "Root", thumb])
    if rc == 0:
        log_callback("[cert] CA 已从受信任根移除")
        return True
    tail = out.strip().splitlines()[-1] if out.strip() else "certutil 无输出"
    log_callback("[cert] 受信任根中未找到本工具 CA（rc=%d：%s）" % (rc, tail))
    return False


# ---------------------------------------------------------------- server
class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass    # 详细请求日志在 go()

    def _send(self, code, body=b"", extra=None):
        # 与成功抓包行为一致：Server/Date 允许重复；304 也发 Content-Length: 0
        self.send_response(code)
        h = dict(REAL_HEADERS)
        if extra:
            h.update(extra)
        h["Date"] = self.date_time_string()
        h["Content-Length"] = str(len(body))
        h["Connection"] = "keep-alive"
        for k, v in h.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def do_HEAD(self):
        self.go()

    def do_GET(self):
        self.go()

    def go(self):
        p = self.path.split("?")[0]
        who = self.client_address[0] if self.client_address else "?"
        via = "1.1 %08x.cloudfront.net (CloudFront)" % int.from_bytes(
            os.urandom(4), "big")
        cfid = "%040x" % int.from_bytes(os.urandom(20), "big")
        extra = {"Via": via, "X-Amz-Cf-Id": cfid}
        if _MANIFEST and "manifest.ssra" in p:
            inm = self.headers.get("If-None-Match", "")
            etag = load_etag()
            if inm and etag and etag in inm:
                log_callback("[http] %s %s %s → 304（ETag 命中，游戏判定清单未变）"
                             % (self.command, who, p))
                return self._send(304, b"", {"ETag": etag, **extra})
            body = load_body()
            log_callback("[http] %s %s %s → 200（%s 字节）"
                         % (self.command, who, p, format(len(body), ",")))
            return self._send(200, body, {"ETag": etag, **extra})
        log_callback("[http] %s %s %s → 404（非清单请求，直接落空）"
                     % (self.command, who, p))
        return self._send(404, b"{}")


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False    # Windows 上 SO_REUSEADDR 等于允许端口劫持，不开

    def server_bind(self):
        # 独占绑定：其他进程无法再绑同一地址（含 SO_REUSEADDR 劫持者）
        excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if excl is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, excl, 1)
        return http.server.HTTPServer.server_bind(self)

    def handle_error(self, request, client_address):
        # TLS 握手失败不进 handler，必须在这里进日志，否则连接失败不可见
        exc = sys.exc_info()[1]
        log_callback("[tls] %s 连接失败: %r（多为握手失败 —— 检查 CA 是否已装入受信任根）"
                     % (client_address[0], exc))


_LOOPBACK_CANDIDATES = ["127.0.0.%d" % i for i in range(1, 21)]


def make_server():
    """从 127.0.0.1 起逐个尝试回环 IP 的 443 端口；全部占用则点名占用者。"""
    global ACTIVE_IP
    if not ensure_cert():
        return None
    srv = None
    for ip in _LOOPBACK_CANDIDATES:
        try:
            srv = _Server((ip, PORT), _Handler)
            ACTIVE_IP = ip
            break
        except OSError:
            log_callback("[srv] %s:%d 被占用，尝试下一个回环 IP…" % (ip, PORT))
    if srv is None:
        for pid, name in _port_occupiers():
            log_callback("[srv] 占用者: %s (PID %s)" % (name, pid))
        log_callback("[srv] ✘ 端口 %d 在所有回环 IP 上均被占用"
                     " —— 请结束占用程序后重试" % PORT)
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    log_callback("[srv] TLS 证书链已加载（叶子 CN=%s ← CA 指纹 %s…），监听 %s:%d"
                 % (DOMAIN, (cert_thumb() or "?")[:16], ACTIVE_IP, PORT))
    return srv


def _port_occupiers():
    """反查本机 443 监听者，返回 [(pid, 进程名)]。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             timeout=15).stdout.decode("gbk", "replace")
    except Exception:
        return []
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and \
                parts[1].endswith(":%d" % PORT):
            pids.add(parts[4])
    names = []
    for pid in sorted(pids, key=lambda x: int(x) if x.isdigit() else 0):
        name = "未知进程"
        try:
            t = subprocess.run(
                ["tasklist", "/FI", "PID eq %s" % pid, "/FO", "CSV", "/NH"],
                capture_output=True, timeout=15).stdout.decode("gbk", "replace")
            row = [l for l in t.splitlines() if l.strip().startswith('"')]
            if row:
                name = row[0].split('","')[0].strip('"')
        except Exception:
            pass
        if pid == "4":
            name = "System（内核 HTTP.SYS —— IIS/WinRM 等）"
        names.append((pid, name))
    return names


def manifest_path():
    return _MANIFEST


def manifest_size():
    return len(load_body()) if _MANIFEST else 0


def current_etag():
    return load_etag() if _MANIFEST else None


def status():
    return {
        "hosts": hosts_status(),
        "cert": os.path.exists(CERT),
        "cert_in_store": cert_in_store(),
        "gameres": GAMERES,
        "manifest_bytes": manifest_size(),
        "port_free": not _port_busy(),
    }


def _port_busy():
    s = socket.socket()
    s.settimeout(1)
    try:
        s.bind(("127.0.0.1", PORT))
        s.close()
        return False
    except OSError:
        return True


if __name__ == "__main__":
    detect_gameres()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "install":
        hosts_install()
    elif cmd == "uninstall":
        hosts_uninstall()
    elif cmd == "cert-install":
        cert_install()
    elif cmd == "cert-uninstall":
        cert_uninstall()
    elif cmd == "serve":
        if not GAMERES:
            print("[x] gameres not found")
            sys.exit(1)
        srv = make_server()
        if srv:
            print("[+] serving on :%d" % PORT)
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
    else:
        print(status())
