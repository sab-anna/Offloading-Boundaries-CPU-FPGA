#!/usr/bin/env bash
# Put the KV260 (Kria Ubuntu) in a quiet, documented state before the measurement campaigns
# and save a snapshot of that state for the "Measurement conditions" paragraph 
# Usage: sudo ./quiet_system.sh [snapshot_file]      Undo: reboot.
set -u
SNAP=${1:-system_state_$(date +%Y%m%d_%H%M%S).txt}

# 1) no desktop session (if one is running)
systemctl isolate multi-user.target 2>/dev/null || true

# 2) stop periodic / background services that wake up during long runs
for s in unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer \
         snapd.service snapd.socket packagekit.service fwupd.service \
         motd-news.timer man-db.timer ModemManager.service cups.service \
         avahi-daemon.service avahi-daemon.socket; do
  systemctl stop "$s" 2>/dev/null || true
done

# 3) fix the APU frequency at its maximum (1333 MHz on the K26); one policy covers all four A53 cores
P=/sys/devices/system/cpu/cpufreq/policy0
echo userspace > $P/scaling_governor
cat $P/cpuinfo_max_freq > $P/scaling_setspeed

# 4) snapshot of the state used for the runs
{
  echo "# date: $(date -Is)"
  echo "# kernel: $(uname -r)"
  echo "# governor: $(cat $P/scaling_governor)  cur_freq_kHz: $(cat $P/scaling_cur_freq)"
  echo "# sched_rt_runtime_us: $(cat /proc/sys/kernel/sched_rt_runtime_us)"
  echo "# isolcpus/nohz in cmdline: $(grep -o 'isolcpus=[^ ]*\|nohz_full=[^ ]*' /proc/cmdline || echo none)"
  echo "# network interfaces up:"; ip -brief link | awk '$2=="UP"{print "#   "$1}'
  echo "# logged-in sessions:"; who | sed 's/^/#   /'
  echo "# running services:"; systemctl list-units --type=service --state=running --no-legend | awk '{print "#   "$1}'
  echo "# top CPU consumers (5 s sample):"; top -b -n 2 -d 5 | awk '/^ *PID/{n++} n==2' | head -8 | sed 's/^/#   /'
  echo "# platformstats:"; (xmutil platformstats 2>/dev/null || true) | sed 's/^/#   /'
} > "$SNAP"
echo "State saved to $SNAP"
