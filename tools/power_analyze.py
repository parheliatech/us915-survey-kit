"""Analyse one or more rtl_power CSV files from the same survey (a survey interrupted and resumed leaves several).

Usage: python3 tools/power_analyze.py OUT_DIR FILE.csv [FILE2.csv ...] [--thresh-db 8] [--chan-khz 125]

A bin counts as active in a sweep when it is more than --thresh-db above that bin's noise floor, where the floor is the
bin's median over the surrounding clock hour (rolling floor, so slow daily changes in the floor do not count as activity).
Narrowband carriers are found separately, from 10-minute median levels: a bin more than 6 dB above its neighbours in a
block, but not in most blocks (which would be a receiver spur).
Writes OUT_DIR/power_summary.json and OUT_DIR/power_waterfall.png.
Occupancy is the share of sweeps in which a channel was active; it is not airtime (each frequency is watched for only
part of every sweep).
"""
import argparse, datetime as dt, json, os, sys
import numpy as np, warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)  # all-NaN bins at band edges

# Channel-plan groups for the US 902-928 MHz band (LoRaWAN: RP002-1.0.3 US902-928; Meshtastic: firmware MeshRadio.h,
# freq = 902.0 + bw/2 + n*bw; MeshCore: published US preset)
GROUPS = [('LoRaWAN uplink 125 kHz, sub-band %d' % (sb + 1), [(902.3 + 0.2 * (8 * sb + k), 125) for k in range(8)]) for sb in range(8)]
GROUPS += [('LoRaWAN uplink 500 kHz', [(903.0 + 1.6 * n, 500) for n in range(8)]),
           ('LoRaWAN downlink / beacon 500 kHz', [(923.3 + 0.6 * n, 500) for n in range(8)]),
           ('Meshtastic LongFast default slot (906.875 MHz)', [(906.875, 250)]),
           ('MeshCore US preset (910.525 MHz)', [(910.525, 62.5)])]


def load(paths):
    rows = []
    for path in paths:
        for line in open(path):
            p = [x.strip() for x in line.split(',')]
            if len(p) < 7:
                continue
            try:
                t = dt.datetime.strptime(p[0] + ' ' + p[1], '%Y-%m-%d %H:%M:%S').timestamp()
                rows.append((t, float(p[2]), float(p[4]), np.array(p[6:], dtype=np.float32)))
            except ValueError:
                continue
    if not rows:
        sys.exit('no usable rows')
    rows.sort(key=lambda r: r[0])
    f0 = min(r[1] for r in rows); step = float(np.median([r[2] for r in rows]))
    fmax = max(r[1] + r[2] * len(r[3]) for r in rows)
    nb = int(round((fmax - f0) / step)) + 1
    sweep_t, cur = [], -1
    for r in rows:  # a sweep restarts at f_low = f0
        if abs(r[1] - f0) < 1 or cur < 0:
            cur += 1; sweep_t.append(r[0])
    M = np.full((len(sweep_t), nb), np.nan, np.float32)
    cur = -1
    for r in rows:
        if abs(r[1] - f0) < 1 or cur < 0:
            cur += 1
        i0 = int(round((r[1] - f0) / step)); n = min(len(r[3]), nb - i0)
        M[cur, i0:i0 + n] = r[3][:n]
    return np.array(sweep_t), f0 + step * np.arange(nb), step, M


def rolling_floor(t, M):
    """Per-bin median over each clock hour of the run, so the floor follows slow drifts."""
    F = np.empty_like(M)
    hb = ((t - t[0]) // 3600).astype(int)
    for h in np.unique(hb):
        sel = hb == h
        F[sel] = np.nanmedian(M[sel], axis=0)
    return F


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out_dir'); ap.add_argument('csv', nargs='+')
    ap.add_argument('--thresh-db', type=float, default=8.0); ap.add_argument('--chan-khz', type=float, default=125.0)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    t, freqs, step, M = load(a.csv)
    ns, nb = M.shape
    floor = rolling_floor(t, M)
    act = (M - floor) > a.thresh_db
    act[np.isnan(M)] = False
    hours = [dt.datetime.fromtimestamp(x).hour for x in t]
    hours = np.array(hours)

    def hourly(mask):  # % of sweeps active, per local clock hour 0-23 (None where the hour was not observed)
        return [round(100 * float(mask[hours == h].mean()), 2) if (hours == h).any() else None for h in range(24)]

    cb = max(1, int(round(a.chan_khz * 1e3 / step)))
    chans = []
    for c in range(0, nb, cb):
        sl = slice(c, min(c + cb, nb))
        am = act[:, sl].any(axis=1)
        chans.append(dict(mhz=round(float(freqs[sl].mean()) / 1e6, 4), occupancy_pct=round(100 * float(am.mean()), 2),
                          peak_db=round(float(np.nanmax(M[:, sl])), 1), floor_db=round(float(np.nanmedian(floor[:, sl])), 1)))

    def idx(c_mhz, bw_khz):
        return np.flatnonzero(np.abs(freqs - c_mhz * 1e6) <= bw_khz * 500)
    groups = []
    for name, chs in GROUPS:
        m = np.zeros(ns, bool)
        per = []
        for c, bw in chs:
            i = idx(c, bw)
            if len(i):
                mi = act[:, i].any(axis=1); m |= mi
                per.append((c, 100 * float(mi.mean())))
        if per:
            best = max(per, key=lambda x: x[1])
            groups.append(dict(name=name, occupancy_pct=round(100 * float(m.mean()), 2), busiest_mhz=round(best[0], 3),
                               busiest_pct=round(best[1], 2), hourly=hourly(m)))
    # wideband events: >= 40 contiguous active bins (~1 MHz at 25 kHz bins) in one sweep
    run_len = np.zeros(ns, int)
    wide_lo = np.full(ns, np.nan)
    for s in range(ns):
        a_ = act[s].astype(np.int8)
        if not a_.any():
            continue
        d = np.diff(np.concatenate([[0], a_, [0]]))
        st, en = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        k = (en - st).argmax()
        run_len[s] = en[k] - st[k]
        wide_lo[s] = freqs[st[k]]
    wide = run_len >= 40
    # narrowband carriers: per 10-minute block, the bin's median level vs its neighbours 2-4 bins away
    occ_bin = act.mean(axis=0)
    blk = ((t - t[0]) // 600).astype(int)
    ublk = np.unique(blk)
    L = np.array([np.nanmedian(M[blk == b], axis=0) for b in ublk])
    nbr = np.full_like(L, np.nan)
    for i in range(4, nb - 4):
        nbr[:, i] = np.nanmax(L[:, [i - 4, i - 3, i - 2, i + 2, i + 3, i + 4]], axis=1)
    excess = L - nbr
    hit = excess > 6
    nb_cands = []
    for i in np.flatnonzero(hit.any(axis=0)):
        if hit[:, i].mean() > 0.5 or (i > 0 and hit[:, i - 1].sum() >= hit[:, i].sum()) or (i + 1 < nb and hit[:, i + 1].sum() > hit[:, i].sum()):
            continue  # a spur present most of the time, or not the strongest bin of a wider hit
        hb_ = ublk[hit[:, i]]
        nb_cands.append(dict(mhz=round(float(freqs[i]) / 1e6, 4), blocks=int(hit[:, i].sum()), blocks_total=len(ublk),
                             first=dt.datetime.fromtimestamp(t[blk == hb_[0]][0]).strftime('%Y-%m-%d %H:%M'),
                             last=dt.datetime.fromtimestamp(t[blk == hb_[-1]][-1]).strftime('%Y-%m-%d %H:%M'),
                             max_excess_db=round(float(np.nanmax(excess[:, i])), 1), occupancy_pct=round(100 * float(occ_bin[i]), 2),
                             hourly=hourly(act[:, i])))
    nb_cands.sort(key=lambda c: -c['max_excess_db'])
    S = dict(files=[os.path.basename(c) for c in a.csv], start=dt.datetime.fromtimestamp(t[0]).isoformat(timespec='minutes'),
             end=dt.datetime.fromtimestamp(t[-1]).isoformat(timespec='minutes'), hours=round((t[-1] - t[0]) / 3600, 2),
             sweeps=ns, sweep_interval_s=round(float(np.median(np.diff(t))), 2) if ns > 1 else None,
             band_mhz=[round(freqs[0] / 1e6, 3), round(freqs[-1] / 1e6, 3)], bin_khz=round(step / 1e3, 3),
             threshold_db=a.thresh_db, floor='rolling hourly median per bin', noise_floor_db=round(float(np.nanmedian(floor)), 1),
             band_active_pct=round(100 * float(act.any(axis=1).mean()), 2), band_hourly=hourly(act.any(axis=1)),
             channel_khz=a.chan_khz, channels=chans, groups=groups,
             wideband=dict(pct=round(100 * float(wide.mean()), 2), hourly=hourly(wide),
                           low_edge_mhz_median=round(float(np.nanmedian(wide_lo[wide])) / 1e6, 2) if wide.any() else None),
             narrowband=nb_cands[:8])
    json.dump(S, open(os.path.join(a.out_dir, 'power_summary.json'), 'w'), indent=1)
    try:
        import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
        fig, ax = plt.subplots(2, 1, figsize=(11, 7), gridspec_kw=dict(height_ratios=[3, 1.3]), sharex=True)
        step_s = max(1, ns // 2000)
        ax[0].imshow(M[::step_s], aspect='auto', origin='lower', extent=[freqs[0] / 1e6, freqs[-1] / 1e6, 0, (t[-1] - t[0]) / 3600],
                     cmap='viridis', vmin=np.nanpercentile(M, 5), vmax=np.nanpercentile(M, 99.9))
        ax[0].set_ylabel('hours from start'); ax[0].set_title('Power survey waterfall (dB)')
        ax[1].plot(freqs / 1e6, 100 * occ_bin, lw=0.8)
        ax[1].set_xlabel('MHz'); ax[1].set_ylabel('% of sweeps active')
        plt.tight_layout(); plt.savefig(os.path.join(a.out_dir, 'power_waterfall.png'), dpi=110)
    except Exception as e:
        print('plot skipped:', e)
    print(f"{ns} sweeps over {S['hours']} h; band active {S['band_active_pct']}% of sweeps; wideband events {S['wideband']['pct']}%")


if __name__ == '__main__':
    main()
