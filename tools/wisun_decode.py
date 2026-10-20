"""Find and decode IEEE 802.15.4g SUN-FSK (Wi-SUN FAN, 50 kbps mode 1a/1b) frame headers in RTL-SDR .cu8 windows.

Usage: python3 tools/wisun_decode.py FILE.cu8 [FILE2.cu8 ...] --fs HZ [--fc HZ] [--ppm 4.7] [--out frames.jsonl] [--workers N]
  (--fc defaults to the wNNNNNNk_ prefix of the file name)

Per narrow burst (fsk_survey.find): mix to its centre, 2-FSK discriminator, slice at 50 kbps, look for the uncoded
SFD 0x904E after a 1010 preamble, read the 16-bit PHR (MS, FCS type, data-whitening, 11-bit length), de-whiten the
PSDU with PN9, check the FCS (CRC-32 or CRC-16), then parse the MAC header and the Wi-SUN IEs.
Payloads are NOT written out: only header fields, IE ids, the network name (public, from PAN adverts), length, and a
short SHA-256 prefix of the PSDU.
"""
import argparse, binascii, hashlib, json, os, re, sys
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
from fsk_survey import find, load_cu8

BAUD = 50_000
SFD = '1001000001001110'  # 0x904E, uncoded, as transmitted
UTT_TYPES = {0: 'PAN-Advert', 1: 'PAN-Advert-Solicit', 2: 'PAN-Config', 3: 'PAN-Config-Solicit', 4: 'Data', 5: 'Ack',
             6: 'EAPOL', 9: 'LPA', 10: 'LPAS', 11: 'LPC', 12: 'LPCS', 15: 'Extended'}
MIC_LEN = {0: 0, 1: 4, 2: 8, 3: 16, 4: 0, 5: 4, 6: 8, 7: 16}  # by security level
MAC_TYPES = ['Beacon', 'Data', 'Ack', 'Cmd', 'Reserved', 'Multipurpose', 'Frak', 'Extended']


def pn9(n):
    out = np.empty(n, np.uint8)
    r = 0x1FF
    for i in range(n):
        out[i] = r & 1
        fb = (r ^ (r >> 5)) & 1
        r = (r >> 1) | (fb << 8)
    return out


def load_oui(path='/usr/share/ieee-data/oui.txt'):
    d = {}
    try:
        for line in open(path, encoding='utf-8', errors='replace'):
            if '(base 16)' in line:
                k, v = line.split('(base 16)')
                d[k.strip()] = v.strip()
    except OSError:
        pass
    return d


OUI = load_oui()
PN9 = pn9(8 * 2100 + 9)[9:]  # output taken 9 shifts on (feedback-bit convention); verified by CRC on real frames


def crc16_kermit(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


def soft_bits(x, fs, f_off):
    """Discriminator output averaged over each symbol; returns (values at the best timing phase, samples/symbol)."""
    n = len(x)
    x = x * np.exp(-2j * np.pi * f_off / fs * np.arange(n)).astype(np.complex64)
    X = np.fft.fft(x)
    f = np.fft.fftfreq(n, 1 / fs)
    X[np.abs(f) > 70e3] = 0
    y = np.fft.ifft(X)
    v = np.angle(y[1:] * np.conj(y[:-1]))
    sps = fs / BAUD
    w = int(sps)
    v = np.convolve(v, np.ones(w) / w, 'valid')
    on = np.abs(y[1:len(v) + 1]) > 0.5 * np.percentile(np.abs(y), 95)
    v = v - np.median(v[on]) if on.any() else v  # residual carrier offset -> slicer threshold
    best = max(range(w), key=lambda q: np.mean(np.abs(v[q::w][:2000])))
    return v[best::w] if float(sps).is_integer() else v[(best + np.arange(0, len(v) - best - 1, sps)).astype(int)]


def parse_mac(m):
    r = {}
    if len(m) < 2:
        return r
    fcf = m[0] | m[1] << 8
    ftype, sec, pend, ar, panc = fcf & 7, fcf >> 3 & 1, fcf >> 4 & 1, fcf >> 5 & 1, fcf >> 6 & 1
    sns, iep, dam, ver, sam = fcf >> 8 & 1, fcf >> 9 & 1, fcf >> 10 & 3, fcf >> 12 & 3, fcf >> 14 & 3
    r.update(mac_type=MAC_TYPES[ftype], security=bool(sec), ack_req=bool(ar), frame_ver=ver, ie_present=bool(iep))
    i = 2
    if not sns:
        r['seq'] = m[i] if i < len(m) else None
        i += 1
    alen = {0: 0, 2: 2, 3: 8}
    # 802.15.4-2015 PAN ID presence (frame version 2), simplified to the cases Wi-SUN uses
    if ver == 2:
        if dam and sam:
            dpan, span = (not panc), False
        elif dam:
            dpan, span = (not panc), False
        elif sam:
            dpan, span = False, (not panc)
        else:
            dpan, span = bool(panc), False
    else:
        dpan, span = dam != 0, sam != 0 and not panc
    try:
        if dpan:
            r['dst_pan'] = f"{m[i] | m[i + 1] << 8:04x}"; i += 2
        if dam:
            r['dst'] = m[i:i + alen.get(dam, 0)][::-1].hex(); i += alen.get(dam, 0)
        if span:
            r['src_pan'] = f"{m[i] | m[i + 1] << 8:04x}"; i += 2
        if sam:
            r['src'] = m[i:i + alen.get(sam, 0)][::-1].hex(); i += alen.get(sam, 0)
        end = len(m)
        level = 0
        if sec:
            sc = m[i]; i += 1
            level = sc & 7; kim = sc >> 3 & 3
            r['sec_level'] = level
            if not sc >> 5 & 1:
                i += 4  # frame counter
            i += {0: 0, 1: 1, 2: 5, 3: 9}[kim]
            end -= MIC_LEN[level]  # the MIC trails the frame: never walk IEs into it
        if i > end:
            r['truncated'] = True
        elif iep:
            parse_ies(m, i, end, r, encrypted_payload=level >= 4)
    except IndexError:
        r['truncated'] = True
    return r


def parse_ies(m, i, end, r, encrypted_payload):
    """Walk header IEs, then payload IEs when they are in the clear, over m[i:end] (end excludes the MIC).
    Sets ie_struct_ok when every IE fits and the walk ends exactly at a termination IE or at `end`."""
    hies = []
    fits = True
    while i + 2 <= end:
        d = m[i] | m[i + 1] << 8
        ln, eid = d & 0x7F, d >> 7 & 0xFF
        if i + 2 + ln > end:
            fits = False
            break
        c = m[i + 2:i + 2 + ln]
        i += 2 + ln
        if eid == 0x2A and c:  # Wi-SUN header IE
            sub = c[0]
            hies.append(f"wh{sub:02x}")
            if sub == 0x01 and len(c) >= 2:  # UTT-IE
                r['wisun_type'] = UTT_TYPES.get(c[1] & 0x0F, c[1] & 0x0F)
        else:
            hies.append(f"h{eid:02x}")
        if eid in (0x7E, 0x7F):  # header termination 1 (payload IEs follow) / 2 (payload follows)
            break
    r['hdr_ies'] = hies
    term = bool(hies) and hies[-1] in ('h7e', 'h7f')
    r['ie_struct_ok'] = fits and (term or i == end)
    if hies and hies[-1] == 'h7e' and not encrypted_payload:
        pies = []
        while i + 2 <= end:
            d = m[i] | m[i + 1] << 8
            ln, gid = d & 0x7FF, d >> 11 & 0xF
            if i + 2 + ln > end:
                r['ie_struct_ok'] = False
                break
            c = m[i + 2:i + 2 + ln]
            i += 2 + ln
            pies.append(f"p{gid:x}")
            if gid == 0x4:  # WP-IE: nested sub-IEs
                j = 0
                while j + 2 <= len(c):
                    sd = c[j] | c[j + 1] << 8
                    if sd >> 15:  # long format: len 11 bits, sub-id 4 bits
                        sl, sid = sd & 0x7FF, sd >> 11 & 0xF
                        pies.append(f"wl{sid:x}")
                    else:  # short format: len 8 bits, sub-id 7 bits
                        sl, sid = sd & 0xFF, sd >> 8 & 0x7F
                        pies.append(f"ws{sid:02x}")
                        sc = c[j + 2:j + 2 + sl]
                        if sid == 0x05 and sc:  # NETNAME-IE
                            r['netname'] = sc.decode('ascii', 'replace')
                        elif sid == 0x04 and len(sc) >= 5:  # PAN-IE
                            r['pan_size'] = sc[0] | sc[1] << 8
                            r['routing_cost'] = sc[2] | sc[3] << 8
                        elif sid == 0x06 and len(sc) >= 2:  # PANVER-IE
                            r['pan_version'] = sc[0] | sc[1] << 8
                    j += 2 + sl
            if gid == 0xF:
                break
        else:
            if i != end:
                r['ie_struct_ok'] = False
        r['pay_ies'] = pies


def decode_burst(path, fs, fc, ppm, b, total):
    pre = int(0.002 * fs)
    s0 = max(0, b['s0'] - pre)
    head = load_cu8(path, s0, min(b['n'] + 2 * pre, total - s0))
    v = soft_bits(head, fs, b['f_off'])
    bits = ''.join('1' if q > 0 else '0' for q in v)
    k = bits.find('1010' + SFD)
    if k < 0:
        k = bits.find('0101' + SFD)
    if k < 0:
        return None
    k += 4 + 16
    phr = bits[k:k + 16]
    if len(phr) < 16:
        return None
    ms, fcs_type, dw = phr[0] == '1', int(phr[3]), phr[4] == '1'
    ln = int(phr[5:], 2)
    if ms or ln < 5 or ln > 2047:
        return dict(phr=phr, ok=False, reason='phr')
    # re-read long enough for the whole PSDU (the burst region may have been cut short)
    sfd_sample = s0 + int((k - 32) * fs / BAUD)
    need = int((32 + 16 + 8 * ln + 64) * fs / BAUD)
    st = max(0, sfd_sample - int(0.001 * fs))
    v = soft_bits(load_cu8(path, st, min(need + int(0.002 * fs), total - st)), fs, b['f_off'])
    bits = ''.join('1' if q > 0 else '0' for q in v)
    k2 = bits.find(SFD + phr)
    if k2 < 0:
        return dict(phr=phr, ok=False, reason='resync')
    p = np.frombuffer(bits[k2 + 32:k2 + 32 + 8 * ln].encode(), np.uint8) - 48
    if len(p) < 8 * ln:
        return dict(phr=phr, len=ln, ok=False, reason='short')
    if dw:
        p = p ^ PN9[:len(p)]
    psdu = bytes(np.packbits(p.reshape(-1, 8)[:, ::-1], axis=1).ravel())  # octets go LSB first
    nf = 2 if fcs_type else 4
    mpdu, fcs = psdu[:-nf], psdu[-nf:]
    ok = (crc16_kermit(mpdu) == int.from_bytes(fcs, 'little')) if fcs_type else \
         (binascii.crc32(mpdu) & 0xFFFFFFFF) == int.from_bytes(fcs, 'little')
    if not ok and not fcs_type:  # also try CRC-16 on 4-octet-flagged frames (some stacks mis-set the bit)
        ok16 = crc16_kermit(psdu[:-2]) == int.from_bytes(psdu[-2:], 'little')
        if ok16:
            ok, mpdu, nf = True, psdu[:-2], 2
    t = sfd_sample / fs
    rec = dict(t=round(t, 5), f=round(fc + b['f_off']), f_nominal=round(round((fc + b['f_off']) * (1 - ppm * 1e-6) / 200e3) * 200e3),
               len=ln, fcs32=not fcs_type, whitened=dw, ok=ok, sha8=hashlib.sha256(psdu).hexdigest()[:8])
    # header fields are parsed even when the FCS fails. The Landis+Gyr IE frames (27/33 B Enhanced Acks, 77 B beacons)
    # never pass: their bits are clean, but the last 4 octets are no affine CRC of the frame (every span, bit order and
    # CRC-16/32 polynomial tested), so the check covers something not sent on air. ie_struct_ok (IEs end exactly at
    # the MIC) is a weak substitute: random bytes pass it ~2.5% of the time. Trust an address only if it is in an
    # FCS-ok frame or recurs across frames
    rec['hdr3'] = mpdu[:3].hex()
    rec.update(parse_mac(mpdu))
    for k in ('src', 'dst'):
        if len(rec.get(k) or '') == 16:
            rec[k + '_vendor'] = OUI.get(rec[k][:6].upper(), '?')
    return rec


def decode_file(args):
    path, fs, fc, ppm = args
    total = np.memmap(path, dtype=np.uint8, mode='r').size // 2
    bursts = [b for b in find(path, fs) if b['n'] / fs >= 0.0015]
    out, seen = [], set()
    for b in bursts:
        try:
            r = decode_burst(path, fs, fc, ppm, b, total)
        except Exception as e:  # never let one burst stop a file
            r = dict(ok=False, reason=f'err {e}')
        if not r or 't' not in r:
            continue
        key = (round(r['t'], 3), r['f_nominal'])
        if key in seen:
            continue
        seen.add(key)
        r['file'] = os.path.basename(path)
        out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='+'); ap.add_argument('--fs', type=float, default=2.4e6); ap.add_argument('--fc', type=float)
    ap.add_argument('--ppm', type=float, default=4.7); ap.add_argument('--out'); ap.add_argument('--workers', type=int, default=1)
    a = ap.parse_args()
    jobs = []
    for f in a.files:
        fc = a.fc
        if fc is None:
            m = re.match(r'w(\d+)k_', os.path.basename(f))
            fc = float(m.group(1)) * 1e3
        jobs.append((f, a.fs, fc, a.ppm))
    out = open(a.out, 'a') if a.out else None
    if a.workers > 1:
        from multiprocessing import Pool
        it = Pool(a.workers).imap_unordered(decode_file, jobs)
    else:
        it = map(decode_file, jobs)
    for recs in it:
        nok = sum(r['ok'] for r in recs)
        print(f"{recs[0]['file'] if recs else '?'}: {len(recs)} SFDs, {nok} FCS ok", file=sys.stderr, flush=True)
        for r in recs:
            if out:
                out.write(json.dumps(r) + '\n')
            else:
                print(json.dumps(r))
        if out:
            out.flush()


if __name__ == '__main__':
    main()
