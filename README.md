# 902-928 MHz spectrum survey kit

Survey the US 902-928 MHz ISM band with one RTL-SDR dongle and get a report on what transmits at your location:
LoRa networks (Meshtastic, MeshCore, LoRaWAN including gateway beacons, and any other LoRa), IEEE 802.15.4g
SUN-FSK utility and IoT mesh traffic, other FSK systems, narrowband carriers, and when each one is active.

The kit records for about 36 hours unattended, then writes `report.html`. Every number in the report comes from
your own recordings: nothing is copied from another site.

## What you need

- A PC running **Ubuntu or Debian** (Ubuntu 24.04 or newer recommended), 4 or more CPU cores, 8 GB RAM.
- An **RTL-SDR dongle** (RTL2832U + R820T/R860; an RTL-SDR Blog V3/V4 is ideal) and an antenna for 900 MHz,
  outdoors or high up if possible. A wideband discone or a 915 MHz ground plane both work.
- About **90 GB of free disk**. Recordings wait on disk until they are decoded, up to `IQ_CAP_GB` (80 GB), and are
  deleted once decoded. Set `KEEP_IQ=yes` only if you want to keep them (~200 GB for a full scan).
- An internet connection during installation.

## Install

```sh
tar xzf survey-kit.tar.gz && cd survey-kit
./install.sh
```

`install.sh` installs the system packages with apt (it asks for your password), builds the LoRa decoder
([gr-lora_sdr](https://github.com/tapparelj/gr-lora_sdr) plus the kit's patch) inside the kit folder, and runs a
self-test on synthetic signals. Every line of the self-test must say PASS.

If the dongle was plugged in before installing, unplug it and plug it back in so the new device rules apply.

## Run a survey

1. Edit `survey.conf`: at least `SITE_NAME`, `SITE_LOCATION` (coarse, e.g. a town) and `ANTENNA`.
2. Check the setup:
   ```sh
   ./survey.sh check
   ```
3. Start the survey and leave the PC alone:
   ```sh
   ./survey.sh autostart          # optional: resume by itself after a reboot or power cut
   nohup ./survey.sh new > survey.out 2>&1 &
   ```
4. Check progress at any time with `./survey.sh status`.
5. When it finishes, open `runs/<date>_<site>/report/report.html` in a browser. `report.md` and the chart PNGs
   are in the same folder.

The steps run in this order:

| Step | Default length | What it does |
|---|---|---|
| power | 24 h | `rtl_power` sweeps 902-928 MHz every 2 s. Shows where and when there is energy. |
| iqscan | about 12 h | Records 15 overlapping 2.4 MHz windows in turn, 4 min each, 12 passes. Each recording is decoded right after capture. |
| report | minutes | Combines everything into `summary.json` and `report/report.html`. |

All lengths are set in `survey.conf`. For a quick trial run, set `POWER_HOURS=0.1`, `IQ_PASSES=1` and `DWELL_S=30`.

## If something stops it

Power cuts, reboots and crashes are expected. Run `./survey.sh` again, or let `autostart` do it, and it carries
on where it stopped:
- the power survey records only the hours still missing;
- the IQ scan records only the window visits still missing;
- each recording's decoders skip whatever they already finished;
- a LoRa decoder crash skips the packet it died on and continues.

Other commands:
- `./survey.sh stop`: ask the IQ scan to stop after the current recording.
- `./survey.sh process`: decode recordings still waiting.
- `./survey.sh report`: rebuild the report, for example after changing the site name.
- `./survey.sh selftest`: rerun the synthetic test.

## What the report contains

- **Summary and band map**: what was found and where in the band.
- **Signal inventory**: one row per signal class with modulation, channels, activity and identity.
- **LoRa**: each decoded class (frequency, bandwidth, spreading factor, sync word, counts, SNR), with
  network details where the header allows. Meshtastic gives node and packet counts, channel hashes and hops.
  MeshCore gives route and message types, hops and relays. LoRaWAN gives message types and device-address counts.
  Class B beacons give channel, timing and whether the gateway hops. Preambles that never decoded are listed
  separately.
- **802.15.4g SUN-FSK**: frame counts and decode rate, vendors (from IEEE OUI), PAN IDs, frame types, the channel
  grid in use, and the busiest node.
- **Other FSK families**: symbol rate, deviation, frame length, channel grid and sync pattern, measured blind.
- **Power survey**: busiest channels, activity by hour for each channel-plan group, wideband events and narrowband
  carriers.
- **Time of day**: activity at each visit through the scan.
- **Receiver health**: noise level and clipping, which tells you whether to change the gain.

## Privacy and the law

The kit only receives. It never stores or decrypts payloads. It keeps header fields (addresses, hop counts, frame
types), lengths and short hashes used to spot duplicates.

Device addresses are pseudonymous but persistent, so be careful when sharing `summary.json` and the dwell files.
The report shows only counts and network-level identifiers.

Check your local rules before publishing anything that identifies specific devices.

## Settings worth knowing

- `GAIN` (default 40). If the report shows clipping above about 1000 ppm, a strong transmitter is nearby: try 30.
- `PPM`. The report measures the dongle's frequency error from decoded packets on known channels (Meshtastic,
  LoRaWAN, beacons) and corrects every frequency for it. The decoders work without it for errors up to about
  ±12 ppm. If the report shows a bigger error, put that value in `PPM` and run `./survey.sh process` and
  `./survey.sh report` again (with `KEEP_IQ=yes`), or simply use it for your next survey.
- `MAX_PARALLEL` (default 2) and `IQ_CAP_GB` (default 80). Decoding one 4-minute recording takes longer than
  recording it, so recordings wait on disk in a queue of up to `IQ_CAP_GB`. Capture never waits for the decoders,
  so every window is visited on schedule, and the queue is finished after the last recording. On a 4-core
  i7-6700K one recording takes 4-9 minutes to decode (longer in busy windows), so two decoders keep pace with
  capture and decoding ends within about half an hour of the last recording. On a slower PC the queue grows and
  decoding runs on after the scan; raise `MAX_PARALLEL` only if you have spare cores.

## Files

```
survey.sh             main script (see ./survey.sh help)
survey.conf           your settings (from survey.conf.example)
install.sh            installs packages and builds the decoder
tools/                iqscan, process_dwell, lora_detect, lora_decode, lora_rx, wisun_decode,
                      fsk_survey, power_analyze, summarize, report, selftest (+ loopback_test, synth_window)
patches/              gr-lora_sdr patch: any sync word, plus SNR, sync word and position in the outputs
runs/<run>/           one folder per survey: power/, dwells/, state/, summary.json, report/
```

## Sources for the protocol details the kit relies on

- LoRaWAN US902-928 channels and Class B beacon: LoRa Alliance
  [RP002-1.0.3 Regional Parameters](https://lora-alliance.org/wp-content/uploads/2021/05/RP002-1.0.3-FINAL-1.pdf), section 2.5.
- Meshtastic presets and slot formula: firmware
  [MeshRadio.h](https://github.com/meshtastic/firmware/blob/master/src/mesh/MeshRadio.h) and
  [RadioInterface.cpp](https://github.com/meshtastic/firmware/blob/master/src/mesh/RadioInterface.cpp).
- MeshCore packet format: [meshcore-dev/MeshCore](https://github.com/meshcore-dev/MeshCore), `src/Packet.cpp`.
- IEEE 802.15.4g SUN-FSK PHY (SFD 0x904E, PN9 whitening) and the 802.15.4-2015 MAC.

## License

GPL-3.0 (see `LICENSE`), the same as [gr-lora_sdr](https://github.com/tapparelj/gr-lora_sdr), which `install.sh`
downloads, patches (`patches/gr-lora_sdr-sdrplan.patch`) and builds.
