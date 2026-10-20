"""End-to-end self-test, no radio needed: synthesise two RTL-style IQ dwells with known signals, run the real pipeline
(process_dwell -> summarize -> report) on them, and check that every signal comes out.

Usage: . lib/env.sh && python3 tools/selftest.py [--keep DIR]

Dwell A (904.6 MHz): Meshtastic MediumFast, MeshCore 62.5 kHz / SF9, a LoRaWAN uplink, two 802.15.4g SUN-FSK frames.
Dwell B (923.643 MHz): a LoRaWAN Class B beacon at 923.3 MHz (implicit header).
"""
import argparse, binascii, json, os, shutil, subprocess, sys, tempfile, time
import numpy as np
from scipy.signal import resample_poly

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from gnuradio import gr, blocks, lora_sdr
from lora_decode import crc16_xmodem
from wisun_decode import PN9, SFD

FS = 2_400_000
NOISE_RMS = 4 / 127.5  # ADC counts, like real captures


def lora_iq(payload, sf, bw, sync, cr, implicit=False, os_=4):
    tb = gr.top_block()
    src = blocks.vector_source_b(list(payload + b','), False)
    chain = [src, lora_sdr.whitening(False, False, ',', 'packet_len'), lora_sdr.header(implicit, not implicit, cr),
             lora_sdr.add_crc(not implicit), lora_sdr.hamming_enc(cr, sf), lora_sdr.interleaver(cr, sf, 2, bw),
             lora_sdr.gray_demap(sf), lora_sdr.modulate(sf, bw * os_, bw, [sync], 2 ** sf * os_, 10 if implicit else 8)]
    snk = blocks.vector_sink_c()
    tb.connect(*chain, snk)
    tb.run()
    s = np.array(snk.data(), np.complex64)
    g = np.gcd(FS, bw * os_)
    return resample_poly(s, FS // g, bw * os_ // g).astype(np.complex64)


def sunfsk_iq(mpdu):
    psdu = mpdu + (binascii.crc32(mpdu) & 0xFFFFFFFF).to_bytes(4, 'little')
    phr = '0000' + '1' + format(len(psdu), '011b')  # MS 0, FCS type 0 (4 octets), data whitening on, length
    bits = np.unpackbits(np.frombuffer(psdu, np.uint8)[:, None], axis=1)[:, ::-1].ravel()  # octets LSB first
    bits = bits ^ PN9[:len(bits)]
    air = '10' * 32 + SFD + phr + ''.join(map(str, bits)) + '1010'
    sps = FS // 50_000
    f = np.repeat(np.array([25e3 if b == '1' else -25e3 for b in air]), sps)
    return np.exp(2j * np.pi * np.cumsum(f) / FS).astype(np.complex64)


def place(x, s, t, f_off, snr_db, bw):
    amp = np.sqrt(NOISE_RMS ** 2 * bw / FS * 10 ** (snr_db / 10))
    k = np.arange(len(s))
    i0 = int(t * FS)
    seg = (s * amp * np.exp(2j * np.pi * f_off / FS * k))[:len(x) - i0]
    x[i0:i0 + len(seg)] += seg


def write_cu8(path, x):
    q = np.clip(np.round(np.stack([x.real, x.imag], 1).ravel() * 127.5 + 127.5), 0, 255).astype(np.uint8)
    q.tofile(path)


def noise(dur, seed):
    rng = np.random.default_rng(seed)
    n = int(FS * dur)
    return (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex64) * NOISE_RMS / np.sqrt(2)


def build(run):
    dw = os.path.join(run, 'dwells'); iqd = os.path.join(run, 'iq')
    os.makedirs(dw); os.makedirs(iqd)
    t0 = time.time() - 600
    # dwell A
    fc = 904.6e6
    x = noise(8.0, 1)
    mt = bytes.fromhex('ffffffff') + bytes.fromhex('a1b2c3d4')[::-1] + (0x1234).to_bytes(4, 'little') + bytes([3 | (3 << 5), 8, 0, 0x55]) + bytes(range(20))
    place(x, lora_iq(mt, 9, 250000, 0x2B, 1), 0.3, 904.375e6 - fc, 6, 250000)
    mc = bytes([0x09, 0x02, 0x0f, 0x8e, 0xa5, 0x3c, 0x11, 0x22]) + bytes(range(16))  # Flood TxtMsg, 2 hops
    place(x, lora_iq(mc, 9, 62500, 0x12, 4), 1.5, 904.025e6 - fc, 6, 62500)
    lw = bytes([0x40]) + bytes.fromhex('26011234')[::-1] + bytes([0, 7, 0, 2]) + b'\x01\x02\x03\x04' + b'MIC!'
    place(x, lora_iq(lw, 7, 125000, 0x34, 1), 5.0, 903.9e6 - fc, 6, 125000)
    for i, t in enumerate((6.0, 7.0)):
        mpdu = bytes([0x41, 0x88, 10 + i, 0x1c, 0xb6, 0x02, 0x00, 0x01, 0x00]) + bytes(range(40))  # data, PAN b61c, short addrs
        place(x, sunfsk_iq(mpdu), t, 905.0e6 - fc, 30, 150000)
    a = 'wSELFTESTA_20000101T000000Z'
    write_cu8(os.path.join(iqd, a + '.cu8'), x)
    json.dump({'name': a, 'iq': os.path.join(iqd, a + '.cu8'), 'window': 1, 'pass': 0, 'fc': fc, 't0': t0, 'samples': len(x),
               'expected': len(x), 'rc': 0, 'lost': [], 'gain': 40, 'ppm': 0}, open(os.path.join(dw, a + '.meta.json'), 'w'))
    # dwell B: beacon (RFU 5 | GPS time 4 | CRC | InfoDesc 1, lat 3, lng 3 | RFU 3 | CRC)
    fc = 923.643e6
    x = noise(4.0, 2)
    p1 = bytes(5) + int(t0 + 900 - 315964800 + 18).to_bytes(4, 'little')
    p2 = bytes(7) + bytes(3)
    bcn = p1 + crc16_xmodem(p1).to_bytes(2, 'little') + p2 + crc16_xmodem(p2).to_bytes(2, 'little')
    place(x, lora_iq(bcn, 12, 500000, 0x34, 1, implicit=True), 0.5, 923.3e6 - fc, 3, 500000)
    b = 'wSELFTESTB_20000101T000400Z'
    write_cu8(os.path.join(iqd, b + '.cu8'), x)
    json.dump({'name': b, 'iq': os.path.join(iqd, b + '.cu8'), 'window': 12, 'pass': 0, 'fc': fc, 't0': t0 + 900, 'samples': len(x),
               'expected': len(x), 'rc': 0, 'lost': [], 'gain': 40, 'ppm': 0}, open(os.path.join(dw, b + '.meta.json'), 'w'))
    json.dump(dict(name='Self-test (synthetic signals)'), open(os.path.join(run, 'site.json'), 'w'))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--keep')
    a = ap.parse_args()
    run = a.keep or tempfile.mkdtemp(prefix='survey-selftest-')
    if a.keep and os.path.exists(run):
        shutil.rmtree(run)
    build(run)
    for m in sorted(os.listdir(os.path.join(run, 'dwells'))):
        r = subprocess.run([sys.executable, os.path.join(HERE, 'process_dwell.py'), os.path.join(run, 'dwells', m), '--fsk-seconds', '8'])
        if r.returncode:
            print(open(os.path.join(run, 'dwells', m.replace('.meta.json', '.process.log'))).read()[-3000:])
            sys.exit(f'FAIL: process_dwell {m}')
    subprocess.run([sys.executable, os.path.join(HERE, 'summarize.py'), run], check=True)
    subprocess.run([sys.executable, os.path.join(HERE, 'report.py'), run], check=True)
    S = json.load(open(os.path.join(run, 'summary.json')))
    labels = {c['label']: c for c in S['lora']['classes'] if c['crc_ok']}
    checks = [('Meshtastic MediumFast decoded', 'Meshtastic MediumFast' in labels),
              ('MeshCore decoded with valid structure', labels.get('MeshCore', {}).get('valid_structure', 0) >= 1),
              ('LoRaWAN uplink decoded', 'LoRaWAN uplink' in labels),
              ('LoRaWAN Class B beacon decoded (both CRCs)', 'LoRaWAN Class B beacon' in labels),
              ('802.15.4g frames pass the frame check', (S.get('sunfsk') or {}).get('ok', 0) >= 1),
              ('report written', os.path.exists(os.path.join(run, 'report', 'report.html')))]
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not a.keep:
        shutil.rmtree(run)
    if not all(ok for _, ok in checks):
        sys.exit('self-test FAILED' + ('' if a.keep else ' (rerun with --keep DIR to inspect)'))
    print('self-test passed')


if __name__ == '__main__':
    main()
