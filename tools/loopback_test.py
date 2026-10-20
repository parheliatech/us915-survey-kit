"""Loopback test: modulate known LoRa packets, add noise + CFO, decode with lora_rx.RxChain (any sync word).

Usage: . lib/env.sh && python3 tools/loopback_test.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from gnuradio import gr, blocks, channels
from gnuradio import lora_sdr
from lora_rx import RxChain

OS = 4


def run(sf, bw, sync, snr_db=0.0, ppm=3.0, cr=1, n=4, soft=True, fc=915e6):
    fs = bw * OS
    msgs = [f"sf{sf} bw{bw//1000}k sw{sync:02x} #{i}" for i in range(n)]
    tb = gr.top_block()
    src = blocks.vector_source_b(list((",".join(msgs) + ",").encode()), False)
    white = lora_sdr.whitening(False, False, ',', 'packet_len')
    hdr = lora_sdr.header(False, True, cr)
    addcrc = lora_sdr.add_crc(True)
    ham = lora_sdr.hamming_enc(cr, sf)
    inter = lora_sdr.interleaver(cr, sf, 2, bw)
    gray = lora_sdr.gray_demap(sf)
    mod = lora_sdr.modulate(sf, fs, bw, [sync], int(20 * 2**sf * OS), 8)
    # signal power is 1; noise_voltage sets SNR over the full fs bandwidth.
    # A crystal error of `ppm` shifts carrier and sample clock together (as in real radios and the RTL-SDR).
    chan = channels.channel_model(noise_voltage=10 ** (-snr_db / 20), frequency_offset=fc * ppm * 1e-6 / fs,
                                  epsilon=1.0 + ppm * 1e-6, taps=[1.0 + 0j], noise_seed=sf * 7 + sync, block_tags=True)
    rx = RxChain(sf, bw, OS, center_freq=fc, soft=soft)
    tb.connect(src, white, hdr, addcrc, ham, inter, gray, mod, chan, rx)
    tb.run()
    got = rx.frames()
    ok = [f for f in got if f['crc_ok']]
    payload_match = sum(1 for f in ok if f['payload'].decode(errors='replace') in msgs)
    bad = [i for i, f in enumerate(got) if not f['crc_ok']]
    syncs = sorted({f'{f["sync"]:02x}' for f in got})
    return len(msgs), len(got), len(ok), payload_match, syncs, [f['snr'] for f in got][:2], [f['cfo_bins'] for f in got][:1], bad


if __name__ == '__main__':
    cases = [(10, 125000, 0x34), (12, 125000, 0x34), (11, 250000, 0x12), (12, 500000, 0x2B), (11, 125000, 0x2B), (9, 500000, 0x12), (7, 125000, 0x34), (7, 125000, 0x2B), (7, 125000, 0x12), (9, 250000, 0x2B), (10, 125000, 0x34),
             (12, 125000, 0x34), (8, 500000, 0x34), (11, 250000, 0x12)]
    print("sf  bw     sync   ppm  sent detected crc_ok match  sync_seen  snr_est  cfo_bins")
    for sf, bw, sw in cases:
        for ppm in (0.0, 3.0, -12.0):
            sent, det, ok, match, syncs, snr, cfo, bad = run(sf, bw, sw, ppm=ppm)
            print(f"{sf:<3} {bw//1000:>3}k   0x{sw:02x}  {ppm:>5} {sent:>4} {det:>8} {ok:>6} {match:>5}  {','.join(syncs):<9}  {snr}  {cfo}  failed#{bad}")
    print("\nlow-SNR (SF7/125k, 0x34, 20 packets; SNR over the 500 kHz sample band, in-channel is +6 dB):")
    for soft in (False, True):
        for snr_db in (-8, -10, -12, -14):
            sent, det, ok, match, syncs, *_ = run(7, 125000, 0x34, snr_db=snr_db, n=20, soft=soft)
            print(f"  {'soft' if soft else 'hard'} SNR {snr_db:>4} dB: sent {sent} detected {det} crc_ok {ok} match {match} sync {syncs}")
