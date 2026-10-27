"""KILLU X HOST backend: upload code, run it, watch it, restart it.

Run:  pip install flask  &&  python server.py   ->  http://localhost:8000
"""
import hmac, json, os, re, shlex, shutil, signal, subprocess, sys, threading, time, zipfile
from pathlib import Path
from flask import Flask, Response, jsonify, request, send_file, send_from_directory

BASE = Path(__file__).parent.resolve()
DATA = Path(os.environ.get("DATA_DIR", BASE / "data")).resolve()
BOTS, DB = DATA / "bots", DATA / "bots.json"
BOTS.mkdir(parents=True, exist_ok=True)

LOCK = threading.RLock()
PROCS = {}      # bot id -> {"p": Popen|None, "start": ts, "state": str, "tok": object}
CRASHES = {}    # bot id -> [timestamps]
HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024


def load():
    try:
        return json.loads(DB.read_text())
    except Exception:
        return []


STATE = load()


def save():
    DB.write_text(json.dumps(STATE, indent=1))


def bdir(b):
    return BOTS / str(b["id"])


def find(i):
    return next((b for b in STATE if b["id"] == i), None)


def err(msg, code=400):
    return jsonify(error=msg), code


def slog(b, msg):
    with open(bdir(b) / "bot.log", "ab") as h:
        h.write(f"[killu] {time.strftime('%H:%M:%S')} {msg}\n".encode())


def tail(b, n=80):
    try:
        with open(bdir(b) / "bot.log", "rb") as h:
            h.seek(0, 2)
            size = h.tell()
            h.seek(max(0, size - 20000))
            data = h.read()
        return ANSI.sub("", data.decode("utf-8", "replace")).splitlines()[-n:]
    except Exception:
        return []


def detect_cmd(lang, d):
    names = ["main.py", "bot.py", "app.py", "index.py"] if lang == "py" else ["index.js", "bot.js", "main.js", "app.js"]
    for n in names:
        if (d / n).exists():
            return ("python " if lang == "py" else "node ") + n
    if lang == "js" and (d / "package.json").exists():
        return "npm start"
    return None


# ---------- alerts, events, settings ----------
BOOT = time.time()
SETTINGS = DATA / "settings.json"
DISCORD_RE = re.compile(r"^https://(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$")


def get_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except Exception:
        return {}


def urlreq(url, payload):
    import urllib.request
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "KilluXHost/1.0"})
    urllib.request.urlopen(req, timeout=10).read()


def send_alert(text):
    """Returns an error message, or None when at least one channel worked."""
    s, errs, sent = get_settings(), [], False
    if s.get("discord"):
        try:
            urlreq(s["discord"], {"content": text[:1900]})
            sent = True
        except Exception as e:
            errs.append(f"Discord: {e}")
    if s.get("tg_token") and s.get("tg_chat"):
        try:
            urlreq(f"https://api.telegram.org/bot{s['tg_token']}/sendMessage", {"chat_id": s["tg_chat"], "text": text[:4000]})
            sent = True
        except Exception as e:
            errs.append(f"Telegram: {e}")
    if not sent and not errs:
        errs.append("No alert channel is set up yet")
    return "; ".join(errs) or None


def notify(text):
    threading.Thread(target=send_alert, args=(text,), daemon=True).start()


def event(b, text, alert=False):
    with LOCK:
        ev = b.setdefault("events", [])
        ev.append({"t": int(time.time()), "m": text})
        del ev[:-30]
        save()
    if alert:
        notify(f"\u26a0\ufe0f {b['name']}: {text}")


# ---------- running bots ----------
def _run(b, tok):
    i, d = b["id"], bdir(b)
    alive = lambda: PROCS.get(i, {}).get("tok") is tok
    env = {**os.environ, **b.get("env", {}), "PYTHONUNBUFFERED": "1"}
    env.pop("PANEL_USER", None)
    env.pop("PANEL_PASS", None)
    logp = d / "bot.log"
    if logp.exists() and logp.stat().st_size > 2_000_000:
        logp.write_bytes(b"")
    log = open(logp, "ab", buffering=0)

    def say(m):
        log.write(f"[killu] {time.strftime('%H:%M:%S')} {m}\n".encode())

    def fail(m):
        say(m)
        with LOCK:
            if alive():
                PROCS.pop(i, None)
                b["want"] = False
                save()

    try:
        steps = []
        if b["lang"] == "py" and (d / "requirements.txt").exists():
            base = [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]
            steps.append(("requirements.txt", [base, base + ["--break-system-packages"]]))
        if b["lang"] == "js" and (d / "package.json").exists():
            steps.append(("package.json", [["npm", "install"]]))
        for label, attempts in steps:
            say(f"installing {label}…")
            ok = False
            for cmd in attempts:
                if subprocess.run(cmd, cwd=d, env=env, stdout=log, stderr=subprocess.STDOUT).returncode == 0:
                    ok = True
                    break
            if not ok:
                return fail(f"install failed ({label}). Fix it and press start again.")
            if not alive():
                return
        cmd = b.get("cmd") or detect_cmd(b["lang"], d)
        if cmd and " " not in cmd and cmd.lower().endswith((".py", ".js")):
            cmd = ("python " if cmd.lower().endswith(".py") else "node ") + cmd
        if not cmd:
            return fail("no start file found (main.py, bot.py, index.js …). Set a start command.")
        args = shlex.split(cmd)
        if args[0] in ("python", "python3"):
            args[0] = sys.executable
        if not alive():
            return
        say("starting: " + cmd)
        p = subprocess.Popen(args, cwd=d, env=env, stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        with LOCK:
            if alive():
                PROCS[i].update(p=p, start=time.time(), state="running")
            else:
                p.terminate()
    except Exception as e:
        fail(f"error: {e}")


def spawn(b):
    with LOCK:
        if b["id"] in PROCS:
            return
        tok = object()
        PROCS[b["id"]] = {"p": None, "start": time.time(), "state": "starting", "tok": tok}
    threading.Thread(target=_run, args=(b, tok), daemon=True).start()


def start_bot(b):
    with LOCK:
        b["want"] = True
        save()
    spawn(b)
    event(b, "Started")


def halt(b, quiet=False):
    with LOCK:
        b["want"] = False
        save()
        e = PROCS.pop(b["id"], None)
    p = e and e.get("p")
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            p.terminate()
        try:
            p.wait(timeout=4)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                p.kill()
    if not quiet and bdir(b).exists():
        slog(b, "stopped")
        event(b, "Stopped")


def watcher():
    while True:
        time.sleep(3)
        with LOCK:
            for b in list(STATE):
                e = PROCS.get(b["id"])
                hrs = b.get("restart_hours") or 0
                if (e and e.get("p") and e["p"].poll() is None and hrs and not e.get("sched")
                        and time.time() - e["start"] > hrs * 3600):
                    e["sched"] = True
                    event(b, f"Scheduled restart (every {hrs:g} h)")
                    threading.Thread(target=lambda x=b: (halt(x, quiet=True), start_bot(x)), daemon=True).start()
                    continue
                if not (e and e.get("p") and e["p"].poll() is not None):
                    continue
                code = e["p"].returncode
                PROCS.pop(b["id"], None)
                if not b.get("want"):
                    continue
                if b.get("auto", True) is False:
                    b["want"] = False
                    save()
                    slog(b, f"exited with code {code}. Auto-restart is off.")
                    event(b, f"Stopped on its own (exit code {code})", alert=True)
                    continue
                now = time.time()
                hist = [t for t in CRASHES.get(b["id"], []) if now - t < 120] + [now]
                CRASHES[b["id"]] = hist
                if len(hist) > 5:
                    b["want"] = False
                    save()
                    slog(b, f"exited with code {code} too many times. Auto-restart stopped.")
                    event(b, "Keeps crashing. Auto-restart gave up.", alert=True)
                else:
                    slog(b, f"exited with code {code}. Restarting in 5s…")
                    event(b, f"Crashed (exit code {code}), restarting", alert=True)
                    threading.Timer(5, lambda x=b: x.get("want") and spawn(x)).start()


def stats(e):
    p = e.get("p")
    if not p or p.poll() is not None:
        return 0, 0
    try:
        st = Path(f"/proc/{p.pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks, now = int(st[11]) + int(st[12]), time.time()
        rss = 0
        for line in Path(f"/proc/{p.pid}/status").read_text().splitlines():
            if line.startswith("VmRSS"):
                rss = int(line.split()[1]) // 1024
        prev, e["prev"] = e.get("prev"), (ticks, now)
        cpu = round((ticks - prev[0]) / HZ / (now - prev[1]) * 100) if prev and now > prev[1] else 0
        return max(cpu, 0), rss
    except Exception:
        return 0, 0


# ---------- optional password (set PANEL_USER and PANEL_PASS) ----------
@app.before_request
def guard():
    u, pw = os.environ.get("PANEL_USER"), os.environ.get("PANEL_PASS")
    if not (u and pw):
        return
    a = request.authorization
    if not (a and hmac.compare_digest(a.username, u) and hmac.compare_digest(a.password or "", pw)):
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="KILLU X HOST"'})


# ---------- pages ----------
@app.get("/")
def home():
    return send_from_directory(BASE, "index.html")


@app.get("/<page>.html")
def page(page):
    if page not in ("index", "panel", "bot"):
        return err("Not found", 404)
    return send_from_directory(BASE, page + ".html")


# ---------- API ----------
@app.get("/api/bots")
def list_bots():
    out = []
    with LOCK:
        for b in STATE:
            e = PROCS.get(b["id"])
            cpu, ram = stats(e) if e else (0, 0)
            out.append(dict(id=b["id"], name=b["name"], lang=b["lang"], on=e is not None,
                            status=e["state"] if e else "stopped", cpu=cpu, ram=ram,
                            up=int(time.time() - e["start"]) if e and e.get("p") else 0, logs=tail(b)))
    return jsonify(out)


def extract_zip(src, root, strip_top=True):
    root, count = Path(root).resolve(), 0
    with zipfile.ZipFile(src) as z:
        names = [n for n in z.namelist() if not n.endswith("/") and not n.startswith("__MACOSX")]
        strip = strip_top and len({n.split("/")[0] for n in names}) == 1 and all("/" in n for n in names)
        for n in names:
            rel = "/".join(n.split("/")[1:]) if strip else n
            t = (root / rel).resolve()
            if root not in t.parents:
                continue
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(z.read(n))
            count += 1
    return count


def save_files(d, files):
    root = d.resolve()
    for f in files:
        fn = f.filename or ""
        if not fn:
            continue
        if fn.lower().endswith(".zip"):
            extract_zip(f.stream, root)
        else:
            f.save(root / Path(fn).name)


@app.post("/api/bots")
def create():
    name, lang = request.form.get("name", "").strip()[:40], request.form.get("lang", "py")
    if not name or lang not in ("py", "js"):
        return err("Give the bot a name and a language.")
    env = {}
    for line in request.form.get("env", "").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    files = [f for f in request.files.getlist("files") if f.filename]
    with LOCK:
        b = dict(id=max([x["id"] for x in STATE], default=0) + 1, name=name, lang=lang,
                 cmd=request.form.get("cmd", "").strip(), env=env, want=False)
    bdir(b).mkdir(parents=True)
    try:
        save_files(bdir(b), files)
    except Exception as e:
        shutil.rmtree(bdir(b), ignore_errors=True)
        return err(f"Could not read the upload: {e}")
    repo = request.form.get("repo", "").strip()
    if repo:
        try:
            r = subprocess.run(["git", "clone", "--depth", "1", repo, "."], cwd=bdir(b),
                               capture_output=True, text=True, timeout=180)
            if r.returncode:
                raise RuntimeError(r.stderr.strip()[-300:])
        except Exception as e:
            shutil.rmtree(bdir(b), ignore_errors=True)
            return err(f"git clone failed: {e} (is git installed? pkg install git)")
    with LOCK:
        STATE.append(b)
        save()
    slog(b, f"created from {'GitHub' if repo else str(len(files)) + ' upload(s)'}")
    if files or repo:
        start_bot(b)
    return jsonify(id=b["id"])


@app.post("/api/bots/<int:i>/<action>")
def action(i, action):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    if action == "start":
        start_bot(b)
    elif action == "stop":
        halt(b)
    elif action == "restart":
        halt(b, quiet=True)
        slog(b, "restarting…")
        start_bot(b)
    else:
        return err("Unknown action")
    return jsonify(ok=True)


@app.delete("/api/bots/<int:i>")
def delete(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    halt(b, quiet=True)
    with LOCK:
        STATE.remove(b)
        save()
    shutil.rmtree(bdir(b), ignore_errors=True)
    shutil.rmtree(DATA / "backups" / str(i), ignore_errors=True)
    return jsonify(ok=True)


# ---------- single bot: details, settings, files, terminal ----------
def safe(b, rel):
    root = bdir(b).resolve()
    t = (root / (rel or "")).resolve()
    if t != root and root not in t.parents:
        raise ValueError("Path not allowed")
    return root, t


def body():
    return request.get_json(force=True, silent=True) or {}


@app.get("/api/bots/<int:i>")
def detail(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    e = PROCS.get(i)
    cpu, ram = stats(e) if e else (0, 0)
    return jsonify(id=i, name=b["name"], lang=b["lang"], cmd=b.get("cmd", ""), env=b.get("env", {}),
                   description=b.get("description", ""), restart_hours=b.get("restart_hours", 0),
                   events=list(reversed(b.get("events", [])))[:15],
                   auto=b.get("auto", True), on=e is not None, status=e["state"] if e else "stopped",
                   cpu=cpu, ram=ram, up=int(time.time() - e["start"]) if e and e.get("p") else 0,
                   detected=detect_cmd(b["lang"], bdir(b)), logs=tail(b, 300))


@app.put("/api/bots/<int:i>")
def update(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    d = body()
    with LOCK:
        if str(d.get("name", "")).strip():
            b["name"] = str(d["name"]).strip()[:40]
        if "cmd" in d:
            b["cmd"] = str(d["cmd"]).strip()
        if "restart_hours" in d:
            try:
                b["restart_hours"] = max(0.0, min(float(d["restart_hours"]), 720.0))
            except (TypeError, ValueError):
                pass
        if "description" in d:
            b["description"] = str(d["description"]).strip()[:200]
        if "auto" in d:
            b["auto"] = bool(d["auto"])
        if isinstance(d.get("env"), dict):
            b["env"] = {str(k).strip(): str(v) for k, v in d["env"].items() if str(k).strip()}
        save()
    return jsonify(ok=True)


@app.post("/api/bots/<int:i>/clear")
def clear_logs(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    open(bdir(b) / "bot.log", "wb").close()
    return jsonify(ok=True)


@app.get("/api/bots/<int:i>/files")
def ls(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    try:
        root, t = safe(b, request.args.get("path", ""))
    except ValueError as e:
        return err(str(e))
    if not t.is_dir():
        return err("Not a folder")
    items = []
    for p in sorted(t.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        s = p.stat()
        items.append(dict(name=p.name, dir=p.is_dir(), size=0 if p.is_dir() else s.st_size, mtime=int(s.st_mtime)))
    return jsonify(path="" if t == root else str(t.relative_to(root)), items=items)


@app.get("/api/bots/<int:i>/file")
def read_file(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    try:
        root, t = safe(b, request.args.get("path", ""))
    except ValueError as e:
        return err(str(e))
    if not t.is_file():
        return err("File not found", 404)
    if t.stat().st_size > 1_000_000:
        return err("File is over 1 MB. Download it instead.")
    data = t.read_bytes()
    if b"\0" in data[:4000]:
        return err("Binary file. Download it instead.")
    return jsonify(content=data.decode("utf-8", "replace"))


@app.put("/api/bots/<int:i>/file")
def write_file(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    d = body()
    try:
        root, t = safe(b, d.get("path", ""))
        if t == root:
            return err("Give the file a name")
        if t == root / "bot.log":
            return err("bot.log is managed by the panel")
        t.parent.mkdir(parents=True, exist_ok=True)
        with open(t, "w", encoding="utf-8", newline="") as h:
            h.write(str(d.get("content", "")))
    except (ValueError, OSError) as e:
        return err(str(e))
    return jsonify(ok=True)


@app.delete("/api/bots/<int:i>/file")
def delete_file(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    try:
        root, t = safe(b, request.args.get("path", ""))
        if t == root:
            return err("Can't delete the bot folder here")
        if t == root / "bot.log":
            return err("bot.log is managed by the panel. Use Clear in the Logs tab.")
        shutil.rmtree(t) if t.is_dir() else t.unlink()
    except (ValueError, OSError) as e:
        return err(str(e))
    return jsonify(ok=True)


@app.post("/api/bots/<int:i>/mkdir")
def mkdir(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    try:
        root, t = safe(b, body().get("path", ""))
        t.mkdir(parents=True, exist_ok=True)
    except (ValueError, OSError) as e:
        return err(str(e))
    return jsonify(ok=True)


@app.post("/api/bots/<int:i>/upload")
def upload(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    n, extract = 0, request.form.get("extract") == "1"
    try:
        root, t = safe(b, request.form.get("path", ""))
        t.mkdir(parents=True, exist_ok=True)
        for f in request.files.getlist("files"):
            if not f.filename:
                continue
            if extract and f.filename.lower().endswith(".zip"):
                n += extract_zip(f.stream, t)
            else:
                name = Path(f.filename).name
                if t == root and name == "bot.log":
                    continue
                f.save(t / name)
                n += 1
    except (ValueError, OSError, zipfile.BadZipFile) as e:
        return err(str(e))
    return jsonify(count=n)


@app.get("/api/bots/<int:i>/download")
def download(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    try:
        root, t = safe(b, request.args.get("path", ""))
    except ValueError as e:
        return err(str(e))
    if not t.is_file():
        return err("File not found", 404)
    return send_from_directory(t.parent, t.name, as_attachment=True)


def _kill(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


@app.post("/api/bots/<int:i>/exec")
def run_cmd(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    d = body()
    cmd = str(d.get("cmd", "")).strip()
    if not cmd:
        return err("Type a command")
    try:
        root, cwd = safe(b, d.get("cwd", ""))
    except ValueError as e:
        return err(str(e))
    if not cwd.is_dir():
        cwd = root
    env = {**os.environ, **b.get("env", {}), "PYTHONUNBUFFERED": "1", "TERM": "dumb"}
    env.pop("PANEL_USER", None)
    env.pop("PANEL_PASS", None)
    script = cmd + '\n__rc=$?\nprintf "\\n\\000PWD:%s\\n" "$(pwd)"\nexit $__rc'

    def gen():
        p = subprocess.Popen(script, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        timer = threading.Timer(900, _kill, [p])
        timer.start()
        buf = b""
        try:
            fd = p.stdout.fileno()
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                buf += chunk
                k = buf.find(b"\0")
                if k == -1:
                    yield buf
                    buf = b""
                else:
                    if k:
                        yield buf[:k]
                    buf = buf[k:]
            p.wait()
            cwd_line = b""
            if buf.startswith(b"\0PWD:"):
                try:
                    pth = Path(buf[5:].decode("utf-8", "replace").strip()).resolve()
                    rel = "" if pth == root else str(pth.relative_to(root))
                except Exception:
                    rel = ""
                cwd_line = b"\0CWD:" + rel.encode() + b"\n"
            yield cwd_line + b"\0EXIT:%d\n" % p.returncode
        finally:
            timer.cancel()
            if p.poll() is None:
                _kill(p)

    return Response(gen(), mimetype="text/plain", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/bots/<int:i>/unzip")
def unzip(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    try:
        root, t = safe(b, body().get("path", ""))
        n = extract_zip(t, t.parent)
    except (ValueError, OSError, zipfile.BadZipFile) as e:
        return err(f"Could not extract: {e}")
    return jsonify(count=n)


@app.post("/api/bots/<int:i>/rename")
def rename(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    d = body()
    try:
        root, s = safe(b, d.get("from", ""))
        _, t = safe(b, d.get("to", ""))
        if s == root or t == root or not s.exists():
            return err("Nothing to rename")
        if s == root / "bot.log":
            return err("bot.log is managed by the panel")
        if t.exists():
            return err("That name already exists")
        t.parent.mkdir(parents=True, exist_ok=True)
        s.rename(t)
    except (ValueError, OSError) as e:
        return err(str(e))
    return jsonify(ok=True)


@app.get("/api/bots/<int:i>/export")
def export(i):
    import io
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    root, buf = bdir(b), io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in root.rglob("*"):
            rel = p.relative_to(root)
            if p.is_file() and rel.name != "bot.log" and not {"node_modules", "__pycache__", ".git"} & set(rel.parts):
                z.write(p, rel.as_posix())
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=re.sub(r"[^\w.-]+", "_", b["name"]) + ".zip")


# ---------- backups ----------
BKNAME = re.compile(r"^[\w.-]+\.zip$")


def bk_dir(b):
    d = DATA / "backups" / str(b["id"])
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.get("/api/bots/<int:i>/backups")
def bk_list(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    items = [dict(name=p.name, size=p.stat().st_size, mtime=int(p.stat().st_mtime)) for p in bk_dir(b).glob("*.zip")]
    return jsonify(sorted(items, key=lambda x: -x["mtime"]))


@app.post("/api/bots/<int:i>/backups")
def bk_create(i):
    b = find(i)
    if not b:
        return err("Bot not found", 404)
    d, root = bk_dir(b), bdir(b)
    name = time.strftime("%Y-%m-%d_%H-%M-%S") + ".zip"
    with zipfile.ZipFile(d / name, "w", zipfile.ZIP_DEFLATED) as z:
        for p in root.rglob("*"):
            rel = p.relative_to(root)
            if p.is_file() and rel.name != "bot.log" and not {"node_modules", "__pycache__", ".git"} & set(rel.parts):
                z.write(p, rel.as_posix())
    for old in sorted(d.glob("*.zip"), key=lambda p: p.stat().st_mtime)[:-20]:
        old.unlink()
    return jsonify(name=name)


def bk_path(i, name):
    b = find(i)
    if not b or not BKNAME.match(name):
        return None
    p = bk_dir(b) / name
    return p if p.is_file() else None


@app.get("/api/bots/<int:i>/backups/<name>")
def bk_download(i, name):
    p = bk_path(i, name)
    if not p:
        return err("Backup not found", 404)
    return send_from_directory(p.parent, p.name, as_attachment=True)


@app.post("/api/bots/<int:i>/backups/<name>/restore")
def bk_restore(i, name):
    p = bk_path(i, name)
    if not p:
        return err("Backup not found", 404)
    try:
        n = extract_zip(p, bdir(find(i)), strip_top=False)
    except (OSError, zipfile.BadZipFile) as e:
        return err(f"Could not restore: {e}")
    return jsonify(count=n)


@app.delete("/api/bots/<int:i>/backups/<name>")
def bk_delete(i, name):
    p = bk_path(i, name)
    if not p:
        return err("Backup not found", 404)
    p.unlink()
    return jsonify(ok=True)


# ---------- server info, alerts, backup of everything ----------
@app.get("/api/server")
def server_info():
    du, mem = shutil.disk_usage(DATA), None
    try:
        m = {l.split(":")[0]: int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines() if ":" in l}
        mem = dict(total=m["MemTotal"] * 1024, used=(m["MemTotal"] - m.get("MemAvailable", m["MemFree"])) * 1024)
    except Exception:
        pass
    with LOCK:
        total, online = len(STATE), len(PROCS)
    return jsonify(disk=dict(total=du.total, used=du.used), mem=mem, uptime=int(time.time() - BOOT),
                   bots=total, online=online, python=sys.version.split()[0])


@app.get("/api/settings")
def get_set():
    s = get_settings()
    return jsonify(discord_set=bool(s.get("discord")), tg_set=bool(s.get("tg_token")), tg_chat=s.get("tg_chat", ""))


@app.put("/api/settings")
def put_set():
    d, s = body(), get_settings()
    if d.get("clear"):
        s = {}
    else:
        if "discord" in d:
            v = str(d["discord"]).strip()
            if v and not DISCORD_RE.match(v):
                return err("That doesn't look like a Discord webhook link (https://discord.com/api/webhooks/...)")
            s["discord"] = v
        if "tg_token" in d:
            v = str(d["tg_token"]).strip()
            if v and not re.match(r"^\d+:[\w-]+$", v):
                return err("That doesn't look like a Telegram bot token (123456:ABC...)")
            s["tg_token"] = v
        if "tg_chat" in d:
            v = str(d["tg_chat"]).strip()
            if v and not re.match(r"^(-?\d+|@\w+)$", v):
                return err("Telegram chat ID should be a number like 123456789")
            s["tg_chat"] = v
    SETTINGS.write_text(json.dumps(s))
    return jsonify(ok=True)


@app.post("/api/settings/test")
def test_alert():
    e = send_alert("\u2705 KILLU X HOST: test alert. Alerts are working.")
    return jsonify(error=e) if e else jsonify(ok=True)


@app.get("/api/backup-all")
def backup_all():
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for b in list(STATE):
            root, pre = bdir(b), re.sub(r"[^\w.-]+", "_", b["name"]) + f"_{b['id']}"
            z.writestr(pre + "/killu-project.json", json.dumps({k: b.get(k) for k in ("name", "lang", "cmd", "description", "auto", "restart_hours")}, indent=1))
            for p in root.rglob("*"):
                rel = p.relative_to(root)
                if p.is_file() and rel.name != "bot.log" and not {"node_modules", "__pycache__", ".git"} & set(rel.parts):
                    z.write(p, f"{pre}/{rel.as_posix()}")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="killu-all-projects-" + time.strftime("%Y-%m-%d") + ".zip")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    # Hosts like Render set PORT. Listen on all interfaces there so the platform can reach the app.
    host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") or os.environ.get("RENDER") else "127.0.0.1")
    if host not in ("127.0.0.1", "localhost") and not os.environ.get("PANEL_PASS"):
        import secrets
        os.environ.setdefault("PANEL_USER", "killu")
        os.environ["PANEL_PASS"] = secrets.token_urlsafe(12)
        print("=" * 60)
        print("This panel is reachable from the internet, so it needs a password.")
        print("None was set, so one was generated for this run:")
        print(f"    username: {os.environ['PANEL_USER']}")
        print(f"    password: {os.environ['PANEL_PASS']}")
        print("Set PANEL_USER and PANEL_PASS to choose your own.")
        print("=" * 60, flush=True)
    for b in STATE:
        if b.get("want"):
            spawn(b)
    threading.Thread(target=watcher, daemon=True).start()
    print(f"KILLU X HOST running on http://{host}:{port}", flush=True)
    app.run(host=host, port=port, threaded=True)
