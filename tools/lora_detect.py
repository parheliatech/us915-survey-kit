"""Protocol-agnostic LoRa preamble detector for RTL-SDR .cu8 IQ files.

Usage: python3 tools/lora_detect.py FILE.cu8 --fc HZ --fs HZ [--json out.json] [--bws 62500,125000]

Method: per chunk, one FFT of the whole window; for each BW in 125/250/500 kHz, channels are cut out every BW/2
(critically sampled at BW). Each channel is dechirped for SF7-12 in non-overlapping symbol windows; a LoRa preamble
(>= MIN_RUN consecutive identical upchirps) shows as the same FFT peak bin in consecutive windows.
Detections of one packet in neighbouring channels / aliasing BW-SF pairs are merged, keeping the strongest.
Returns: t (s, preamble start, ~1 symbol accuracy), f_chan (Hz), bw, sf, bin, ratio_db (dechirped peak/mean), run.
"""
import argparse, json, os
import numpy as np
import scipy.fft as sfft  # keeps complex64 (numpy.fft upcasts to complex128, doubling memory traffic)

SFS = range(7, 13)
BWS = (62500, 125000, 250000, 500000)  # 62.5 kHz: MeshCore US preset and Meshtastic narrow presets
MIN_RUN = 5          # consecutive matching windows (preamble has 8 upchirps; misalignment costs one)
EDGE_HZ = 100e3      # keep channels this far inside the RTL anti-alias roll-off at each window edge


def load_cu8(path, start, count):
    raw = np.fromfile(path, dtype=np.uint8, offset=2 * start, count=2 * count)
    x = ((raw.astype(np.float32) - 127.5) / 127.5).view(np.complex64)
    return x - x.mean()  # remove the RTL DC offset


def upchirp(sf):
    n = np.arange(2 ** sf)
    N = 2 ** sf
    return np.exp(1j * np.pi * (n * n / N - n)).astype(np.complex64)  # baseband upchirp at 1 sample per chip


def ratio_threshold_db(sf):
    # noise-only max/mean over N exponential bins is ~ln(N)+0.58; require ~3x that (in dB)
    return 10 * np.log10(3 * (np.log(2 ** sf) + 0.58))


def channel_centres(fs, bw):
    half = fs / 2 - EDGE_HZ - bw / 2
    k = np.arange(-np.floor(half / (bw / 2)), np.floor(half / (bw / 2)) + 1)
    return k * bw / 2  # offsets from fc


def detect_chunk(X, fs, t0, bws=BWS):
    """X: FFT (unshifted) of a chunk sampled at fs. Returns raw detections."""
    L = len(X)
    out = []
    for bw in bws:
        nb = int(round(L * bw / fs))
        for off in channel_centres(fs, bw):
            k0 = int(round(off * L / fs)) - nb // 2
            idx = np.arange(k0, k0 + nb) % L
            seg = X[idx]  # ascending frequency, centre at nb//2 -> reorder to FFT order for a baseband IFFT at rate ~bw
            y = sfft.ifft(np.concatenate([seg[nb // 2:], seg[:nb // 2]]))
            rate = nb * fs / L
            for sf in SFS:
                N = 2 ** sf
                nsym = len(y) // N
                if nsym < MIN_RUN + 1:
                    continue
                d = y[:nsym * N].reshape(nsym, N) * np.conj(upchirp(sf))
                F = np.abs(sfft.fft(d, axis=1)) ** 2
                pk = F.argmax(axis=1)
                ratio = 10 * np.log10(F.max(axis=1) / (F.mean(axis=1) + 1e-20))
                good = ratio > ratio_threshold_db(sf)
                same = np.zeros(nsym, bool)
                same[1:] = (np.abs(((pk[1:] - pk[:-1] + N // 2) % N) - N // 2) <= 1) & good[1:] & good[:-1]
                i = 1
                while i < nsym:
                    if same[i]:
                        j = i
                        while j < nsym and same[j]:
                            j += 1
                        run = j - i + 1
                        if run >= MIN_RUN:
                            out.append(dict(t=round(t0 + (i - 1) * N / rate, 4), f_off=off, bw=bw, sf=sf,
                                            bin=int(pk[i]), ratio_db=round(float(np.median(ratio[i - 1:j])), 1),
                                            run=int(run), dur=(j - i + 1) * N / rate))
                        i = j
                    else:
                        i += 1
    return out


def merge(dets):
    """Keep the strongest detection among those overlapping in time and frequency (same packet seen several ways:
    neighbouring channels, and BW/SF pairs with the same chirp slope, e.g. 125k/SF7 inside a 250k/SF9 packet).
    A weaker detection is dropped if it starts within ~30 symbols after a stronger one's preamble, at an overlapping
    frequency. The decoder is the final judge, so a ghost that slips through only costs one decode attempt.
    A dropped detection with a different BW/SF but the same chirp slope (bw^2 / 2^SF) is kept in the winner's 'alts'
    list: the strongest of such a pair is not always the real mode, so the decoder tries the alternatives when the
    winner gives no header."""
    dets = sorted(dets, key=lambda d: -d['ratio_db'])
    kept = []
    for d in dets:
        clash = False
        for k in kept:
            ksym = 2 ** k['sf'] / k['bw']
            t_ov = k['t'] - 2 * ksym <= d['t'] <= k['t'] + k['dur'] + 30 * ksym
            f_ov = abs(d['f_off'] - k['f_off']) <= (d['bw'] + k['bw']) / 2
            if t_ov and f_ov:
                clash = True
                same_slope = abs(d['bw'] ** 2 / 2 ** d['sf'] - k['bw'] ** 2 / 2 ** k['sf']) < 1e-6 * k['bw'] ** 2 / 2 ** k['sf']
                alts = k.setdefault('alts', [])
                if same_slope and (d['bw'], d['sf']) != (k['bw'], k['sf']) and all((a['bw'], a['sf']) != (d['bw'], d['sf']) for a in alts):
                    alts.append({x: d[x] for x in ('t', 'f_off', 'bw', 'sf', 'bin', 'ratio_db', 'run', 'dur')})
                break
        if not clash:
            kept.append(d)
    return sorted(kept, key=lambda d: d['t'])


def _chunk_job(args):
    path, fs, s0, n, chunk, last, bws = args
    x = load_cu8(path, s0, n)
    # zero-pad to L = 96*q (q 5-smooth): at fs 2.4 MS/s the 125/250/500k channel slices are then 5q/10q/20q bins,
    # all fast FFT sizes (an awkward L makes the per-channel IFFTs near-prime and several times slower)
    q = sfft.next_fast_len(-(-len(x) // 96))
    X = sfft.fft(x, n=96 * q)
    # detections starting in the overlap belong to the next chunk
    return [d for d in detect_chunk(X, fs, s0 / fs, bws) if d['t'] < (s0 + chunk) / fs or last]


def detect_file(path, fc, fs, chunk_s=4.0, overlap_s=0.6, workers=None, bws=BWS):  # overlap > longest preamble (SF12/62.5k: 0.52 s)
    from multiprocessing import Pool
    total = np.memmap(path, dtype=np.uint8, mode='r').size // 2
    chunk, ov = int(chunk_s * fs), int(overlap_s * fs)
    jobs = [(path, fs, s0, min(chunk + ov, total - s0), chunk, s0 + chunk + ov >= total, tuple(bws)) for s0 in range(0, total, chunk)]
    with Pool(workers or max(1, os.cpu_count() // 2)) as pool:  # physical cores; FFTs gain nothing from SMT
        dets = [d for part in pool.map(_chunk_job, jobs) for d in part]
    merged = merge(dets)
    for d in merged:
        d['f_chan'] = fc + d.pop('f_off')
        for alt in d.get('alts', []):
            alt['f_chan'] = fc + alt.pop('f_off')
    return merged, len(dets)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file'); ap.add_argument('--fc', type=float, required=True); ap.add_argument('--fs', type=float, required=True)
    ap.add_argument('--json')
    ap.add_argument('--bws', help='comma-separated channel bandwidths in Hz (default 62500,125000,250000,500000)')
    a = ap.parse_args()
    bws = tuple(int(float(b)) for b in a.bws.split(',')) if a.bws else BWS
    dets, nraw = detect_file(a.file, a.fc, a.fs, bws=bws)
    print(f"{nraw} raw detections -> {len(dets)} after merging")
    for d in dets:
        print(f"t={d['t']:8.3f}s f={d['f_chan']/1e6:9.4f}MHz bw={d['bw']//1000:3d}k sf={d['sf']:2d} run={d['run']:2d} ratio={d['ratio_db']:5.1f}dB")
    if a.json:
        json.dump(dets, open(a.json, 'w'), indent=0)


if __name__ == '__main__':
    main()
