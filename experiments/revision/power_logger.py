#!/usr/bin/env python3
"""Sample K26 SOM power (INA260, hwmon) and on-chip temperatures while a command runs.

Usage on the KV260:
  sudo python3 power_logger.py --out idle.csv --idle 60
  sudo python3 power_logger.py --rt --child-nice -20 --out cpu4_k64.csv -- ./revbench --mode cpu4 ...

--rt runs the logger as SCHED_FIFO (priority 60) so that it keeps sampling when all four cores are busy;
the command is reset to SCHED_OTHER and, with --child-nice, given that nice value.
The column mono_s is CLOCK_MONOTONIC, the same clock used by revbench for the loop start/end.

Writes one CSV row per sample (time, power in W, temperatures in degC) and prints a summary:
duration, mean power, energy. Energy per element = energy / (N * reps).
"""
import argparse, csv, glob, os, subprocess, sys, time


def read(path):
    with open(path) as f:
        return f.read().strip()


def find_power(root, override):
    if override:
        return override
    names = []
    for h in sorted(glob.glob(os.path.join(root, 'class/hwmon/hwmon*'))):
        try:
            name = read(os.path.join(h, 'name'))
        except OSError:
            continue
        names.append(f'{h}: {name}')
        if 'ina260' in name and os.path.exists(os.path.join(h, 'power1_input')):
            return h
    sys.exit('INA260 not found among hwmon devices:\n  ' + '\n  '.join(names) +
             '\nPass the right one with --hwmon /sys/class/hwmon/hwmonX')


def find_temps(root):
    """hwmon temp*_input (millidegC) and IIO in_temp*_raw/offset/scale (Zynq MPSoC AMS)."""
    sensors = []
    for h in sorted(glob.glob(os.path.join(root, 'class/hwmon/hwmon*'))):
        try:
            name = read(os.path.join(h, 'name'))
        except OSError:
            continue
        for t in sorted(glob.glob(os.path.join(h, 'temp*_input'))):
            sensors.append((f'{name}:{os.path.basename(t)[:-6]}', ('hwmon', t)))
    for d in sorted(glob.glob(os.path.join(root, 'bus/iio/devices/iio:device*'))):
        try:
            name = read(os.path.join(d, 'name'))
        except OSError:
            continue
        for raw in sorted(glob.glob(os.path.join(d, 'in_temp*_raw'))):
            base = raw[:-4]
            label = base.split('/')[-1]
            if os.path.exists(base + '_label'):
                label = read(base + '_label')
            sensors.append((f'{name}:{label}', ('iio', base)))
    return sensors


def temp_value(kind, path):
    if kind == 'hwmon':
        return int(read(path)) / 1000.0
    raw = float(read(path + '_raw'))
    off = float(read(path + '_offset')) if os.path.exists(path + '_offset') else 0.0
    scale = float(read(path + '_scale')) if os.path.exists(path + '_scale') else 1.0
    return (raw + off) * scale / 1000.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True, help='CSV file to write')
    ap.add_argument('--hz', type=float, default=50.0, help='sampling rate (default 50 Hz)')
    ap.add_argument('--idle', type=float, default=0.0, help='log for this many seconds with no command')
    ap.add_argument('--hwmon', default=None, help='hwmon directory of the INA260 (auto-detected)')
    ap.add_argument('--rt', action='store_true', help='run the logger as SCHED_FIFO 60')
    ap.add_argument('--child-nice', type=int, default=None, help='nice value of the command (e.g. -20)')
    ap.add_argument('--sysfs-root', default='/sys', help=argparse.SUPPRESS)
    ap.add_argument('cmd', nargs=argparse.REMAINDER, help='-- command to run')
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == '--' else a.cmd
    if not cmd and a.idle <= 0:
        ap.error('give a command after -- or --idle SECONDS')

    pdir = find_power(a.sysfs_root, a.hwmon)
    temps = find_temps(a.sysfs_root)
    period = 1.0 / a.hz
    rows = []
    def child_prep():
        try:
            os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
        except (AttributeError, OSError):
            pass
        if a.child_nice is not None:
            os.setpriority(os.PRIO_PROCESS, 0, a.child_nice)

    if a.rt:
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(60))
        except (AttributeError, OSError) as e:
            print(f'warning: cannot switch the logger to SCHED_FIFO ({e})', file=sys.stderr)
    proc = subprocess.Popen(cmd, preexec_fn=child_prep) if cmd else None
    t0 = time.monotonic()
    nxt = t0
    while True:
        now = time.monotonic()
        p_w = int(read(os.path.join(pdir, 'power1_input'))) / 1e6  # microwatt -> W
        rows.append([round(now - t0, 4), round(now, 6), p_w] + [round(temp_value(*s[1]), 2) for s in temps])
        if proc is not None and proc.poll() is not None:
            break
        if proc is None and now - t0 >= a.idle:
            break
        nxt += period
        time.sleep(max(0.0, nxt - time.monotonic()))
    dur = time.monotonic() - t0

    with open(a.out, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['t_s', 'mono_s', 'power_W'] + [s[0] for s in temps])
        w.writerows(rows)
    pw = [r[2] for r in rows]
    mean_p = sum(pw) / len(pw)
    print(f'samples={len(pw)} duration={dur:.3f} s mean_power={mean_p:.3f} W '
          f'min={min(pw):.3f} max={max(pw):.3f} energy={mean_p * dur:.3f} J -> {a.out}')
    if temps:
        last = rows[-1][3:]
        print('final temperatures: ' + ', '.join(f'{s[0]}={v}' for s, v in zip(temps, last)))
    if proc is not None and proc.returncode != 0:
        sys.exit(f'command exited with code {proc.returncode}')


if __name__ == '__main__':
    main()
