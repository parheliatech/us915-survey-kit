"""Collect every decoder output of a survey run into one RUN_DIR/summary.json for the report.

Usage: python3 tools/summarize.py RUN_DIR

Reads RUN_DIR/site.json (site name, notes, the PPM the decoders used), RUN_DIR/power/power_summary.json
(if the power survey ran) and RUN_DIR/dwells/* (one set of files per IQ dwell, see process_dwell.py).
All frequencies in the summary are corrected for the dongle's frequency error. The error is taken from decoded LoRa
frames of plans with a known channel raster when there are enough of them, otherwise from survey.conf PPM (default 0).
Nothing here identifies a protocol from frequency alone: LoRa classes come from decoded headers (sync word + structure),
802.15.4g from the SFD and frame check, other FSK only as measured families (rate, deviation, grid).
"""
import collections, glob, json, math, os, statistics, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Meshtastic presets: firmware src/mesh/MeshRadio.h modemPresetToParams (bw kHz, sf, cr 4/x)
MESHTASTIC_PRESETS = {(250, 11): 'LongFast', (250, 10): 'MediumSlow', (250, 9): 'MediumFast', (250, 8): 'ShortSlow',
                      (250, 7): 'ShortFast', (500, 7): 'ShortTurbo', (500, 11): 'LongTurbo', (125, 11): 'LongModerate',
                      (125, 12): 'LongSlow'}
MESHCORE_US_PRESET = dict(mhz=910.525, bw=62500, sf=7)
GPS_UTC_OFFSET = 18  # leap seconds since 1980 (unchanged since 2017)
GPS_EPOCH = 315964800  # 1980-01-06 in Unix seconds


def jl(path):
    out = []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def jload(path, default=None):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def load_oui(path='/usr/share/ieee-data/oui.txt'):
    d = {}
    try:
        for line in open(path, encoding='utf-8', errors='replace'):
            if '(base 16)' in line:
                k, v = line.split('(base 16)')
                d[k.strip().upper()] = v.strip()
    except OSError:
        pass
    return d


def local(ts):
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


# ---------------------------------------------------------------- frequency calibration
def nominal_raster(rec, f):
    """Nominal channel of a decoded frame whose plan has a fixed raster, else None. f = roughly corrected Hz."""
    bw, proto = rec['bw'], rec.get('proto') or ''
    if proto.startswith('Meshtastic'):
        step = bw
        return 902.0e6 + bw / 2 + round((f - 902.0e6 - bw / 2) / step) * step
    if proto == 'LoRaWAN-beacon' or (proto.startswith('LoRaWAN') and f > 923e6):
        return 923.3e6 + round((f - 923.3e6) / 0.6e6) * 0.6e6
    if proto.startswith('LoRaWAN') and bw == 125000:
        return 902.3e6 + round((f - 902.3e6) / 0.2e6) * 0.2e6
    if proto.startswith('LoRaWAN') and bw == 500000:
        return 903.0e6 + round((f - 903.0e6) / 1.6e6) * 1.6e6
    return None


def calibrate(frames, prior):
    est = []
    for r in frames:
        if not r.get('crc_ok') or not r.get('f_est'):
            continue
        f_rough = r['f_est'] * (1 - prior * 1e-6)
        nom = nominal_raster(r, f_rough)
        if nom and abs(f_rough - nom) < 15e3:
            est.append((r['f_est'] - nom) / nom * 1e6)
    if len(est) >= 5:
        q = statistics.quantiles(est, n=4)
        return dict(ppm=round(statistics.median(est), 2), source=f'{len(est)} decoded LoRa frames on known channel rasters',
                    iqr=round(q[2] - q[0], 2), n=len(est), prior=prior)
    return dict(ppm=prior, source='survey.conf PPM' if prior else 'not measured: too few decoded frames on known channels (0 assumed)', n=len(est), prior=prior)


# ---------------------------------------------------------------- LoRa
def lora_label(r):
    p = r.get('proto') or ''
    bwk = r['bw'] // 1000 if r['bw'] % 1000 == 0 else r['bw'] / 1000
    if p == 'LoRaWAN-beacon':
        return 'LoRaWAN Class B beacon'
    if p.startswith('Meshtastic'):
        name = MESHTASTIC_PRESETS.get((int(r['bw'] / 1000), r['sf']))
        return f"Meshtastic {name}" if name else f"Meshtastic (custom {bwk} kHz / SF{r['sf']})"
    if p.startswith('MeshCore'):
        return 'MeshCore'
    if p.startswith('LoRaWAN'):
        return 'LoRaWAN downlink' if 'Down' in (r.get('mtype') or '') or 'Accept' in (r.get('mtype') or '') else 'LoRaWAN uplink'
    return f"LoRa, sync word {r.get('sync')}"


def summarize_lora(dwells, ppm):
    dets_all, frames = [], []
    for d in dwells:
        for i, det in enumerate(d['det']):
            det = dict(det, dwell=d['name'], ts=d['t0'] + det['t'], visit=d['t0'], window=d['window'], det_index=i)
            dets_all.append(det)
        for r in d['lora']:
            r = dict(r, dwell=d['name'], visit=d['t0'], window=d['window'])
            frames.append(r)
    corr = lambda f: f * (1 - ppm * 1e-6) if f else None
    by_det = collections.defaultdict(list)
    for r in frames:
        by_det[(r['dwell'], r.get('det_index'))].append(r)
    # Classes: CRC-valid frames first, grouped by protocol label and mode, with frequencies clustered (a gap of more
    # than bw/8 starts a new class; LoRaWAN uplinks hop, so they stay one class per mode). Header-only frames (CRC
    # failed: their sync word and payload are unreliable) then join the nearest class of the same mode within bw/4.
    groups = collections.defaultdict(list)
    for r in frames:
        if r.get('header_ok') and r.get('crc_ok'):
            r['_f'] = corr(r.get('f_est') or r['f_chan'])
            groups[(lora_label(r), r['bw'], r['sf'])].append(r)
    classes = []
    for (label, bw, sf), rs in groups.items():
        rs.sort(key=lambda r: r['_f'])
        cur = []
        for r in rs:
            if cur and label != 'LoRaWAN uplink' and r['_f'] - cur[-1]['_f'] > bw / 8:
                classes.append(dict(label=label, bw=bw, sf=sf, frames=cur)); cur = []
            cur.append(r)
        classes.append(dict(label=label, bw=bw, sf=sf, frames=cur))
    for r in frames:
        if not r.get('header_ok') or r.get('crc_ok'):
            continue
        r['_f'] = corr(r.get('f_est') or r['f_chan'])
        near = [c for c in classes if (c['bw'], c['sf']) == (r['bw'], r['sf']) and
                (c['label'] == 'LoRaWAN uplink' or min(abs(x['_f'] - r['_f']) for x in c['frames']) <= r['bw'] / 4)]
        if near:
            near[0]['frames'].append(r)
        else:
            label = lora_label(r)
            same = [c for c in classes if c['label'] == label and (c['bw'], c['sf']) == (r['bw'], r['sf'])
                    and min(abs(x['_f'] - r['_f']) for x in c['frames']) <= r['bw'] / 4]
            if same:
                same[0]['frames'].append(r)
            else:
                classes.append(dict(label=label, bw=r['bw'], sf=r['sf'], frames=[r]))
    for c in classes:
        c['freqs'] = [r['_f'] for r in c['frames']]
    out_classes = []
    for c in sorted(classes, key=lambda c: (-sum(bool(r.get('crc_ok')) for r in c['frames']), -len(c['frames']))):
        label, bw, sf, fr = c['label'], c['bw'], c['sf'], c['frames']
        ok = [r for r in fr if r.get('crc_ok')]
        visits = collections.Counter(r['visit'] for r in ok)
        cls = dict(label=label, bw=bw, sf=sf, mhz=round(med(c['freqs']) / 1e6, 4), mhz_min=round(min(c['freqs']) / 1e6, 4),
                   mhz_max=round(max(c['freqs']) / 1e6, 4), header_ok=len(fr), crc_ok=len(ok),
                   syncs=dict(collections.Counter(r.get('sync') for r in (ok or fr)).most_common(3)),
                   crs=dict(collections.Counter(r.get('cr') for r in fr).most_common(3)),
                   snr_min=min(r['snr'] for r in fr), snr_max=max(r['snr'] for r in fr),
                   first=local(min(r['ts'] for r in fr)), last=local(max(r['ts'] for r in fr)),
                   per_visit=sorted([v, n] for v, n in visits.items()))
        if label.startswith('Meshtastic'):
            senders = {r.get('sender') for r in ok}
            pk = {(r.get('sender'), r.get('packet_id')) for r in ok}
            hops = [r['hop_start'] - r['hop_limit'] for r in ok if r.get('hop_start') is not None and r['hop_start'] >= r['hop_limit']]
            n_slot = round((cls['mhz'] * 1e6 - 902.0e6 - bw / 2) / bw) + 1
            cls.update(nodes=len(senders), packets=len(pk), broadcast=sum(r.get('dest') == 'ffffffff' for r in ok),
                       want_ack=sum(bool(r.get('want_ack')) for r in ok),
                       channel_hashes=dict(collections.Counter(r.get('channel_hash') for r in ok).most_common(8)),
                       hops=dict(sorted(collections.Counter(hops).items())),
                       relays=dict(collections.Counter(r.get('relay_node') for r in ok if r.get('relay_node')).most_common(5)),
                       slot=n_slot)
        elif label == 'MeshCore':
            mc = [r for r in ok if r.get('mc_type')]
            hop_list = [r.get('hops', 0) for r in mc]
            relays = {h for r in mc for h in r.get('path', [])}
            cls.update(valid_structure=len(mc), routes=dict(collections.Counter(r['mc_route'] for r in mc)),
                       types=dict(collections.Counter(r['mc_type'] for r in mc).most_common()),
                       hops_min=min(hop_list) if hop_list else None, hops_max=max(hop_list) if hop_list else None,
                       hops_median=med(hop_list), relay_hashes=len(relays),
                       last_hop=dict(collections.Counter(r['path'][-1] for r in mc if r.get('path')).most_common(5)),
                       src_hashes=len({r.get('src_hash') for r in mc if r.get('src_hash')}),
                       dest_top=dict(collections.Counter(r.get('dest_hash') for r in mc if r.get('dest_hash')).most_common(3)),
                       channel_hashes=dict(collections.Counter(r.get('channel_hash') for r in mc if r.get('channel_hash') is not None).most_common(5)),
                       adverts=sum(r['mc_type'] == 'Advert' for r in mc),
                       is_us_preset=abs(cls['mhz'] - MESHCORE_US_PRESET['mhz']) < 0.02 and bw == MESHCORE_US_PRESET['bw'] and sf == MESHCORE_US_PRESET['sf'])
        elif label.startswith('LoRaWAN') and label != 'LoRaWAN Class B beacon':
            cls.update(mtypes=dict(collections.Counter(r.get('mtype') for r in ok)),
                       dev_addrs=len({r.get('dev_addr') for r in ok if r.get('dev_addr')}),
                       dev_addr_prefixes=dict(collections.Counter((r.get('dev_addr') or '')[:2] for r in ok if r.get('dev_addr')).most_common(5)),
                       joins=sum(r.get('mtype') == 'JoinRequest' for r in ok),
                       channels=sorted({round(corr(r.get('f_est') or r['f_chan']) / 1e5) / 10 for r in ok}))
        elif label == 'LoRaWAN Class B beacon':
            offs, chans = [], collections.Counter()
            for r in ok:
                if r.get('beacon_gps_time') is not None:
                    utc = GPS_EPOCH + r['beacon_gps_time'] - GPS_UTC_OFFSET
                    offs.append(r['ts'] - utc)
                chans[round(corr(r.get('f_est') or r['f_chan']) / 1e5) / 10] += 1
            hopping = None
            if len(ok) >= 2:  # RP002: channel = floor(time / 128) mod 8; consecutive beacons should change channel
                ts_sorted = sorted(r['beacon_gps_time'] for r in ok if r.get('beacon_gps_time') is not None)
                pairs = [(a, b) for a, b in zip(ts_sorted, ts_sorted[1:]) if b - a == 128]
                hopping = False if pairs and len(chans) == 1 else (True if len(chans) > 1 else None)
            locs = {(r.get('beacon_lat'), r.get('beacon_lng')) for r in ok if r.get('beacon_crc2_ok')}
            cls.update(time_offset_s=[round(min(offs), 2), round(max(offs), 2)] if offs else None, channels=dict(chans),
                       hopping=hopping, gw_location=[(round(a, 2), round(b, 2)) for a, b in locs if a or b] or None)
        out_classes.append(cls)
    # detections without any decoded header: undecoded LoRa activity
    decoded_dets = {k for k, rs in by_det.items() if any(r.get('header_ok') for r in rs)}
    und = collections.defaultdict(list)
    for d in dets_all:
        if (d['dwell'], d['det_index']) in decoded_dets:
            continue
        f = corr(d['f_chan'])
        und[(d['bw'], d['sf'], round(f / 25e3) * 25e3)].append(d)
    undecoded = []
    for (bw, sf, f), ds in sorted(und.items(), key=lambda kv: -len(kv[1])):
        slope = bw * bw / 2 ** sf
        ghost_of = same_mode = None
        for c in out_classes:
            if abs(c['mhz'] * 1e6 - f) < max(bw, c['bw']):
                if (c['bw'], c['sf']) == (bw, sf):
                    same_mode = same_mode or c['label']
                elif abs(c['bw'] ** 2 / 2 ** c['sf'] - slope) < 1:
                    ghost_of = ghost_of or c['label']
        undecoded.append(dict(bw=bw, sf=sf, mhz=round(f / 1e6, 3), detections=len(ds),
                              ratio_db_median=round(med([d['ratio_db'] for d in ds]), 1),
                              first=local(min(d['ts'] for d in ds)), last=local(max(d['ts'] for d in ds)),
                              visits=len({d['visit'] for d in ds}), same_slope_as=ghost_of, same_mode_as=same_mode))
    return dict(detections=len(dets_all), frames=len(frames), header_ok=sum(bool(r.get('header_ok')) for r in frames),
                crc_ok=sum(bool(r.get('crc_ok')) for r in frames), classes=out_classes, undecoded=undecoded,
                detections_by_mode=dict(collections.Counter(f"{d['bw'] // 1000 if d['bw'] % 1000 == 0 else d['bw'] / 1000} kHz / SF{d['sf']}" for d in dets_all).most_common()))


# ---------------------------------------------------------------- 802.15.4g SUN-FSK
def channel_grid(ns):
    """ns: channel numbers on the 902.2 + 0.2n grid. Describe the grid they actually use."""
    if len(ns) < 20:
        return None
    for step, offs in ((3, range(3)), (2, range(2))):
        cnt = collections.Counter(n % step for n in ns)
        k, v = cnt.most_common(1)[0]
        if v / len(ns) >= 0.9:
            base = 902.2 + 0.2 * k
            return dict(spacing_khz=200 * step, base_mhz=round(base, 1), share=round(v / len(ns), 3),
                        text=f"{base:.1f} + {0.2 * step:.1f}n MHz")
    return dict(spacing_khz=200, base_mhz=902.2, share=1.0, text='902.2 + 0.2n MHz')


def summarize_wisun(dwells, oui):
    recs = []
    for d in dwells:
        for r in d['wisun']:
            recs.append(dict(r, visit=d['t0'], window=d['window'], dwell=d['name'], fc=d['fc']))
    if not recs:
        return None
    ok = [r for r in recs if r.get('ok')]
    shape = lambda r: f"{r.get('hdr3', '')[:6]}/{r['len']}" if r['len'] <= 40 else None
    shapes = collections.Counter(shape(r) for r in ok if shape(r))
    # short fixed-length frames with an identical 3-byte start are a separate (non-802.15.4 MAC) format if common
    short_fmt = {s for s, n in shapes.items() if n >= max(20, 0.1 * len(ok))}
    is_short = lambda r: shape(r) in short_fmt
    mac = [r for r in ok if not is_short(r)]
    eui = collections.Counter()
    for r in mac:
        for k in ('src', 'dst'):
            v = r.get(k) or ''
            if len(v) == 16:
                eui[v] += 1
    vendors = collections.Counter(oui.get(e[:6].upper(), 'unregistered or random') for e in eui)
    pans = collections.defaultdict(lambda: [0, set()])
    for r in mac:
        if r.get('mac_type') in ('Data', 'Cmd') and r.get('dst_pan'):
            pans[r['dst_pan']][0] += 1
            if r.get('src'):
                pans[r['dst_pan']][1].add(r['src'])
    srcs = collections.Counter(r.get('src') for r in mac if r.get('src'))
    top = srcs.most_common(1)
    top_info = None
    if top:
        s = top[0][0]
        sr = [r for r in mac if r.get('src') == s]
        top_info = dict(src=':'.join(s[i:i + 2] for i in range(0, len(s), 2)).upper(), frames=len(sr),
                        share=round(len(sr) / len(mac), 3), channels=len({r['f_nominal'] for r in sr}),
                        windows=len({r['window'] for r in sr}), pans=sorted({r.get('dst_pan') for r in sr if r.get('dst_pan')}))
    ch_all = lambda rs: [round((r['f_nominal'] - 902.2e6) / 200e3) for r in rs]
    ie = [r for r in recs if r.get('ie_present')]
    types = collections.Counter((r.get('mac_type'), r.get('frame_ver')) for r in mac)
    type_rows = []
    for (t, v), n in types.most_common():
        rs = [r for r in mac if (r.get('mac_type'), r.get('frame_ver')) == (t, v)]
        lens = [r['len'] for r in rs]
        type_rows.append(dict(type=t, frame_ver=v, frames=n, len_min=min(lens), len_max=max(lens),
                              secured=sum(bool(r.get('security')) for r in rs), channels=len({r['f_nominal'] for r in rs}),
                              mhz_min=round(min(r['f_nominal'] for r in rs) / 1e6, 1), mhz_max=round(max(r['f_nominal'] for r in rs) / 1e6, 1)))
    short_rows = []
    for s in sorted(short_fmt):
        rs = [r for r in ok if shape(r) == s]
        short_rows.append(dict(start=s.split('/')[0], len=int(s.split('/')[1]), frames=len(rs), distinct=len({r['sha8'] for r in rs}),
                               channels=len({r['f_nominal'] for r in rs}), mhz_min=round(min(r['f_nominal'] for r in rs) / 1e6, 1),
                               mhz_max=round(max(r['f_nominal'] for r in rs) / 1e6, 1), grid=channel_grid(ch_all(rs))))
    per_channel = collections.Counter(round(r['f_nominal'] / 1e5) / 10 for r in ok)
    per_window = {}
    for w in sorted({r['window'] for r in recs}):
        rw = [r for r in recs if r['window'] == w]
        per_window[w] = dict(sfd=len(rw), ok=sum(bool(r['ok']) for r in rw))
    per_visit = collections.Counter(round(r['visit']) for r in ok)
    return dict(sfd=len(recs), ok=len(ok), ok_pct=round(100 * len(ok) / len(recs), 1),
                mac_frames=len(mac), distinct_eui64=len(eui), vendors=dict(vendors.most_common(6)),
                pans=sorted([dict(pan=p, frames=v[0], sources=len(v[1])) for p, v in pans.items()], key=lambda x: -x['frames']),
                top_source=top_info, frame_types=type_rows, short_formats=short_rows,
                mac_grid=channel_grid(ch_all([r for r in recs if not is_short(r)])),
                ie_frames=len(ie), ie_ok=sum(bool(r['ok']) for r in ie),
                ie_struct_ok=sum(bool(r.get('ie_struct_ok')) for r in ie),
                non_ie_frames=len(recs) - len(ie), non_ie_ok=sum(bool(r['ok']) for r in recs if not r.get('ie_present')),
                netnames=sorted({r['netname'] for r in ok if r.get('netname')}),
                wisun_utt=dict(collections.Counter(r['wisun_type'] for r in ok if r.get('wisun_type'))),
                per_channel=sorted([k, v] for k, v in per_channel.items()), per_window=per_window,
                per_visit=sorted([k, v] for k, v in per_visit.items()))


# ---------------------------------------------------------------- other FSK
def lora_airtime(sf, bw, nbytes):
    de = 1 if (2 ** sf / bw) > 0.016 else 0
    nsym = 8 + max(math.ceil((8 * nbytes - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * 5, 0)
    return (12.25 + nsym) * 2 ** sf / bw


def summarize_fsk(dwells, ppm):
    """FSK families from the burst survey. Bursts that overlap a LoRa preamble in time and frequency are LoRa chirps
    seen by the FSK burst finder and are left out."""
    bursts, lora_hits = [], 0
    for d in dwells:
        spans = []
        plen = collections.defaultdict(list)
        for r in d['lora']:
            if r.get('pay_len') and r.get('header_ok'):
                plen[r.get('det_index')].append(r['pay_len'])
        for i, det in enumerate(d['det']):
            nbytes = max(plen.get(i) or [64])
            spans.append((det['t'] - 0.01, det['t'] + lora_airtime(det['sf'], det['bw'], nbytes) + 0.01, det['f_chan'], det['bw']))
        for b in d['fsk']:
            if any(t0 <= b['t'] <= t1 and abs(b['f'] - f) <= bw for t0, t1, f, bw in spans):
                lora_hits += 1
                continue
            bursts.append(dict(b, visit=d['t0'], window=d['window'], ts=d['t0'] + b['t'], fmhz=b['f'] * (1 - ppm * 1e-6) / 1e6))
    if not bursts:
        return dict(bursts=0, lora_excluded=lora_hits, classified=0, measurable=0, families=[])
    # deviation far above the symbol rate = a chirp or an unstable carrier, not FSK data
    good = sorted([b for b in bursts if b.get('line_snr', 0) >= 6 and b.get('baud') and b['dev_hz'] <= 10 * b['baud']], key=lambda b: b['baud'])
    clusters, cur = [], []
    for b in good:  # greedy clustering on symbol rate: neighbours within 6%
        if cur and b['baud'] > cur[-1]['baud'] * 1.06:
            clusters.append(cur); cur = []
        cur.append(b)
    if cur:
        clusters.append(cur)
    fams = []
    minimum = max(10, int(0.02 * len(good)))
    for c in clusters:
        if len(c) < minimum:
            continue
        baud = med([b['baud'] for b in c])
        fmhz = [b['fmhz'] for b in c]
        grid = None
        for sp in (0.5, 0.4, 0.25, 0.2, 0.1):  # coarsest spacing whose channel phase is tight
            ang = [2 * math.pi * f / sp for f in fmhz]
            R = math.hypot(sum(map(math.cos, ang)), sum(map(math.sin, ang))) / len(ang)
            if R > 0.8:
                phase = math.atan2(sum(map(math.sin, ang)), sum(map(math.cos, ang))) / (2 * math.pi) * sp % sp
                base = 902.0 + ((phase - 902.0) % sp)
                grid = f"{base:.2f} + {sp:g}n MHz"
                break
        syncs = collections.Counter(b['after_pre'][:16] for b in c if b.get('pre_len', 0) >= 16 and len(b.get('after_pre', '')) >= 16)
        sync = None
        if syncs:
            s, n = syncs.most_common(1)[0]
            if n >= 0.3 * len(c):
                sync = f"0x{int(s, 2):04X}"
        fams.append(dict(baud=round(baud), dev_hz=round(med([b['dev_hz'] for b in c])), dur_ms=round(med([b['dur_ms'] for b in c]), 1),
                         dur_ms_range=[round(min(b['dur_ms'] for b in c), 1), round(max(b['dur_ms'] for b in c), 1)],
                         bursts=len(c), windows=sorted({b['window'] for b in c}), mhz_min=round(min(fmhz), 2), mhz_max=round(max(fmhz), 2),
                         grid=grid, preamble_bits=round(med([b.get('pre_len', 0) for b in c])), sync16=sync,
                         by_hour=dict(sorted(collections.Counter(datetime.fromtimestamp(b['ts']).hour for b in c).items())),
                         sun_fsk=45e3 <= baud <= 55e3))
    fams.sort(key=lambda f: -f['bursts'])
    for i, f in enumerate(fams):
        f['name'] = f"F{i + 1}"
    return dict(bursts=len(bursts), lora_excluded=lora_hits, classified=sum(f['bursts'] for f in fams), measurable=len(good), families=fams)


# ---------------------------------------------------------------- main
def main():
    run = sys.argv[1]
    site = jload(os.path.join(run, 'site.json'), {})
    prior = site.get('ppm', 0.0) or 0.0  # what the decoders were given (survey.conf PPM)
    dwells = []
    for m in sorted(glob.glob(os.path.join(run, 'dwells', '*.meta.json'))):
        meta = json.load(open(m))
        base = m[:-len('.meta.json')]
        meta.update(det=jload(base + '.det.json', []), lora=jl(base + '.lora.jsonl'), wisun=jl(base + '.wisun.jsonl'),
                    fsk=jload(base + '.fsk.json', []), clip=jload(base + '.clip.json'), done=os.path.exists(base + '.done'),
                    sunfsk_done=os.path.exists(base + '.wisun.jsonl'))
        # two overlapping detections of one packet both decode it: keep the first of each CRC-ok payload within 1 s
        seen, lora = [], []
        for r in meta['lora']:
            if r.get('crc_ok') and any(r.get('sha8') == s and abs(r['t'] - t) < 1 for s, t in seen):
                continue
            if r.get('crc_ok'):
                seen.append((r.get('sha8'), r['t']))
            lora.append(r)
        meta['lora'] = lora
        dwells.append(meta)
    short = len(glob.glob(os.path.join(run, 'dwells', '*.meta.json.short')))
    all_lora = [r for d in dwells for r in d['lora']]
    ppm_info = calibrate(all_lora, prior)
    ppm = ppm_info['ppm']
    windows = collections.Counter(d['window'] for d in dwells)
    fcs = {d['window']: d['fc'] for d in dwells}
    clips = [d['clip'] for d in dwells if d['clip']]
    S = dict(site=site, generated=datetime.now().isoformat(timespec='minutes'), ppm=ppm_info,
             coverage=dict(dwells=len(dwells), processed=sum(d['done'] for d in dwells), short_captures=short,
                           dwell_s=round(med([d['samples'] / 2.4e6 for d in dwells]) or 0, 1),
                           start=local(min(d['t0'] for d in dwells)) if dwells else None,
                           end=local(max(d['t0'] + d['samples'] / 2.4e6 for d in dwells)) if dwells else None,
                           windows={str(round(fcs[w] / 1e6, 3)): n for w, n in sorted(windows.items())},
                           passes=max((d['pass'] for d in dwells), default=-1) + 1,
                           visits=sorted({round(d['t0']) for d in dwells}),
                           window_visits=sorted([d['window'], round(d['t0']), round(fcs[d['window']] / 1e6, 3), d['pass'], d['sunfsk_done']] for d in dwells)),
             receiver=dict(rms_median=med([c['rms'] for c in clips]), files_clipping=sum(c['clip_ppm'] > 0 for c in clips),
                           files_over_100ppm=sum(c['clip_ppm'] > 100 for c in clips),
                           worst_clip_ppm=max((c['clip_ppm'] for c in clips), default=None)) if clips else None,
             lora=summarize_lora(dwells, ppm) if dwells else None,
             sunfsk=summarize_wisun(dwells, load_oui()) if dwells else None,
             fsk=summarize_fsk(dwells, ppm) if dwells else None,
             power=jload(os.path.join(run, 'power', 'power_summary.json')))
    json.dump(S, open(os.path.join(run, 'summary.json'), 'w'), indent=1, default=list)
    print(f"summary: {len(dwells)} dwells, ppm {ppm} ({ppm_info['source']})")


if __name__ == '__main__':
    main()
