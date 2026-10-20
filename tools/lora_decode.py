"""Decode LoRa detections from an RTL-SDR .cu8 window and classify them (protocol-agnostic).

Usage: . lib/env.sh && python3 tools/lora_decode.py FILE.cu8 --fc HZ --fs HZ [--det det.json] [--out recs.jsonl]

For each preamble from lora_detect: cut the packet out of the wide window, shift to baseband, resample to 4x BW,
run the patched gr-lora_sdr chain (any sync word), then parse LoRaWAN / Meshtastic / MeshCore headers where the sync word fits.
500 kHz / SF12 preambles that give no explicit header are also tried as LoRaWAN US902-928 Class B beacons
(implicit header, 23 bytes, CR 4/5; RP002-1.0.3 section 2.5.8).
Payload bytes are NOT written out: only parsed header fields, length and a short SHA-256 prefix (for duplicate spotting).

Crash safety: gr-lora_sdr can segfault on rare inputs. With --out, records are appended and flushed per detection and
progress is kept in OUT.progress; rerunning the same command resumes, skipping the detection that was being decoded
when the previous run died.
"""
import argparse, hashlib, json, math, os, sys
import numpy as np
from scipy.signal import resample_poly
sys.path.insert(0, os.path.dirname(__file__))
from gnuradio import gr, blocks
from lora_rx import RxChain
from lora_detect import detect_file, load_cu8

OS = 4


def airtime_s(sf, bw, nbytes=255, cr=1):
    """Upper bound on packet duration (explicit header, CRC on) used to size the snippet."""
    de = 1 if (2 ** sf / bw) > 0.016 else 0
    nsym = 8 + max(math.ceil((8 * nbytes - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * (cr + 4), 0)
    return (12.25 + nsym) * 2 ** sf / bw


RASTER = 25e3  # US LoRa plans sit on it: LoRaWAN 902.3+0.2n, Meshtastic 902.0+bw/2+n*bw, MeshCore preset 910.525


def decode_one(path, fs, fc, det, total, ppm=0.0):
    """Two passes. frame_sync treats the whole carrier offset as crystal error and derives the clock drift from it,
    so a detector-grid miss (up to BW/4) breaks long packets. Pass 1 measures the carrier; pass 2 re-centres on the
    nearest 25 kHz raster point (after removing the dongle's own `ppm` error), leaving only true crystal offset."""
    frames = _decode_at(path, fs, fc, det, det['f_chan'], total)
    hdr = [f for f in frames if f['header_ok']]
    centre = det['f_chan']
    if not hdr and det['ratio_db'] >= 15:
        # A strong preamble with no header: the carrier may sit up to bw/2 from the detector channel, where frame_sync's
        # CFO estimate wraps. Retry a quarter bandwidth either side.
        for shift in (det['bw'] / 4, -det['bw'] / 4):
            again = _decode_at(path, fs, fc, det, det['f_chan'] + shift, total)
            if any(f['header_ok'] for f in again):
                frames, centre = again, det['f_chan'] + shift
                hdr = [f for f in frames if f['header_ok']]
                break
    if not hdr or all(f['crc_ok'] for f in hdr):
        return frames
    f_est = centre + hdr[0]['cfo_bins'] * det['bw'] / 2 ** det['sf']
    f_nom = round(f_est * (1 - ppm * 1e-6) / RASTER) * RASTER
    if abs(f_nom - centre) < det['bw'] / 2 ** det['sf'] / 2:
        return frames
    # Mix to the nominal channel, not to where it appears: the dongle's ppm error shifts LO and sample clock together,
    # so leaving it in the residual CFO lets frame_sync derive the matching clock drift. ppm only steers the snap.
    again = _decode_at(path, fs, fc, det, f_nom, total)
    if sum(bool(f['crc_ok']) for f in again) >= sum(bool(f['crc_ok']) for f in frames):
        for f in again:
            f['f_nominal'] = f_nom
        return again
    return frames


def _decode_at(path, fs, fc, det, f_centre, total):
    sym = 2 ** det['sf'] / det['bw']
    span = min(airtime_s(det['sf'], det['bw']), 4.0)
    s0 = max(0, int((det['t'] - 3 * sym) * fs))
    n = min(int((span + 6 * sym) * fs), total - s0)
    x = load_cu8(path, s0, n)
    x = x * np.exp(-2j * np.pi * (f_centre - fc) / fs * np.arange(n)).astype(np.complex64)
    rate = det['bw'] * OS
    g = math.gcd(int(fs), int(rate))
    y = resample_poly(x, rate // g, int(fs) // g).astype(np.complex64)
    y = np.concatenate([y, np.zeros(2 ** det['sf'] * OS * 4, np.complex64)])  # flush the chain
    tb = gr.top_block()
    rx = RxChain(det['sf'], det['bw'], OS, center_freq=f_centre)
    tb.connect(blocks.vector_source_c(y.tolist(), False), rx)
    tb.run()
    frames = rx.frames()
    for f in frames:
        f['t_abs'] = round(s0 / fs + f['in_offset'] / rate, 4)
        f['f_centre'] = f_centre
    return frames


BEACON_LEN = 23  # US902-928: RFU 5 | Time 4 | CRC 2 | GwSpecific 7 | RFU 3 | CRC 2 (pre-1.0.4 layout, RP002 footnote 22)
DL_CHANNELS = [923.3e6 + 0.6e6 * n for n in range(8)]


def crc16_xmodem(data):
    c = 0
    for b in data:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def parse_beacon(p):
    """Beacon CRCs are CRC-16/XMODEM (poly 0x1021, init 0), stored little-endian; verified on real 923.3 MHz beacons.
    Time is GPS seconds. GwSpecific = InfoDesc 1 | Lat 3 | Lng 3 (signed, little-endian, scaled to +-90 / +-180 deg)."""
    if p is None or len(p) != BEACON_LEN:
        return None
    ok1 = crc16_xmodem(p[0:9]) == int.from_bytes(p[9:11], 'little')
    ok2 = crc16_xmodem(p[11:21]) == int.from_bytes(p[21:23], 'little')
    if not (ok1 or ok2):
        return None
    r = dict(proto='LoRaWAN-beacon', beacon_crc1_ok=ok1, beacon_crc2_ok=ok2)
    if ok1:
        r['beacon_gps_time'] = int.from_bytes(p[5:9], 'little')
    if ok2:
        lat = int.from_bytes(p[12:15], 'little', signed=True) * 90 / 2 ** 23
        lng = int.from_bytes(p[15:18], 'little', signed=True) * 180 / 2 ** 23
        r.update(beacon_info_desc=p[11], beacon_lat=round(lat, 5), beacon_lng=round(lng, 5))
    return r


def decode_beacon(path, fs, fc, det, total, ppm=0.0):
    """Implicit-header decode of a 500 kHz / SF12 preamble at the nearest US downlink channel."""
    f_seen = det['f_chan'] * (1 - ppm * 1e-6)
    f_nom = min(DL_CHANNELS, key=lambda c: abs(c - f_seen))
    if abs(f_nom - f_seen) > 300e3 or abs(f_nom - fc) > fs / 2 - 350e3:
        return []
    s0 = max(0, int((det['t'] - 0.5) * fs))
    n = min(int(2.0 * fs), total - s0)
    x = load_cu8(path, s0, n)
    x = x * np.exp(-2j * np.pi * (f_nom - fc) / fs * np.arange(n)).astype(np.complex64)
    rate = det['bw'] * OS
    g = math.gcd(int(fs), int(rate))
    y = resample_poly(x, rate // g, int(fs) // g).astype(np.complex64)
    y = np.concatenate([y, np.zeros(2 ** det['sf'] * OS * 4, np.complex64)])
    tb = gr.top_block()
    rx = RxChain(det['sf'], det['bw'], OS, center_freq=f_nom, implicit=(1, BEACON_LEN, False))
    tb.connect(blocks.vector_source_c(y.tolist(), False), rx)
    tb.run()
    out = []
    for f in rx.frames():
        b = parse_beacon(f['payload'])
        if b:
            f.update(t_abs=round(s0 / fs + f['in_offset'] / rate, 4), f_centre=f_nom, f_nominal=f_nom, beacon=b,
                     header_ok=True, crc_ok=b['beacon_crc1_ok'] and b['beacon_crc2_ok'])
            out.append(f)
    return out


def parse_lorawan(p):
    if len(p) < 12:
        return None
    mtype = p[0] >> 5
    names = ['JoinRequest', 'JoinAccept', 'UnconfDataUp', 'UnconfDataDown', 'ConfDataUp', 'ConfDataDown', 'RejoinReq', 'Proprietary']
    r = dict(proto='LoRaWAN?', mtype=names[mtype], major=p[0] & 3)
    if mtype == 0 and len(p) == 23:
        r.update(join_eui=p[1:9][::-1].hex(), dev_eui=p[9:17][::-1].hex(), dev_nonce=int.from_bytes(p[17:19], 'little'))
    elif mtype in (2, 3, 4, 5):
        fopts = p[5] & 0x0F
        r.update(dev_addr=p[1:5][::-1].hex(), fctrl=p[5], fcnt=int.from_bytes(p[6:8], 'little'), fopts_len=fopts,
                 fport=p[8 + fopts] if len(p) > 8 + fopts + 4 else None)
    return r


def parse_meshtastic(p):
    if len(p) < 16:
        return None
    flags = p[12]
    return dict(proto='Meshtastic?', dest=p[0:4][::-1].hex(), sender=p[4:8][::-1].hex(),
                packet_id=p[8:12][::-1].hex(), hop_limit=flags & 7, want_ack=bool(flags & 8),
                hop_start=(flags >> 5) & 7, channel_hash=p[13], next_hop=p[14], relay_node=p[15])


MC_ROUTES = ['TransportFlood', 'Flood', 'Direct', 'TransportDirect']
MC_TYPES = {0: 'Req', 1: 'Response', 2: 'TxtMsg', 3: 'Ack', 4: 'Advert', 5: 'GrpTxt', 6: 'GrpData', 7: 'AnonReq',
            8: 'Path', 9: 'Trace', 10: 'Multipart', 11: 'Control', 15: 'RawCustom'}


def parse_meshcore(p):
    """MeshCore wire format (meshcore-dev/MeshCore src/Packet.cpp writeTo): header, [4 B transport codes], path_len,
    path (hash_count x hash_size bytes), payload. Header: route bits 0-1, payload type bits 2-5, version bits 6-7.
    Only addressing fields are kept (1-byte node hashes, channel hash, advert key prefix and node type); no MAC,
    ciphertext, names or locations."""
    if len(p) < 3:
        return None
    hdr = p[0]
    route, ptype, ver = hdr & 3, (hdr >> 2) & 0x0F, hdr >> 6
    i = 1
    r = dict(proto='MeshCore?', mc_route=MC_ROUTES[route], mc_type=MC_TYPES.get(ptype, f'type{ptype}'), mc_ver=ver)
    if route in (0, 3):
        r['transport_codes'] = p[1:5].hex()
        i = 5
    if i >= len(p):
        return None
    pl = p[i]
    i += 1
    count, size = pl & 63, (pl >> 6) + 1
    if size == 4 or count * size > 64 or i + count * size >= len(p):
        return None  # readFrom() would reject it: not MeshCore (or corrupt)
    r.update(hops=count, hash_size=size, path=[p[i + k * size:i + (k + 1) * size].hex() for k in range(count)])
    q = p[i + count * size:]
    r['mc_payload_len'] = len(q)
    if ver == 0 and ptype in (0, 1, 2, 8) and len(q) >= 4:   # dest/src hashes, 2-byte MAC, ciphertext
        r.update(dest_hash=q[0:1].hex(), src_hash=q[1:2].hex())
    elif ver == 0 and ptype in (5, 6) and len(q) >= 3:      # channel hash, MAC, ciphertext
        r['channel_hash'] = q[0]
    elif ver == 0 and ptype == 7 and len(q) >= 35:          # dest hash, ephemeral public key, MAC
        r['dest_hash'] = q[0:1].hex()
    elif ptype == 4 and len(q) >= 101:                      # public key 32, timestamp 4, signature 64, app data
        r.update(adv_key8=q[0:4].hex(), adv_node_type=q[100] & 0x0F)
    return r


def classify(f):
    out = {}
    if f.get('beacon'):
        return f['beacon']
    if not f.get('header_ok') or f.get('payload') is None:
        return out
    p = f['payload']
    if f['sync'] == 0x34:
        out = parse_lorawan(p) or {}
    elif f['sync'] == 0x2B:
        out = parse_meshtastic(p) or {}
    elif f['sync'] == 0x12:  # LoRa "private" sync word: MeshCore, also private LoRaWAN and others
        out = parse_meshcore(p) or {}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file'); ap.add_argument('--fc', type=float, required=True); ap.add_argument('--fs', type=float, required=True)
    ap.add_argument('--det'); ap.add_argument('--out')
    ap.add_argument('--ppm', type=float, default=0.0, help="dongle frequency error (+ = reads high)")
    ap.add_argument('--t0', type=float, help="capture start, Unix seconds: adds absolute 'ts' to each record")
    a = ap.parse_args()
    total = np.memmap(a.file, dtype=np.uint8, mode='r').size // 2
    if a.det and os.path.exists(a.det):
        dets = json.load(open(a.det))
    else:
        dets, _ = detect_file(a.file, a.fc, a.fs)
        if a.det:
            json.dump(dets, open(a.det, 'w'), indent=0)
    # progress: one line per finished detection index; 'start i' marks the one in progress (a crash leaves it last)
    prog_path = a.out + '.progress' if a.out else None
    done, skip = set(), set()
    if prog_path and os.path.exists(prog_path):
        last_start = None
        for line in open(prog_path):
            k, _, v = line.strip().partition(' ')
            if k == 'done':
                done.add(int(v)); last_start = None
            elif k == 'start':
                last_start = int(v)
            elif k == 'skip':
                skip.add(int(v))
        if last_start is not None and last_start not in done:
            skip.add(last_start)
            with open(prog_path, 'a') as pf:
                pf.write(f'skip {last_start}\n')
            print(f'resuming: skipping detection #{last_start} (previous run died on it)', file=sys.stderr)
    out = open(a.out, 'a' if done or skip else 'w') if a.out else None
    pf = open(prog_path, 'a') if prog_path else None
    seen = set()
    for i, d in enumerate(dets):
        if i in done or i in skip:
            continue
        if pf:
            pf.write(f'start {i}\n'); pf.flush(); os.fsync(pf.fileno())
        frames = decode_one(a.file, a.fs, a.fc, d, total, a.ppm)
        if d['bw'] == 500000 and d['sf'] == 12 and not any(f['header_ok'] for f in frames):
            frames = decode_beacon(a.file, a.fs, a.fc, d, total, a.ppm) or frames
        for alt in d.get('alts', []):  # same chirp slope, other BW/SF: the weaker reading may be the real mode
            if any(f['header_ok'] for f in frames):
                break
            again = decode_one(a.file, a.fs, a.fc, alt, total, a.ppm)
            if any(f['header_ok'] for f in again):
                frames, d = again, dict(d, bw=alt['bw'], sf=alt['sf'], f_chan=alt['f_chan'], ratio_db=alt['ratio_db'])
        for f in frames:
            key = (round(f['t_abs'], 2), d['bw'], d['sf'])
            if key in seen:
                continue
            seen.add(key)
            p = f.pop('payload')
            rec = dict(t=f['t_abs'], f_chan=d['f_chan'], f_est=round(f['f_centre'] + f['cfo_bins'] * d['bw'] / 2 ** d['sf']),
                       f_nominal=f.get('f_nominal'), bw=d['bw'], sf=d['sf'], det_ratio_db=d['ratio_db'],
                       sync=f'0x{f["sync"]:02x}' if not f.get('beacon') else None, snr=f['snr'], cfo_bins=f['cfo_bins'],
                       header_ok=f['header_ok'], cr=f['cr'], pay_len=f['pay_len'], has_crc=f['has_crc'],
                       crc_ok=f['crc_ok'], sha8=hashlib.sha256(p).hexdigest()[:8] if p is not None else None, det_index=i)
            f['payload'] = p
            rec.update(classify(f))
            if a.t0 is not None:
                rec['ts'] = round(a.t0 + rec['t'], 3)
                rec['fc'] = a.fc
            line = json.dumps(rec)
            print(line)
            if out:
                out.write(line + '\n')
        if out:
            out.flush(); os.fsync(out.fileno())
        if pf:
            pf.write(f'done {i}\n'); pf.flush()
    if pf:
        pf.write('complete\n')


if __name__ == '__main__':
    main()
