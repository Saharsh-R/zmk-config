#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial", "protobuf", "grpcio-tools"]
# ///
import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KEYMAP = REPO / "config" / "sofle.keymap"
CACHE = REPO / ".cache"
GH_REPO = "github.com/Saharsh-R/zmk-config"
ZMK_RAW = "https://raw.githubusercontent.com/zmkfirmware/zmk/main/app"
ZMK_API = "https://api.github.com/repos/zmkfirmware/zmk/contents/app"
MSG_RAW = "https://raw.githubusercontent.com/zmkfirmware/zmk-studio-messages/main/proto/zmk"
ZMK_VID, BOOT_VID = 0x1D50, 0x239A
HEADERS = ["keys.h", "hid_usage.h", "hid_usage_pages.h", "modifiers.h", "bt.h", "outputs.h", "rgb.h", "ext_power.h"]
PROTOS = ["studio", "core", "behaviors", "keymap", "meta"]
PARAM_HEADERS = {"bt": "bt.h", "out": "outputs.h", "rgb_ug": "rgb.h", "ext_power": "ext_power.h"}
MODS = ["LC", "LS", "LA", "LG", "RC", "RS", "RA", "RG"]
UNLOCK_HINT = "hold the outermost left thumb key (adjust layer) and press Y"


def die(msg):
    sys.exit(f"error: {msg}")


def curl(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["curl", "-sfL", "-o", str(dest), url], check=True)


def ensure_refs(refresh=False):
    if refresh:
        shutil.rmtree(CACHE, ignore_errors=True)
    gen = CACHE / "gen"
    if (gen / "studio_pb2.py").exists():
        return
    print("fetching ZMK headers and Studio protocol (one time)...")
    inc = CACHE / "include" / "dt-bindings" / "zmk"
    for h in HEADERS:
        curl(f"{ZMK_RAW}/include/dt-bindings/zmk/{h}", inc / h)
    for p in PROTOS:
        curl(f"{MSG_RAW}/{p}.proto", CACHE / "proto" / f"{p}.proto")
    listing = subprocess.run(["curl", "-sfL", f"{ZMK_API}/dts/behaviors?ref=main"], check=True,
                             capture_output=True, text=True).stdout
    for name in re.findall(r'"name":\s*"([^"]+\.dtsi)"', listing):
        curl(f"{ZMK_RAW}/dts/behaviors/{name}", CACHE / "behaviors" / name)
    from grpc_tools import protoc
    gen.mkdir(parents=True, exist_ok=True)
    rc = protoc.main(["protoc", f"-I{CACHE / 'proto'}", f"--python_out={gen}",
                      *[str(CACHE / "proto" / f"{p}.proto") for p in PROTOS]])
    if rc:
        die("protoc failed")


def load_pb():
    sys.path.insert(0, str(CACHE / "gen"))
    import behaviors_pb2, core_pb2, keymap_pb2, studio_pb2
    return studio_pb2, core_pb2, behaviors_pb2, keymap_pb2


class Studio:
    SOF, ESC, EOF = 0xAB, 0xAC, 0xAD

    def __init__(self, port):
        import serial
        self.S, self.core, self.beh, self.km = load_pb()
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.5, exclusive=True)
        except serial.SerialException as e:
            if "busy" in str(e).lower() or "lock" in str(e).lower():
                die(f"{port} is in use. Click Disconnect in ZMK Studio (or close the tab) and retry.")
            raise
        self.rid = 0
        self.notifications = []

    def _read_frame(self, deadline):
        buf, state = bytearray(), "idle"
        while time.time() < deadline:
            c = self.ser.read(1)
            if not c:
                continue
            c = c[0]
            if state == "idle":
                if c == self.SOF:
                    state, buf = "data", bytearray()
            elif state == "esc":
                buf.append(c)
                state = "data"
            elif c == self.ESC:
                state = "esc"
            elif c == self.EOF:
                return self.S.Response.FromString(bytes(buf))
            elif c == self.SOF:
                buf = bytearray()
            else:
                buf.append(c)
        return None

    def call(self, timeout=5, **kw):
        self.rid += 1
        payload = self.S.Request(request_id=self.rid, **kw).SerializeToString()
        out = bytearray([self.SOF])
        for b in payload:
            if b in (self.SOF, self.ESC, self.EOF):
                out.append(self.ESC)
            out.append(b)
        out.append(self.EOF)
        self.ser.write(out)
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self._read_frame(deadline)
            if r is None:
                break
            if r.WhichOneof("type") == "notification":
                self.notifications.append(r.notification)
            elif r.request_response.request_id == self.rid:
                return r.request_response
        die("keyboard did not answer (is it running Studio firmware?)")

    def locked(self):
        r = self.call(core=self.core.Request(get_lock_state=True))
        return r.core.get_lock_state != self.core.ZMK_STUDIO_CORE_LOCK_STATE_UNLOCKED

    def wait_unlocked(self, secs=90):
        if not self.locked():
            return
        print(f"keyboard is locked: {UNLOCK_HINT}")
        end = time.time() + secs
        while time.time() < end:
            time.sleep(1)
            if not self.locked():
                print("unlocked")
                return
        die("still locked")

    def behaviors(self):
        ids = self.call(behaviors=self.beh.Request(list_all_behaviors=True)).behaviors.list_all_behaviors.behaviors
        out = {}
        for i in ids:
            req = self.beh.Request(get_behavior_details=self.beh.GetBehaviorDetailsRequest(behavior_id=i))
            out[i] = self.call(behaviors=req).behaviors.get_behavior_details
        return out

    def keymap(self):
        return self.call(keymap=self.km.Request(get_keymap=True)).keymap.get_keymap

    def unsaved(self):
        return self.call(keymap=self.km.Request(check_unsaved_changes=True)).keymap.check_unsaved_changes

    def save(self):
        r = self.call(keymap=self.km.Request(save_changes=True)).keymap.save_changes
        if r.WhichOneof("result") != "ok":
            die(f"save failed: {r}")


def ports(vid):
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports() if p.vid == vid]


def studio_port(arg):
    if arg:
        return arg
    found = ports(ZMK_VID)
    if not found:
        die("no ZMK Studio serial port found. Is the left half plugged in by USB with Studio firmware?")
    return found[0]


def split_top(s):
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch.isspace() and depth == 0:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def eval_c(t):
    if not re.fullmatch(r"[0-9a-fA-FxX()<>|&+\-*\s]+", t):
        return None
    try:
        return int(eval(t, {"__builtins__": {}}))
    except Exception:
        return None


def cpp_eval(exprs, defines=()):
    src = "".join(f"#include <dt-bindings/zmk/{h}>\n" for h in HEADERS)
    src += "".join(d + "\n" for d in defines)
    src += "".join(f"@@{i}@@ {e}\n" for i, e in enumerate(exprs))
    out = subprocess.run(["clang", "-E", "-P", "-x", "c", "-nostdinc", "-I", str(CACHE / "include"), "-"],
                         input=src, capture_output=True, text=True, check=True).stdout
    res = {}
    for line in out.splitlines():
        m = re.match(r"\s*@@(\d+)@@\s*(.*)", line)
        if m:
            res[int(m[1])] = [eval_c(t) for t in split_top(m[2])]
    return [res.get(i) for i in range(len(exprs))]


def strip_comments(text):
    return re.sub(r"//[^\n]*|/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m[0]), text, flags=re.S)


def match_brace(clean, i):
    depth = 0
    for j in range(i, len(clean)):
        if clean[j] == "{":
            depth += 1
        elif clean[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    die("unbalanced braces")


def behavior_nodes(text):
    clean = strip_comments(text)
    nodes = {}
    for m in re.finditer(r"(\w+)\s*:\s*(\w+)\s*\{", clean):
        end = match_brace(clean, m.end() - 1)
        block = clean[m.end():end]
        cells = re.search(r"#binding-cells\s*=\s*<(\d+)>", block)
        name = re.search(r'display-name\s*=\s*"([^"]*)"', block)
        nodes[m[1]] = {"node": m[2], "cells": int(cells[1]) if cells else None,
                       "display": name[1] if name else None}
    return nodes


class Layer:
    def __init__(self, text, start, end):
        self.text, self.start, self.end = text, start, end
        self.name = re.match(r"\s*(\w+)", text)[1]
        clean = strip_comments(text)
        m = re.search(r"(?<![-\w])bindings\s*=\s*<", clean)
        self.b_start = m.end()
        self.b_end = clean.index(">", self.b_start)
        body = text[self.b_start:self.b_end]
        self.rows, self.offsets = [], []
        for line in body.split("\n"):
            starts = [mm.start() for mm in re.finditer(r"&", line)]
            if starts:
                toks = [line[s:e].strip() for s, e in zip(starts, starts[1:] + [len(line)])]
                self.rows.append(toks)
                self.offsets.append(starts)
        dn = re.search(r'display-name\s*=\s*"([^"]*)"', clean)
        self.display = dn[1] if dn else None

    @property
    def tokens(self):
        return [t for r in self.rows for t in r]

    def columns(self):
        widest = max(range(len(self.rows)), key=lambda i: len(self.rows[i]))
        cols = self.offsets[widest]
        return [[min(range(len(cols)), key=lambda c: abs(cols[c] - o)) for o in offs] for offs in self.offsets]


def render_rows(rows, colmap):
    ncol = max(c for r in colmap for c in r) + 1
    width = [0] * ncol
    for toks, cols in zip(rows, colmap):
        for t, c in zip(toks, cols):
            width[c] = max(width[c], len(t))
    lines = []
    for toks, cols in zip(rows, colmap):
        cells = [""] * ncol
        for t, c in zip(toks, cols):
            cells[c] = t
        line = "".join(cells[c].ljust(width[c]) + ("    " if c == ncol // 2 - 1 else "  ")
                       for c in range(ncol)).rstrip()
        lines.append(line)
    return "\n".join(lines)


def parse_keymap(text):
    clean = strip_comments(text)
    m = re.search(r"\bkeymap\s*\{", clean)
    kstart = m.end()
    kend = match_brace(clean, m.end() - 1)
    layers, i = [], kstart
    while True:
        n = re.compile(r"\w+\s*\{").search(clean, i, kend)
        if not n:
            break
        end = match_brace(clean, n.end() - 1)
        end = clean.index(";", end) + 1
        layers.append(Layer(text[n.start():end], n.start(), end))
        i = end
    return layers


class Sync:
    def __init__(self, text, dev_behaviors, dev_keymap):
        self.text = text
        self.layers = parse_keymap(text)
        self.defines = [l.strip() for l in text.splitlines() if re.match(r"\s*#define\s+\w+\s+\S", l)]
        self.nodes = {}
        for f in sorted((CACHE / "behaviors").glob("*.dtsi")):
            self.nodes.update(behavior_nodes(f.read_text()))
        self.nodes.update(behavior_nodes(text))
        by_display = {}
        for label, n in self.nodes.items():
            by_display.setdefault(n["display"] or n["node"], label)
            by_display.setdefault(n["node"], label)
        self.dev_beh = {}
        for bid, d in dev_behaviors.items():
            label = by_display.get(d.display_name)
            if not label:
                die(f"don't know which behavior '{d.display_name}' is; add it to the keymap or ZMK refs")
            self.dev_beh[bid] = (label, d)
        self.km = dev_keymap
        self.dev_pos = {l.id: i for i, l in enumerate(dev_keymap.layers)}
        self.reordered = [l.id for l in dev_keymap.layers if l.id < len(self.layers)] != sorted(
            l.id for l in dev_keymap.layers if l.id < len(self.layers))
        self._build_names()

    def _build_names(self):
        keys_h = (CACHE / "include/dt-bindings/zmk/keys.h").read_text()
        names = sorted(set(re.findall(r"#define\s+([A-Z][A-Z0-9_]*)\s", keys_h)))
        vals = cpp_eval(names)
        self.keynames = {}
        for n, v in zip(names, vals):
            if v and len(v) == 1 and v[0] is not None:
                self.keynames.setdefault(v[0], []).append(n)
        self.consts = {}
        for label, h in PARAM_HEADERS.items():
            hn = re.findall(r"#define\s+([A-Z][A-Z0-9_]*)\s", (CACHE / "include/dt-bindings/zmk" / h).read_text())
            ev = cpp_eval(hn)
            self.consts[label] = [(n, v) for n, v in zip(hn, ev) if v and None not in v]
        all_tokens = [t for l in self.layers for t in l.tokens]
        self.file_bindings = {}
        params = [t.split(None, 1)[1] if " " in t else "" for t in all_tokens]
        for t, v in zip(all_tokens, cpp_eval(params, self.defines)):
            self.file_bindings[t] = (t.split()[0].lstrip("&"), v or [])
        usage = Counter()
        for t in all_tokens:
            for p in split_top(t.split(None, 1)[1]) if " " in t else []:
                usage[p] += 1
        pv = cpp_eval(list(usage), self.defines)
        self.pref = {}
        for p, v in sorted(zip(usage, pv), key=lambda x: -usage[x[0]]):
            if v and len(v) == 1 and v[0] is not None:
                self.pref.setdefault(v[0], p)

    def hid(self, v):
        if v in self.pref:
            return self.pref[v]
        if v in self.keynames:
            return min(self.keynames[v], key=lambda n: (n.startswith("K_"), len(n), n))
        mods, base = v >> 24, v & 0xFFFFFF
        if not mods:
            return hex(v)
        s = self.hid(base)
        for i in range(8):
            if mods & (1 << i):
                s = f"{MODS[i]}({s})"
        return s

    def kinds(self, d, p1):
        def kind(desc):
            return desc.WhichOneof("value_type") if desc else None
        sets = list(d.metadata)
        if not sets:
            return None, None
        k1 = kind(sets[0].param1[0]) if sets[0].param1 else None
        k2 = None
        for s in sets:
            hit = any(
                (kind(x) == "constant" and x.constant == p1)
                or (kind(x) == "range" and x.range.min <= p1 <= x.range.max)
                or kind(x) in ("hid_usage", "layer_id")
                for x in s.param1)
            if hit:
                k2 = kind(s.param2[0]) if s.param2 else None
                break
        return k1, k2

    def render(self, label, d, p1, p2):
        cells = self.nodes.get(label, {}).get("cells")
        if cells is None:
            cells = 2 if p2 else (1 if p1 else 0)
        params = [p1, p2][:cells]
        if cells == 0:
            return f"&{label}"
        if label in self.consts:
            for want in (params, params[:1]):
                cands = [n for n, v in self.consts[label] if v == want]
                if cands:
                    name = min(cands, key=lambda n: (n.endswith("_CMD"), len(n)))
                    rest = [str(x) for x in params[len(want):]]
                    return " ".join([f"&{label}", name] + rest)
        k1, k2 = self.kinds(d, p1)
        out = [f"&{label}"]
        for v, k in zip(params, [k1, k2]):
            if k is None:
                k = "hid_usage" if (v >> 16) & 0xFF in (0x07, 0x0C) else "num"
            if k == "hid_usage":
                out.append(self.hid(v))
            elif k == "layer_id":
                out.append(str(self.dev_pos.get(v, v)))
            else:
                out.append(str(v))
        return " ".join(out)

    def same(self, token, label, d, p1, p2):
        flabel, fvals = self.file_bindings.get(token, (None, None))
        if flabel != label or fvals is None or None in fvals:
            return False
        dev = [p1, p2][:len(fvals)]
        if len(fvals) < 2 and p2 and self.nodes.get(label, {}).get("cells", 0) >= 2:
            return False
        k1, k2 = self.kinds(d, p1)
        for i, k in enumerate([k1, k2][:len(fvals)]):
            if k == "layer_id":
                if self.reordered:
                    return False
                dev[i] = self.dev_pos.get(dev[i], dev[i])
        return dev == fvals

    def run(self):
        new_nodes, base_cols, base_shape = [], None, None
        for li, dl in enumerate(self.km.layers):
            fl = self.layers[dl.id] if dl.id < len(self.layers) else None
            toks = fl.tokens if fl else []
            if fl and len(dl.bindings) != len(toks):
                die(f"layer {fl.name}: keyboard has {len(dl.bindings)} keys, file has {len(toks)}")
            out = []
            for k, b in enumerate(dl.bindings):
                if b.behavior_id not in self.dev_beh:
                    out.append(toks[k] if fl else "&none")
                    continue
                label, d = self.dev_beh[b.behavior_id]
                if fl and self.same(toks[k], label, d, b.param1, b.param2):
                    out.append(toks[k])
                else:
                    out.append(self.render(label, d, b.param1, b.param2))
            ref = fl or self.layers[0]
            shape = [len(r) for r in ref.rows]
            rows, i = [], 0
            for n in shape:
                rows.append(out[i:i + n])
                i += n
            if fl and rows == fl.rows:
                body_text = None
            else:
                body_text = render_rows(rows, ref.columns())
            new_nodes.append((dl, fl, body_text))
        return self.assemble(new_nodes)

    def assemble(self, new_nodes):
        def update(fl, dl, body_text):
            t = fl.text
            if body_text is not None:
                body = t[fl.b_start:fl.b_end]
                lines = body.split("\n")
                first = next(i for i, l in enumerate(lines) if "&" in l)
                last = max(i for i, l in enumerate(lines) if "&" in l)
                body = "\n".join(lines[:first] + [body_text] + lines[last + 1:])
                t = t[:fl.b_start] + body + t[fl.b_end:]
            name = dl.name
            if name and name != fl.display:
                if fl.display is not None:
                    t = re.sub(r'(display-name\s*=\s*")[^"]*(")', lambda m: m[1] + name + m[2], t, count=1)
                else:
                    t = re.sub(r"(\{\s*\n)(\s*)", lambda m: f'{m[1]}{m[2]}display-name = "{name}";\n\n{m[2]}',
                               t, count=1)
            return t

        def fresh(dl, body_text):
            name = re.sub(r"\W", "_", dl.name or f"layer_{dl.id}").lower()
            ind = " " * 8
            return (f"{name}_layer {{\n{ind}    display-name = \"{dl.name}\";\n{ind}    bindings = <\n"
                    f"{body_text}\n{ind}    >;\n{ind}}};")

        structural = [dl.id for dl, _, _ in new_nodes] != list(range(len(self.layers)))
        text = self.text
        if not structural:
            for dl, fl, body_text in reversed(new_nodes):
                text = text[:fl.start] + update(fl, dl, body_text) + text[fl.end:]
            return text, False
        parts = [update(fl, dl, b) if fl else fresh(dl, b) for dl, fl, b in new_nodes]
        start, end = self.layers[0].start, self.layers[-1].end
        return text[:start] + "\n\n        ".join(parts) + text[end:], True


def git(*args, check=True):
    return subprocess.run(["git", "-C", str(REPO), *args], check=check, capture_output=True, text=True)


def cmd_sync(a):
    ensure_refs(a.refresh)
    dev = Studio(studio_port(a.port))
    dev.wait_unlocked()
    if dev.unsaved():
        if not a.save:
            die("keyboard has unsaved Studio changes. Click Save in Studio, or rerun with --save.")
        dev.save()
        print("saved Studio changes on keyboard")
    behaviors, keymap = dev.behaviors(), dev.keymap()
    text = KEYMAP.read_text()
    new, structural = Sync(text, behaviors, keymap).run()
    if new == text:
        print("repo already matches keyboard")
        return
    KEYMAP.write_text(new)
    if structural:
        print("note: layers were added, removed or reordered in Studio. After the next flash, "
              "use Studio's 'Restore Stock Settings' so layer numbers match the file.")
    print(git("--no-pager", "diff", "--stat").stdout)
    if a.dry_run:
        print(git("--no-pager", "diff").stdout)
        KEYMAP.write_text(text)
        print("dry run: file restored")
        return
    if a.no_commit:
        return
    git("add", str(KEYMAP))
    git("commit", "-m", a.message)
    print("committed")
    if not a.no_push:
        r = git("push", check=False)
        print(r.stdout + r.stderr)


def gh_env():
    env = dict(os.environ, GH_HOST="github.com")
    if "GH_TOKEN" not in env:
        out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                             capture_output=True, text=True).stdout
        m = re.search(r"^password=(.*)$", out, re.M)
        if m:
            env["GH_TOKEN"] = m[1]
    return env


def gh(*args, capture=True):
    r = subprocess.run(["gh", *args, "-R", GH_REPO], env=gh_env(), capture_output=capture, text=True)
    if r.returncode:
        die(f"gh {' '.join(args)} failed: {r.stderr if capture else ''}")
    return r.stdout


def uf2_to_hex(src, dst):
    data = Path(src).read_bytes()
    mem = {}
    for off in range(0, len(data), 512):
        b = data[off:off + 512]
        m0, m1, flags, addr, size = struct.unpack("<5I", b[:20])
        if (m0, m1) != (0x0A324655, 0x9E5D5157):
            die(f"{src} is not a UF2 file")
        if flags & 1:
            continue
        for i in range(size):
            mem[addr + i] = b[32 + i]

    def rec(t, a, payload):
        r = bytes([len(payload), (a >> 8) & 0xFF, a & 0xFF, t]) + payload
        return ":" + r.hex().upper() + "%02X" % ((-sum(r)) & 0xFF)

    addrs, out, base, i = sorted(mem), [], None, 0
    while i < len(addrs):
        a = addrs[i]
        if a >> 16 != base:
            base = a >> 16
            out.append(rec(4, 0, struct.pack(">H", base)))
        chunk, j = bytearray([mem[a]]), i + 1
        while j < len(addrs) and len(chunk) < 16 and addrs[j] == a + len(chunk) and addrs[j] >> 16 == base:
            chunk.append(mem[addrs[j]])
            j += 1
        out.append(rec(0, a & 0xFFFF, bytes(chunk)))
        i = j
    out.append(":00000001FF")
    Path(dst).write_text("\n".join(out) + "\n")


def find_firmware(a):
    if a.file:
        return Path(a.file)
    run = a.run
    if not run:
        sha = git("rev-parse", "HEAD").stdout.strip()
        if git("branch", "-r", "--contains", sha, check=False).stdout.strip() == "":
            die("HEAD isn't pushed yet, so CI hasn't built it. Push first, or pass --run/--file.")
        out = gh("run", "list", "--commit", sha, "--json", "databaseId,status,conclusion", "-L", "1",
                 "--jq", '.[0] | "\\(.databaseId) \\(.status) \\(.conclusion)"').strip()
        if not out or out == "null null null":
            die(f"no CI run found for {sha[:7]}")
        run, status = out.split()[:2]
        if status != "completed":
            print(f"waiting for CI run {run}...")
            gh("run", "watch", run, "--exit-status", "--interval", "15", capture=False)
        concl = gh("run", "view", run, "--json", "conclusion", "--jq", ".conclusion").strip()
        if concl != "success":
            die(f"CI run {run} {concl}")
    dest = REPO / "past_builds" / f"run-{run}"
    if not dest.exists():
        gh("run", "download", str(run), "-D", str(dest))
    found = sorted(dest.rglob(f"sofle_{a.side}-*.uf2"))
    if not found:
        die(f"no sofle_{a.side} firmware in {dest}")
    return found[0]


def nrfutil():
    exe = shutil.which("adafruit-nrfutil")
    return [exe] if exe else ["uvx", "adafruit-nrfutil"]


def cmd_flash(a):
    fw = find_firmware(a)
    print(f"firmware: {fw.relative_to(REPO) if fw.is_relative_to(REPO) else fw}")
    tmp = Path(tempfile.mkdtemp())
    uf2_to_hex(fw, tmp / "fw.hex")
    subprocess.run(nrfutil() + ["dfu", "genpkg", "--dev-type", "0x0052", "--application", str(tmp / "fw.hex"),
                                str(tmp / "fw.zip")], check=True, capture_output=True)
    port = (ports(BOOT_VID) or [None])[0]
    if not port:
        print(f"plug the {a.side} half in by USB and double-tap its reset button...")
        end = time.time() + 120
        while not port and time.time() < end:
            time.sleep(0.5)
            port = (ports(BOOT_VID) or [None])[0]
        if not port:
            die("bootloader never showed up")
        time.sleep(1)
    print(f"flashing over {port}...")
    subprocess.run(nrfutil() + ["dfu", "serial", "--package", str(tmp / "fw.zip"), "-p", port, "-b", "115200",
                                "--singlebank"], check=True)
    print(f"done: {a.side} half flashed")


def main():
    p = argparse.ArgumentParser(description="Sync the Sofle keymap with ZMK Studio and flash firmware")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="read the live keymap from the keyboard into config/sofle.keymap")
    s.add_argument("--port")
    s.add_argument("--save", action="store_true", help="save unsaved Studio changes on the keyboard first")
    s.add_argument("--dry-run", action="store_true", help="show the diff, change nothing")
    s.add_argument("--no-commit", action="store_true")
    s.add_argument("--no-push", action="store_true")
    s.add_argument("-m", "--message", default="Sync keymap from ZMK Studio")
    s.add_argument("--refresh", action="store_true", help="re-download ZMK headers and protocol files")
    s.set_defaults(fn=cmd_sync)
    f = sub.add_parser("flash", help="flash CI firmware over serial (no USB drive needed)")
    f.add_argument("side", choices=["left", "right"])
    f.add_argument("--run", help="GitHub Actions run id (default: the run for HEAD)")
    f.add_argument("--file", help="flash a local .uf2 instead")
    f.set_defaults(fn=cmd_flash)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
