#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dead Space (2023) - Extractor completo de audio con nombres reales
===================================================================
Basado en el formato que ya usa tu toc_ds2023.cs (layout.toc + *.toc + cas).

Que hace:
  1. Lee layout.toc y todos los .toc de superbundles (incluye idiomas: es, en, ...).
  2. Indexa EBX / RES / CHUNKS con su ubicacion en los .cas (zstd / oodle / sin comprimir).
  3. Para cada NewWaveResource (nombre real = nombre del asset en el TOC) busca los
     GUID de chunks dentro de su payload y extrae esos chunks con el nombre real.
  4. Videologs (MovieTexture2Asset): extrae el .webm y, si hay ffmpeg, el audio.
  5. Opcional: chunks de audio "huerfanos" (nadie los referencia) detectados por firma.
  6. Genera manifest.csv con nombre, idioma, GUIDs, tamanos y ruta de salida.

Dependencias:   pip install customtkinter zstandard
Oodle:          oo2core_8_win64.dll (se busca en la carpeta del juego o junto al script)
Opcional:       ffmpeg en el PATH (para convertir webm -> opus/wav)

Uso GUI:  python ds2023_audio_tool.py
Uso CLI:  python ds2023_audio_tool.py --cli --game "D:/.../Dead Space (2023)" --out salida
                [--cache Caches/deadspace.cache] [--lang es] [--filter texto] [--orphans]
                [--videologs] [--analyze NOMBRE]
"""
import argparse
import csv
import ctypes
import os
import re
import struct
import sys
import threading
import shutil
import subprocess
import time
from collections import OrderedDict, defaultdict

# ----------------------------------------------------------------------------
# Utilidades basicas
# ----------------------------------------------------------------------------

def res_type_hash(name):
    """Hash de tipo de res (igual que en toc_ds2023.cs)."""
    h = 5381
    for ch in name.lower():
        h = ((h * 33) & 0xFFFFFFFF) ^ ord(ch)
    return h


NEWWAVE_HASH = res_type_hash("NewWaveResource")


class Buf:
    """Lector sobre bytes. Metodos *be = big endian (como BinaryReaderBE)."""

    def __init__(self, data):
        self.d = data
        self.p = 0

    def seek(self, p):
        self.p = p

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def i16be(self):
        v = struct.unpack_from(">h", self.d, self.p)[0]
        self.p += 2
        return v

    def i32be(self):
        v = struct.unpack_from(">i", self.d, self.p)[0]
        self.p += 4
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def u64(self):
        v = struct.unpack_from("<Q", self.d, self.p)[0]
        self.p += 8
        return v

    def raw(self, n):
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def cstr(self):
        e = self.d.index(b"\0", self.p)
        s = self.d[self.p:e].decode("latin-1")
        self.p = e + 1
        return s

    def varint(self):
        vb = self.u8(); val = vb & 0x7F; out = val
        if vb > 0x7F:
            vb = self.u8(); out += (vb & 0x7F) << 7
        if vb > 0x7F:
            vb = self.u8(); out += (vb & 0x7F) << 14
        if vb > 0x7F:
            vb = self.u8(); out += (vb & 0x7F) << 21
        return out

    def eof(self):
        return self.p >= len(self.d)


def ms_swap(b):
    """GUID estilo Microsoft: intercambia endianness de los 3 primeros grupos."""
    return b[3::-1] + b[5:3:-1] + b[7:5:-1] + b[8:]


def safe_path(name, maxseg=110):
    parts = []
    for seg in re.split(r"[\\/]+", name):
        seg = re.sub(r'[<>:"|?*\x00-\x1f]', "_", seg).strip(" .")
        if seg:
            parts.append(seg[:maxseg])
    return os.path.join(*parts) if parts else "sin_nombre"


# ----------------------------------------------------------------------------
# Descompresion
# ----------------------------------------------------------------------------

class Decomp:
    def __init__(self, extra_dirs=()):
        self.oodle = None
        self.zstd = None
        self._extra = list(extra_dirs)

    def _load_oodle(self):
        if self.oodle is not None:
            return self.oodle
        cands = []
        for d in self._extra + [os.path.dirname(os.path.abspath(__file__)), os.getcwd()]:
            cands.append(os.path.join(d, "oo2core_8_win64.dll"))
            cands.append(os.path.join(d, "Data", "oo2core_8_win64.dll"))
        cands.append("oo2core_8_win64.dll")
        for c in cands:
            try:
                dll = ctypes.WinDLL(c) if os.name == "nt" else ctypes.CDLL(c)
                fn = dll.OodleLZ_Decompress
                fn.restype = ctypes.c_int
                fn.argtypes = [ctypes.c_char_p, ctypes.c_longlong, ctypes.c_char_p, ctypes.c_longlong,
                               ctypes.c_uint, ctypes.c_uint, ctypes.c_ulonglong, ctypes.c_uint,
                               ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                               ctypes.c_uint, ctypes.c_uint]
                self.oodle = fn
                return fn
            except Exception:
                continue
        raise RuntimeError("No se pudo cargar oo2core_8_win64.dll (copiala junto al script o a la carpeta del juego).")

    def _load_zstd(self):
        if self.zstd is None:
            import zstandard
            self.zstd = zstandard.ZstdDecompressor()
        return self.zstd

    def block(self, comptype, cmp, usize):
        if comptype == 0:
            return cmp
        if comptype == 0x11:
            fn = self._load_oodle()
            out = ctypes.create_string_buffer(usize + 64)
            fn(cmp, len(cmp), out, usize, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3)
            return out.raw[:usize]
        if comptype == 0x0F:
            z = self._load_zstd()
            return z.decompressobj().decompress(cmp)
        raise ValueError("Compresion no soportada: 0x%X" % comptype)


# ----------------------------------------------------------------------------
# Modelo
# ----------------------------------------------------------------------------

class Loc:
    __slots__ = ("cas", "off", "size")

    def __init__(self, cas, off, size):
        self.cas = cas
        self.off = off
        self.size = size


class Entry:
    __slots__ = ("kind", "name", "rtype", "loc", "toc", "lang", "bundle", "size1", "key", "rhash")

    def __init__(self, kind, name, loc, toc, lang, bundle=-1, rtype="", size1=0, key="", rhash=0):
        self.kind = kind
        self.name = name
        self.loc = loc
        self.toc = toc
        self.lang = lang
        self.bundle = bundle
        self.rtype = rtype
        self.size1 = size1
        self.key = key
        self.rhash = rhash


def lang_from_toc(rel):
    """win32/loctext/es.toc -> 'es'; resto -> 'base'."""
    low = rel.replace("\\", "/").lower()
    m = re.search(r"loctext/([a-z]{2,3})(?:\.toc|/)", low)
    if m:
        return m.group(1)
    m = re.search(r"/(en|es|fr|de|it|pl|cn|tw|ru|pt|ja|ko|br|mx|esmx)\.toc$", low)
    if m:
        return m.group(1)
    return "base"


# ----------------------------------------------------------------------------
# Indexador de TOC (port de toc_ds2023.cs)
# ----------------------------------------------------------------------------

class GameIndex:
    def __init__(self, game_dir, log=print, progress=None, stop=None):
        self.root = os.path.abspath(game_dir)
        self.data = self._find_data(self.root)
        self.log = log
        self.progress = progress or (lambda a, b: None)
        self.stop = stop or (lambda: False)
        self.bundles = {}              # persistentIndex -> installBundle
        self.entries = []              # todos los Entry
        self.chunks = defaultdict(list)    # key -> [Entry]
        self.named = []                # ebx + res
        self.decomp = Decomp([self.data, self.root, os.path.join(self.root, "Bin")])
        self._fh = {}
        self.casmiss = set()

    @staticmethod
    def _find_data(root):
        for c in (root, os.path.join(root, "Data"), os.path.join(root, "data")):
            if os.path.isfile(os.path.join(c, "layout.toc")):
                return c
        raise FileNotFoundError("No encuentro layout.toc en '%s' (ni en Data/)." % root)

    # --- layout.toc --------------------------------------------------------
    def read_layout(self):
        data = open(os.path.join(self.data, "layout.toc"), "rb").read()
        r = Buf(data)
        r.seek(0x22C)
        bundle = ""
        while not r.eof():
            code = r.u8()
            nameflag = code >> 7
            typ = code & 0x7F
            objname = ""
            if nameflag == 0 and code != 0:
                objname = r.cstr()
            if typ == 0:
                pass
            elif typ in (1, 2):
                r.varint()
            elif typ == 6:
                r.u8()
            elif typ == 7:
                n = r.varint()
                s = r.raw(n)[:-1].decode("latin-1")
                if objname == "installBundle":
                    bundle = s
            elif typ == 8:
                v = r.i32()
                if objname == "persistentIndex":
                    self.bundles[v] = bundle
            elif typ in (9, 0xC):
                r.raw(8)
            elif typ == 0xA:
                r.varint()
            elif typ == 0xB:
                r.raw(4)
            elif typ == 0xF:
                r.raw(16)
            elif typ == 0x10:
                r.raw(20)
            elif typ in (0x13, 0x14):
                r.raw(r.varint())
            else:
                self.log("layout.toc: tipo desconocido %d" % typ)
                break
        self.log("layout.toc: %d install bundles" % len(self.bundles))

    def cas_path(self, bun, cas):
        b = self.bundles.get(bun, "")
        rel = "%s/cas_%02d.cas" % (b, cas)
        cands = [os.path.join(self.data, rel), os.path.join(self.root, rel)]
        if rel.lower().startswith("native_data/"):
            cands.append(os.path.join(self.data, rel[12:]))
        for c in cands:
            if os.path.isfile(c):
                return c
        self.casmiss.add(rel)
        return cands[0]

    # --- un .toc -------------------------------------------------------------
    def _cas_ref(self, r, flag, st):
        if flag > 0:
            r.u8()
            bun = r.i16be()
            cas = r.u8()
            st["cas"] = self.cas_path(bun, cas)
            st["bun"] = bun
        off = r.i32be()
        size = r.i32be()
        return Loc(st["cas"], off, size)

    def parse_toc(self, path):
        rel = os.path.relpath(path, self.data)
        lang = lang_from_toc(rel)
        data = open(path, "rb").read()
        r = Buf(data)
        r.seek(0x22C)
        t1off1 = r.i32be(); t1off2 = r.i32be(); n1 = r.i32be()
        t2off1 = r.i32be(); t2off2 = r.i32be(); n2 = r.i32be(); t2off3 = r.i32be(); r.i32be()
        t3off = r.i32be(); r.i32be(); r.i32be(); r.i32be(); n3 = r.i32be()
        n4 = r.i32be(); t4off = r.i32be()
        st = {"cas": "", "bun": 0}
        cnt = [0, 0, 0]

        for i in range(n1):
            if self.stop():
                return
            r.seek(0x22C + t1off2 + i * 16)
            r.i32be(); r.i32be(); r.i32be()
            blockoff = r.i32be()
            r.seek(0x22C + blockoff)
            bn1 = r.i32be(); bn2 = r.i32be(); flagoff = r.i32be(); bn3 = r.i32be(); partsoff = r.i32be()
            r.seek(0x22C + blockoff + flagoff)
            flags = [r.u8() for _ in range(bn3)]
            r.seek(0x22C + blockoff + partsoff)
            k = 0
            hloc = self._cas_ref(r, flags[k], st); k += 1
            try:
                f = self._open(hloc.cas)
                f.seek(hloc.off)
                hsize = struct.unpack(">i", f.read(4))[0]
                if hsize + 4 != hloc.size:
                    self.log("  [!] tamano de cabecera inesperado en %s (bundle %d)" % (rel, i))
                    continue
                head = f.read(hsize)
            except Exception as ex:
                self.log("  [!] cabecera de bundle ilegible en %s: %s" % (rel, ex))
                continue
            h = Buf(head)
            if h.u32() != 0x9D798ED6:
                self.log("  [!] magic de cabecera invalido en %s (bundle %d)" % (rel, i))
                continue
            base = 0
            h.seek(base + 4)
            nfiles = h.i32(); nebx = h.i32(); nres = h.i32(); nchunks = h.i32()
            names_off = h.i32(); meta_off = h.i32(); meta_size = h.i32()
            h.p += nfiles * 0x14
            nameoff = []; fsize = []
            for _ in range(nebx + nres):
                nameoff.append(h.i32()); fsize.append(h.i32())
            rhash = [h.u32() for _ in range(nres)]
            for _ in range(nres):
                h.i32(); h.p += 12
            for _ in range(nres):
                h.u64()
            hpos = h.p

            def nm(idx):
                h.seek(base + names_off + nameoff[idx])
                return h.cstr()

            for j in range(nebx):
                name = nm(j)
                loc = self._cas_ref(r, flags[k], st); k += 1
                e = Entry("ebx", name, loc, rel, lang, i)
                self.entries.append(e); self.named.append(e); cnt[0] += 1
            for j in range(nres):
                name = nm(nebx + j)
                loc = self._cas_ref(r, flags[k], st); k += 1
                e = Entry("res", name, loc, rel, lang, i, rhash[j] and ("%08X" % rhash[j]), rhash=rhash[j])
                self.entries.append(e); self.named.append(e); cnt[1] += 1
            h.seek(hpos)
            for j in range(nchunks):
                guid = h.raw(16)
                s1 = h.i32(); s2 = h.i32()
                loc = self._cas_ref(r, flags[k], st); k += 1
                key = guid.hex().upper()
                if s1 == 0:
                    e = Entry("chunk", key, loc, rel, lang, i, size1=s1, key=key)
                    self.entries.append(e); self.chunks[key].append(e); cnt[2] += 1

        # chunks "grandes" (tabla global del toc)
        ids = []
        r.seek(0x22C + t2off2)
        for _ in range(n2):
            raw = r.raw(16)
            off = r.i32be() & 0xFFFFFF
            ids.append((raw[::-1].hex().upper(), off))
        for key, off in ids:
            r.seek(0x22C + t2off3 + off * 4)
            loc = self._cas_ref(r, 1, st)
            e = Entry("chunk", key, loc, rel, lang, -1, size1=0, key=key)
            self.entries.append(e); self.chunks[key].append(e); cnt[2] += 1
        self.log("  %-40s ebx=%d res=%d chunks=%d  [%s]" % (rel, cnt[0], cnt[1], cnt[2], lang))

    def build(self):
        self.read_layout()
        tocs = []
        for dp, _dn, fn in os.walk(self.data):
            for f in fn:
                if f.lower().endswith(".toc") and f.lower() != "layout.toc":
                    tocs.append(os.path.join(dp, f))
        tocs.sort()
        self.log("Superbundles .toc encontrados: %d" % len(tocs))
        for n, t in enumerate(tocs):
            if self.stop():
                break
            self.progress(n, len(tocs))
            try:
                self.parse_toc(t)
            except Exception as ex:
                self.log("  [!] error en %s: %s" % (os.path.relpath(t, self.data), ex))
        self.progress(len(tocs), len(tocs))
        if self.casmiss:
            self.log("Aviso: %d .cas no encontrados (p.ej. %s)" % (len(self.casmiss), sorted(self.casmiss)[0]))
        self.log("Indice: %d nombrados, %d chunks unicos" % (len(self.named), len(self.chunks)))

    # --- lectura de assets ---------------------------------------------------
    def _open(self, path):
        f = self._fh.get(path)
        if f is None:
            if len(self._fh) > 24:
                for v in self._fh.values():
                    v.close()
                self._fh.clear()
            f = open(path, "rb")
            self._fh[path] = f
        return f

    def read_loc(self, loc, limit=None):
        f = self._open(loc.cas)
        f.seek(loc.off)
        end = loc.off + loc.size
        out = bytearray()
        while f.tell() < end:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            usize, p = struct.unpack(">ii", hdr)
            comp = (p >> 24) & 0xFF
            psize = p & 0xFFFFF
            cmp = f.read(psize)
            out += self.decomp.block(comp, cmp, usize)
            if limit and len(out) >= limit:
                break
        return bytes(out)

    def close(self):
        for v in self._fh.values():
            v.close()
        self._fh.clear()


# ----------------------------------------------------------------------------
# Busqueda de GUIDs de chunk en payloads
# ----------------------------------------------------------------------------

class GuidFinder:
    def __init__(self, chunk_keys):
        self.map = {}
        self.pref = set()
        for key in chunk_keys:
            k = bytes.fromhex(key)
            for v in (k, ms_swap(k), k[::-1], ms_swap(k[::-1])):
                self.map.setdefault(v, key)
                self.pref.add(v[:4])

    def find(self, payload):
        hits = OrderedDict()
        pref = self.pref
        mp = self.map
        for off in range(0, len(payload) - 15):
            if payload[off:off + 4] in pref:
                key = mp.get(payload[off:off + 16])
                if key and key not in hits:
                    hits[key] = off
        return list(hits.items())


def sniff_ext(b):
    h = b[:64]
    if h[:4] == b"OggS":
        return ".opus" if b"OpusHead" in h else ".ogg"
    if h[:4] == b"RIFF":
        return ".wav"
    if h[:4] == b"\x1aE\xdf\xa3":
        return ".webm"
    if h[:4] == b"fLaC":
        return ".flac"
    if h[:4] == b"SCHl":
        return ".schl"
    if h[:3] == b"ID3" or (len(h) > 1 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0):
        return ".mp3"
    if h[4:8] == b"ftyp":
        return ".mp4"
    return ".bin"



# ----------------------------------------------------------------------------
# Formato EA Opus crudo (chunks de audio de Dead Space 2023)
#   unidades:  [tipo u8][tamano u24 BE, incluye estas 4 bytes][payload]
#   0x48 = cabecera de segmento: [flags u8][sample_rate u24 BE][0x40|nmuestras u24... (u32 BE)]
#   0x44 = frame:  [muestras validas u32 BE][paquete Opus]   (el 1.º de cada segmento: 648 = 960 - 312 de pre-skip)
#   0x45 = fin de segmento
# ----------------------------------------------------------------------------

def _opus_pkt_samples(pkt):
    if not pkt:
        return 0
    toc = pkt[0]
    cfg = toc >> 3
    if cfg < 12:
        dur = (480, 960, 1920, 2880)[cfg & 3]
    elif cfg < 16:
        dur = (480, 960)[cfg & 1]
    else:
        dur = (120, 240, 480, 960)[cfg & 3]
    code = toc & 3
    if code == 0:
        n = 1
    elif code in (1, 2):
        n = 2
    else:
        n = (pkt[1] & 0x3F) if len(pkt) > 1 else 1
    return dur * n


def parse_ea_opus(data):
    """Devuelve lista de segmentos [{'rate','total','frames':[(muestras, paquete)]}] o None si no es este formato."""
    if len(data) < 16 or data[0] != 0x48:
        return None
    segs = []
    p = 0
    cur = None
    n = len(data)
    while p + 4 <= n:
        t = data[p]
        sz = int.from_bytes(data[p + 1:p + 4], "big")
        if sz < 4 or p + sz > n:
            break
        pl = data[p + 4:p + sz]
        if t == 0x48:
            if len(pl) < 8:
                return None
            cur = {"rate": int.from_bytes(pl[1:4], "big"), "total": int.from_bytes(pl[5:8], "big"), "frames": []}
            segs.append(cur)
        elif t == 0x44:
            if cur is None or len(pl) < 5:
                return None
            cur["frames"].append((struct.unpack(">I", pl[:4])[0], bytes(pl[4:])))
        elif t == 0x45:
            pass
        else:
            return None
        p += sz
    if not segs or any(not s["frames"] for s in segs):
        return None
    if p < n and any(data[p:]):       # basura no nula al final
        return None
    return segs


_OGG_CRC = []
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        _r = ((_r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if _r & 0x80000000 else (_r << 1) & 0xFFFFFFFF
    _OGG_CRC.append(_r)


def _ogg_page(serial, seq, granule, flags, packets, cont_last=False):
    lace = bytearray()
    body = bytearray()
    for pk in packets:
        l = len(pk)
        while l >= 255:
            lace.append(255); l -= 255
        lace.append(l)
        body += pk
    hdr = bytearray(b"OggS\x00")
    hdr.append(flags)
    hdr += struct.pack("<qIII", granule, serial, seq, 0)
    hdr.append(len(lace))
    hdr += lace
    page = bytearray(hdr) + body
    crc = 0
    for b in page:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC[((crc >> 24) & 0xFF) ^ b]
    page[22:26] = struct.pack("<I", crc)
    return bytes(page)


def segment_to_ogg_opus(seg, serial=0x44535232):
    frames = seg["frames"]
    rate = seg["rate"] or 48000
    first = frames[0][1]
    channels = 2 if (first and first[0] & 0x04) else 1
    valid = sum(f[0] for f in frames)
    nominal = [_opus_pkt_samples(f[1]) for f in frames]
    # el primer frame trae (nominal - pre_skip) muestras validas; el recorte del final lo da el granule
    pre_skip = max(0, min(nominal[0] - frames[0][0], 3840))
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, channels, pre_skip, rate, 0, 0)
    vendor = b"ds2023_audio_tool"
    tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    out = bytearray()
    out += _ogg_page(serial, 0, 0, 0x02, [head])
    out += _ogg_page(serial, 1, 0, 0x00, [tags])
    seq = 2
    gran = 0
    i = 0
    nfr = len(frames)
    while i < nfr:
        pk = []
        segs_used = 0
        while i < nfr and len(pk) < 40:
            need = len(frames[i][1]) // 255 + 1
            if segs_used + need > 255:
                break
            pk.append(frames[i][1]); segs_used += need
            gran += nominal[i]
            i += 1
        last = i >= nfr
        g = (pre_skip + valid) if last else gran
        out += _ogg_page(serial, seq, g, 0x04 if last else 0x00, pk)
        seq += 1
    return bytes(out)



# ----------------------------------------------------------------------------
# WAV -> chunk EA Opus (para reemplazar audio)
# ----------------------------------------------------------------------------

def ogg_read_packets(data):
    """Lee un Ogg Opus. Devuelve (pre_skip, channels, rate, [paquetes])."""
    p = 0
    packets = []
    cur = bytearray()
    while p + 27 <= len(data) and data[p:p + 4] == b"OggS":
        nseg = data[p + 26]
        lace = data[p + 27:p + 27 + nseg]
        body = p + 27 + nseg
        for l in lace:
            cur += data[body:body + l]
            body += l
            if l < 255:
                packets.append(bytes(cur)); cur = bytearray()
        p = body
    if len(packets) < 3 or not packets[0].startswith(b"OpusHead"):
        raise ValueError("No es un Ogg Opus valido")
    ch = packets[0][9]
    pre = struct.unpack_from("<H", packets[0], 10)[0]
    rate = struct.unpack_from("<I", packets[0], 12)[0]
    return pre, ch, rate, packets[2:]


def build_ea_segment(packets, valid_samples, pre_skip=312, rate=48000, flags=0x1E):
    """Construye un segmento EA Opus (cabecera 0x48 + frames 0x44 + fin 0x45, alineado a 4)."""
    n = len(packets)
    nominal = [_opus_pkt_samples(pk) or 960 for pk in packets]
    sc = list(nominal)
    sc[0] = max(1, nominal[0] - pre_skip)
    if n > 1:
        sc[-1] = max(1, valid_samples - sum(sc[:-1]))
    else:
        sc[0] = valid_samples
    out = bytearray()
    out += b"\x48" + (12).to_bytes(3, "big") + bytes([flags]) + rate.to_bytes(3, "big") + struct.pack(">I", 0x40000000 | valid_samples)
    for c, pk in zip(sc, packets):
        out += b"\x44" + (8 + len(pk)).to_bytes(3, "big") + struct.pack(">I", c) + pk
    pad = (-(len(out) + 4)) % 4
    out += b"\x45" + (4 + pad).to_bytes(3, "big") + b"\x00" * pad
    return bytes(out)


def wav_to_ea_segment(wav_path, bitrate="112k", flags=0x1E, log=print):
    import tempfile
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("Hace falta ffmpeg en el PATH para codificar a Opus.")
    tmp = tempfile.mkdtemp()
    try:
        op = os.path.join(tmp, "x.opus")
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", wav_path, "-vn", "-ac", "1", "-ar", "48000",
                        "-c:a", "libopus", "-b:a", bitrate, "-frame_duration", "20", "-application", "voip", op],
                       check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        pre, ch, rate, packets = ogg_read_packets(open(op, "rb").read())
        # muestras validas reales = lo que decodifica ffmpeg
        pcm = subprocess.run([ff, "-loglevel", "error", "-i", op, "-f", "s16le", "-"], capture_output=True,
                             check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        valid = len(pcm) // 2
        return build_ea_segment(packets, valid, pre, 48000, flags), valid
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def wavs_to_ea_chunk(wav_paths, out_path, bitrate="112k", flags=0x1E, log=print):
    """Une uno o varios WAV (un segmento por WAV, en orden) en un chunk EA Opus."""
    blob = bytearray()
    for w in wav_paths:
        seg, valid = wav_to_ea_segment(w, bitrate, flags, log)
        blob += seg
        log("  %s -> %d muestras (%.2f s)" % (os.path.basename(w), valid, valid / 48000.0))
    with open(out_path, "wb") as f:
        f.write(blob)
    log("Chunk escrito: %s (%d bytes, %d segmentos)" % (out_path, len(blob), len(wav_paths)))
    return bytes(blob)


# ----------------------------------------------------------------------------
# Cache de Frosty (opcional): nombres con mayusculas correctas + tipos
# ----------------------------------------------------------------------------

_EBX_PAT = re.compile(
    rb"([\x20-\x7e]{3,400}?)\x00(.{20})(.{8})(.{8})(.{4})(.)([A-Za-z0-9_:<>\.]{2,100})\x00(.{16})", re.S)


def read_frosty_cache(path, log=print):
    """Devuelve (lower->nombre_real, {tipo: [nombre_real,...]}). Parser heuristico: cubre ~95%."""
    d = open(path, "rb").read()
    p = 8
    ver, head, a, n = struct.unpack_from("<IIII", d, p); p += 16
    for _ in range(n):
        p = d.index(b"\0", p) + 1
    nb = struct.unpack_from("<I", d, p)[0]; p += 4
    for _ in range(nb):
        p = d.index(b"\0", p) + 5
    start = p + 4
    names = {}
    bytype = defaultdict(list)
    for m in _EBX_PAT.finditer(d, start):
        sz, osz = struct.unpack("<QQ", m.group(3) + m.group(4))
        if sz >= 2 ** 31 or osz >= 2 ** 33:
            continue
        nm = m.group(1).decode("latin-1")
        # el regex puede arrastrar 1 byte imprimible del registro anterior
        if nm[:1] in "CGSAD" and nm[1:2] in "GSAUDCI" and nm[1:].lower().startswith(("game/", "sound/", "audio/")) and not nm.lower().startswith(("game/", "sound/", "audio/")):
            nm = nm[1:]
        t = m.group(7).decode("latin-1")
        names[nm.lower()] = nm
        bytype[t].append(nm)
    log("Cache Frosty: %d ebx con nombre (%d NewWaveAsset, %d MovieTexture2Asset)" % (
        len(names), len(bytype.get("NewWaveAsset", [])), len(bytype.get("MovieTexture2Asset", []))))
    return names, bytype


# ----------------------------------------------------------------------------
# Extraccion
# ----------------------------------------------------------------------------

class Extractor:
    def __init__(self, index, out_dir, log=print, progress=None, stop=None, names=None, bytype=None,
                 wav=True, join=False, keep_opus=False):
        self.wav = wav
        self.join = join
        self.keep_opus = keep_opus
        self._warned_ff = False
        self.ix = index
        self.out = out_dir
        self.log = log
        self.progress = progress or (lambda a, b: None)
        self.stop = stop or (lambda: False)
        self.names = names or {}
        self.bytype = bytype or {}
        self.manifest = []
        self.used_chunks = set()
        self.finder = None

    def real_name(self, n):
        return self.names.get(n.lower(), n)

    def _finder(self):
        if self.finder is None:
            self.finder = GuidFinder(self.ix.chunks.keys())
        return self.finder


    def write_ea(self, base, segs):
        """Escribe un chunk EA-Opus (varios segmentos) como .opus y/o .wav. Devuelve [(ruta_rel, ext, tam)]."""
        ff = shutil.which("ffmpeg")
        want_wav = self.wav
        if want_wav and not ff:
            if not self._warned_ff:
                self.log("  Aviso: ffmpeg no esta en el PATH; se guardan .opus (Ogg) sin convertir a WAV.")
                self._warned_ff = True
            want_wav = False
        join = self.join and want_wav and len(segs) > 1
        outs = []
        wavs = []
        for si, seg in enumerate(segs):
            suffix = "" if len(segs) == 1 else "_s%02d" % si
            rel_opus = base + suffix + ".opus"
            data = segment_to_ogg_opus(seg)
            path = self._write(rel_opus, data)
            if want_wav:
                wpath = os.path.splitext(path)[0] + ".wav"
                try:
                    if not os.path.exists(wpath):
                        subprocess.run([ff, "-y", "-loglevel", "error", "-i", path, "-acodec", "pcm_s16le", wpath],
                                       check=True, stderr=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    wavs.append(wpath)
                    outs.append((os.path.splitext(rel_opus)[0] + ".wav", ".wav", os.path.getsize(wpath)))
                    if not self.keep_opus and not join:
                        os.remove(path)
                    elif not self.keep_opus:
                        os.remove(path)
                    else:
                        outs.append((rel_opus, ".opus", len(data)))
                except Exception:
                    self.log("  ffmpeg no pudo convertir %s" % os.path.basename(path))
                    outs.append((rel_opus, ".opus", len(data)))
            else:
                outs.append((rel_opus, ".opus", len(data)))
        if join and len(wavs) == len(segs):
            import wave
            jp = os.path.join(self.out, base + ".wav")
            with wave.open(wavs[0], "rb") as w0:
                params = w0.getparams()
            with wave.open(jp, "wb") as wo:
                wo.setparams(params)
                for wp in wavs:
                    with wave.open(wp, "rb") as wi:
                        wo.writeframes(wi.readframes(wi.getnframes()))
            for wp in wavs:
                os.remove(wp)
            outs = [(base + ".wav", ".wav", os.path.getsize(jp))]
        return outs

    def _write(self, rel, data):
        path = os.path.join(self.out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) == len(data):
            return path
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _chunk_data(self, key):
        ents = self.ix.chunks.get(key, [])
        for e in ents:
            try:
                return self.ix.read_loc(e.loc)
            except Exception as ex:
                last = ex
        raise RuntimeError("chunk %s ilegible" % key)

    def _chunk_pref(self, key, lang, toc):
        """Prefiere el chunk del mismo toc/idioma."""
        ents = self.ix.chunks.get(key, [])
        best = None
        for e in ents:
            if e.toc == toc:
                return e
            if e.lang == lang and best is None:
                best = e
        return best or (ents[0] if ents else None)

    def extract_named(self, entries, kind_label, sub, lang_folder=True, convert_audio=False):
        fnd = self._finder()
        seen = set()
        total = len(entries)
        ok = 0; nochunk = 0
        for n, e in enumerate(entries):
            if self.stop():
                self.log("Cancelado.")
                break
            if n % 20 == 0:
                self.progress(n, total)
            try:
                payload = self.ix.read_loc(e.loc)
            except Exception as ex:
                self.log("  [!] %s: %s" % (e.name, ex))
                continue
            hits = fnd.find(payload)
            keys = [k for k, _ in hits]
            sig = (e.name.lower(), tuple(keys), e.lang if lang_folder else "")
            if sig in seen:
                continue
            seen.add(sig)
            if not keys:
                nochunk += 1
                self.manifest.append([self.real_name(e.name), e.lang, e.toc, kind_label, len(payload), "", "", "", ""])
                continue
            rn = self.real_name(e.name)
            for ci, key in enumerate(keys):
                ce = self._chunk_pref(key, e.lang, e.toc)
                try:
                    data = self.ix.read_loc(ce.loc)
                except Exception as ex:
                    self.log("  [!] chunk %s de %s: %s" % (key, rn, ex))
                    continue
                self.used_chunks.add(key)
                ext = sniff_ext(data)
                suffix = "" if len(keys) == 1 else "_c%d" % ci
                parts = [sub]
                if lang_folder:
                    parts.append(e.lang)
                segs = parse_ea_opus(data) if ext == ".bin" else None
                if segs:
                    base = os.path.join(*parts, safe_path(rn) + suffix)
                    for rel_o, ext_o, sz_o in self.write_ea(base, segs):
                        self.manifest.append([rn, e.lang, e.toc, kind_label, len(payload), key, sz_o, ext_o, rel_o])
                    ok += 1
                    continue
                rel = os.path.join(*parts, safe_path(rn) + suffix + ext)
                path = self._write(rel, data)
                self.manifest.append([rn, e.lang, e.toc, kind_label, len(payload), key, len(data), ext, rel])
                ok += 1
                if ext == ".webm" and convert_audio:
                    self.webm_audio(path)
        self.progress(total, total)
        self.log("%s: %d archivos extraidos, %d sin chunk referenciado (de %d)" % (kind_label, ok, nochunk, total))

    def webm_audio(self, path):
        ff = shutil.which("ffmpeg")
        if not ff:
            return
        out = os.path.splitext(path)[0] + ".wav"
        if os.path.exists(out):
            return
        try:
            subprocess.run([ff, "-y", "-loglevel", "error", "-i", path, "-vn", "-acodec", "pcm_s16le", out],
                           check=True, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as ex:
            self.log("  ffmpeg no pudo convertir %s" % os.path.basename(path))

    def audio_res(self, lang=None, text=None):
        ents = [e for e in self.ix.named if e.kind == "res" and e.rhash == NEWWAVE_HASH]
        if lang:
            ents = [e for e in ents if e.lang in (lang, "base")]
        if text:
            ents = [e for e in ents if text.lower() in e.name.lower()]
        return ents

    def videologs(self, lang=None, text=None):
        wanted = set(n.lower() for n in self.bytype.get("MovieTexture2Asset", []))
        if wanted:
            ents = [e for e in self.ix.named if e.kind == "ebx" and e.name.lower() in wanted]
        else:
            ents = [e for e in self.ix.named if e.kind == "ebx" and re.search(r"video|vid$|riglink", e.name.lower())]
        if lang:
            ents = [e for e in ents if e.lang in (lang, "base")]
        if text:
            ents = [e for e in ents if text.lower() in e.name.lower()]
        return ents

    def orphans(self, convert_audio=False):
        """Chunks no referenciados cuya firma es de audio/video. Lee solo el primer bloque."""
        keys = [k for k in self.ix.chunks if k not in self.used_chunks]
        self.log("Chunks sin referenciar a revisar: %d" % len(keys))
        n_ok = 0
        for n, key in enumerate(keys):
            if self.stop():
                break
            if n % 200 == 0:
                self.progress(n, len(keys))
            e = self.ix.chunks[key][0]
            try:
                head = self.ix.read_loc(e.loc, limit=64)
            except Exception:
                continue
            ext = sniff_ext(head)
            is_ea = ext == ".bin" and head[:1] == b"\x48" and head[1:4] == b"\x00\x00\x0c"
            if ext == ".bin" and not is_ea:
                continue
            try:
                data = self.ix.read_loc(e.loc)
            except Exception:
                continue
            if is_ea:
                segs = parse_ea_opus(data)
                if not segs:
                    continue
                for rel_o, ext_o, sz_o in self.write_ea(os.path.join("huerfanos", e.lang, key), segs):
                    self.manifest.append(["(sin nombre)", e.lang, e.toc, "orphan", 0, key, sz_o, ext_o, rel_o])
                n_ok += 1
                continue
            rel = os.path.join("huerfanos", e.lang, key + ext)
            self._write(rel, data)
            self.manifest.append(["(sin nombre)", e.lang, e.toc, "orphan", 0, key, len(data), ext, rel])
            n_ok += 1
        self.progress(len(keys), len(keys))
        self.log("Huerfanos de audio/video extraidos: %d" % n_ok)

    def save_manifest(self):
        os.makedirs(self.out, exist_ok=True)
        p = os.path.join(self.out, "manifest.csv")
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["nombre_real", "idioma", "toc", "tipo", "tam_res", "chunk_guid", "tam_chunk", "ext", "archivo"])
            w.writerows(self.manifest)
        self.log("manifest.csv escrito (%d filas)" % len(self.manifest))

    def analyze(self, text):
        """Diagnostico: muestra cabecera hex del res/ebx y los GUID de chunk que encuentra."""
        fnd = self._finder()
        found = [e for e in self.ix.named if text.lower() in e.name.lower()][:6]
        if not found:
            self.log("Sin coincidencias para '%s'" % text)
        for e in found:
            payload = self.ix.read_loc(e.loc)
            self.log("== %s [%s] %s  (%d bytes) toc=%s" % (e.name, e.kind, e.rtype, len(payload), e.toc))
            rawp = self._write(os.path.join("raw", safe_path(e.name) + "." + e.kind), payload)
            self.log("  crudo guardado: %s" % rawp)
            for off in range(0, min(len(payload), 192), 16):
                chunk = payload[off:off + 16]
                self.log("  %04X  %s" % (off, chunk.hex(" ")))
            for key, off in fnd.find(payload):
                ce = self.ix.chunks[key][0]
                try:
                    self._write(os.path.join("raw", key + ".chunk"), self.ix.read_loc(ce.loc))
                except Exception:
                    pass
                try:
                    head = self.ix.read_loc(ce.loc, limit=32)
                except Exception:
                    head = b""
                self.log("  -> chunk %s en offset 0x%X, cabecera %s (%s)" % (key, off, head[:16].hex(" "), sniff_ext(head)))


# ----------------------------------------------------------------------------
# Flujo completo
# ----------------------------------------------------------------------------

def run_job(opts, log, progress, stop):
    t0 = time.time()
    names = bytype = None
    if opts.get("cache") and os.path.isfile(opts["cache"]):
        try:
            names, bytype = read_frosty_cache(opts["cache"], log)
        except Exception as ex:
            log("No pude leer el cache de Frosty (%s); sigo sin el." % ex)
    ix = GameIndex(opts["game"], log, progress, stop)
    ix.build()
    ex = Extractor(ix, opts["out"], log, progress, stop, names, bytype,
                   wav=opts.get("wav", True), join=opts.get("join", False), keep_opus=opts.get("keep_opus", False))
    try:
        if opts.get("analyze"):
            ex.analyze(opts["analyze"])
            return
        if opts.get("audio", True):
            ents = ex.audio_res(opts.get("lang"), opts.get("filter"))
            log("NewWaveResource a procesar: %d" % len(ents))
            ex.extract_named(ents, "NewWaveResource", "audio")
        if opts.get("videologs"):
            ents = ex.videologs(opts.get("lang"), opts.get("filter"))
            log("Videologs (MovieTexture2Asset) a procesar: %d" % len(ents))
            ex.extract_named(ents, "MovieTexture2Asset", "videologs", lang_folder=False,
                             convert_audio=opts.get("webm_audio", True))
        if opts.get("orphans"):
            ex.orphans()
        ex.save_manifest()
    finally:
        ix.close()
    log("Listo en %.1f s -> %s" % (time.time() - t0, opts["out"]))


# ----------------------------------------------------------------------------
# GUI (CustomTkinter, adaptable, con scroll vertical)
# ----------------------------------------------------------------------------

def run_gui():
    import customtkinter as ctk
    from tkinter import filedialog

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    class App(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.title("Dead Space (2023) - Extractor de audio")
            self.geometry("920x720")
            self.minsize(560, 420)
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(0, weight=1)
            self._stop = False
            self._thread = None

            # Todo el contenido dentro de un frame con scroll vertical
            self.body = ctk.CTkScrollableFrame(self)
            self.body.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
            self.body.grid_columnconfigure(1, weight=1)
            b = self.body

            self.v_game = ctk.StringVar(); self.v_out = ctk.StringVar(); self.v_cache = ctk.StringVar()
            self.v_lang = ctk.StringVar(value="todos"); self.v_filter = ctk.StringVar(); self.v_an = ctk.StringVar()
            self.c_audio = ctk.BooleanVar(value=True); self.c_vid = ctk.BooleanVar(value=True)
            self.c_orph = ctk.BooleanVar(value=False); self.c_wav = ctk.BooleanVar(value=True)
            self.c_towav = ctk.BooleanVar(value=True); self.c_join = ctk.BooleanVar(value=False); self.c_keep = ctk.BooleanVar(value=False)

            def row(r, label, var, browse):
                ctk.CTkLabel(b, text=label, anchor="w").grid(row=r, column=0, sticky="w", padx=6, pady=4)
                ctk.CTkEntry(b, textvariable=var).grid(row=r, column=1, sticky="ew", padx=6, pady=4)
                ctk.CTkButton(b, text="...", width=40, command=browse).grid(row=r, column=2, padx=6, pady=4)

            row(0, "Carpeta del juego", self.v_game, lambda: self._pick_dir(self.v_game))
            row(1, "Carpeta de salida", self.v_out, lambda: self._pick_dir(self.v_out))
            row(2, "deadspace.cache (opcional)", self.v_cache, self._pick_cache)

            ctk.CTkLabel(b, text="Idioma", anchor="w").grid(row=3, column=0, sticky="w", padx=6, pady=4)
            ctk.CTkOptionMenu(b, variable=self.v_lang,
                              values=["todos", "es", "en", "fr", "de", "it", "pl", "cn", "tw"]).grid(
                row=3, column=1, sticky="w", padx=6, pady=4)
            ctk.CTkLabel(b, text="Filtro de nombre", anchor="w").grid(row=4, column=0, sticky="w", padx=6, pady=4)
            ctk.CTkEntry(b, textvariable=self.v_filter, placeholder_text="ej. CH07_0100").grid(
                row=4, column=1, sticky="ew", padx=6, pady=4)

            opts = ctk.CTkFrame(b)
            opts.grid(row=5, column=0, columnspan=3, sticky="ew", padx=6, pady=6)
            for i, (t, v) in enumerate([("Audio (NewWaveResource)", self.c_audio),
                                        ("Videologs (webm)", self.c_vid),
                                        ("Audio de videologs a WAV (ffmpeg)", self.c_wav),
                                        ("Chunks huerfanos de audio", self.c_orph),
                                        ("Convertir audio a WAV (ffmpeg)", self.c_towav),
                                        ("Unir segmentos en un solo WAV", self.c_join),
                                        ("Conservar tambien .opus", self.c_keep)]):
                ctk.CTkCheckBox(opts, text=t, variable=v).grid(row=i // 2, column=i % 2, sticky="w", padx=10, pady=4)

            ctk.CTkLabel(b, text="Analizar nombre", anchor="w").grid(row=6, column=0, sticky="w", padx=6, pady=4)
            ctk.CTkEntry(b, textvariable=self.v_an, placeholder_text="diagnostico de un asset (hex + GUIDs)").grid(
                row=6, column=1, sticky="ew", padx=6, pady=4)
            ctk.CTkButton(b, text="Analizar", width=80, command=lambda: self.start(True)).grid(row=6, column=2, padx=6)

            btns = ctk.CTkFrame(b, fg_color="transparent")
            btns.grid(row=7, column=0, columnspan=3, sticky="ew", padx=6, pady=6)
            btns.grid_columnconfigure((0, 1), weight=1)
            self.btn_go = ctk.CTkButton(btns, text="Extraer todo", command=lambda: self.start(False))
            self.btn_go.grid(row=0, column=0, sticky="ew", padx=4)
            ctk.CTkButton(btns, text="Cancelar", fg_color="#7a2b2b", hover_color="#5e2020",
                          command=self.cancel).grid(row=0, column=1, sticky="ew", padx=4)

            self.bar = ctk.CTkProgressBar(b)
            self.bar.set(0)
            self.bar.grid(row=8, column=0, columnspan=3, sticky="ew", padx=10, pady=6)

            # CTkTextbox trae su propia barra de scroll vertical y crece con la ventana
            self.logbox = ctk.CTkTextbox(b, height=320, wrap="word")
            self.logbox.grid(row=9, column=0, columnspan=3, sticky="nsew", padx=6, pady=6)
            b.grid_rowconfigure(9, weight=1)

            # --- WAV -> chunk EA Opus (para reemplazar audio) ---
            self.v_wavs = ctk.StringVar(); self.v_bin = ctk.StringVar(); self.v_br = ctk.StringVar(value="112k")
            box = ctk.CTkFrame(b)
            box.grid(row=10, column=0, columnspan=3, sticky="ew", padx=6, pady=8)
            box.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(box, text="Crear chunk desde WAV (un segmento por WAV, en orden)",
                         anchor="w").grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 2))
            ctk.CTkLabel(box, text="WAV(s)", anchor="w").grid(row=1, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(box, textvariable=self.v_wavs).grid(row=1, column=1, sticky="ew", padx=6, pady=4)
            ctk.CTkButton(box, text="...", width=40, command=self._pick_wavs).grid(row=1, column=2, padx=6)
            ctk.CTkLabel(box, text="Salida .bin", anchor="w").grid(row=2, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(box, textvariable=self.v_bin).grid(row=2, column=1, sticky="ew", padx=6, pady=4)
            ctk.CTkButton(box, text="...", width=40, command=self._pick_bin).grid(row=2, column=2, padx=6)
            ctk.CTkLabel(box, text="Bitrate", anchor="w").grid(row=3, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkOptionMenu(box, variable=self.v_br, values=["64k", "96k", "112k", "128k", "160k"]).grid(
                row=3, column=1, sticky="w", padx=6, pady=4)
            ctk.CTkButton(box, text="Crear chunk", command=self._make_chunk).grid(
                row=4, column=0, columnspan=3, sticky="ew", padx=8, pady=8)

        def _pick_dir(self, var):
            d = filedialog.askdirectory()
            if d:
                var.set(d)

        def _pick_cache(self):
            f = filedialog.askopenfilename(filetypes=[("Frosty cache", "*.cache"), ("Todos", "*.*")])
            if f:
                self.v_cache.set(f)

        def _pick_wavs(self):
            fs = filedialog.askopenfilenames(filetypes=[("WAV", "*.wav"), ("Todos", "*.*")])
            if fs:
                self.v_wavs.set(";".join(fs))

        def _pick_bin(self):
            f = filedialog.asksaveasfilename(defaultextension=".bin", filetypes=[("Chunk", "*.bin"), ("Todos", "*.*")])
            if f:
                self.v_bin.set(f)

        def _make_chunk(self):
            wavs = [w for w in self.v_wavs.get().split(";") if w]
            if not wavs or not self.v_bin.get():
                self.log("Elige al menos un WAV y el archivo .bin de salida.")
                return

            def work():
                try:
                    wavs_to_ea_chunk(wavs, self.v_bin.get(), self.v_br.get(), 0x1E, self.log)
                except Exception as ex:
                    self.log("ERROR: %s" % ex)
            threading.Thread(target=work, daemon=True).start()

        def log(self, s):
            self.after(0, lambda: (self.logbox.insert("end", s + "\n"), self.logbox.see("end")))

        def prog(self, a, b):
            self.after(0, lambda: self.bar.set(a / b if b else 0))

        def cancel(self):
            self._stop = True

        def start(self, analyze):
            if self._thread and self._thread.is_alive():
                return
            if not self.v_game.get() or not self.v_out.get():
                self.log("Elige carpeta del juego y de salida.")
                return
            self._stop = False
            lang = None if self.v_lang.get() == "todos" else self.v_lang.get()
            opts = dict(game=self.v_game.get(), out=self.v_out.get(), cache=self.v_cache.get() or None,
                        lang=lang, filter=self.v_filter.get() or None, audio=self.c_audio.get(),
                        videologs=self.c_vid.get(), webm_audio=self.c_wav.get(), orphans=self.c_orph.get(), wav=self.c_towav.get(),
                        join=self.c_join.get(), keep_opus=self.c_keep.get(),
                        analyze=(self.v_an.get() if analyze else None))

            def work():
                try:
                    run_job(opts, self.log, self.prog, lambda: self._stop)
                except Exception as ex:
                    import traceback
                    self.log("ERROR: %s\n%s" % (ex, traceback.format_exc()))
            self._thread = threading.Thread(target=work, daemon=True)
            self._thread.start()

    App().mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", action="store_true")
    ap.add_argument("--game"); ap.add_argument("--out"); ap.add_argument("--cache")
    ap.add_argument("--lang"); ap.add_argument("--filter")
    ap.add_argument("--videologs", action="store_true"); ap.add_argument("--orphans", action="store_true")
    ap.add_argument("--no-audio", action="store_true"); ap.add_argument("--analyze")
    ap.add_argument("--no-wav", action="store_true"); ap.add_argument("--join", action="store_true")
    ap.add_argument("--keep-opus", action="store_true")
    ap.add_argument("--encode", nargs="+", help="WAV(s) -> chunk EA Opus (usa --out como archivo .bin)")
    ap.add_argument("--bitrate", default="112k")
    a = ap.parse_args()
    if not a.cli:
        run_gui()
        return
    if a.encode:
        if not a.out:
            ap.error("--out = archivo .bin de salida")
        wavs_to_ea_chunk(a.encode, a.out, a.bitrate)
        return
    if not a.game or not a.out:
        ap.error("--game y --out son obligatorios en modo CLI")
    opts = dict(game=a.game, out=a.out, cache=a.cache, lang=a.lang, filter=a.filter, audio=not a.no_audio,
                videologs=a.videologs, orphans=a.orphans, analyze=a.analyze,
                wav=not a.no_wav, join=a.join, keep_opus=a.keep_opus)
    run_job(opts, print, lambda x, y: None, lambda: False)


if __name__ == "__main__":
    main()
