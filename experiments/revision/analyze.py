#!/usr/bin/env python3
"""analyze.py <results_dir> [--fpl MHz]

Turns a run_all.sh results directory into the tables and the figure data of the paper.
Repeated trials are always reported with their standard deviation (SD):
  timing  -> median of the timed runs (as in the paper) +- SD
  energy  -> mean over the independent repetitions +- SD

<results_dir>/tables/
  grid_stats.csv     every target/campaign/N/K: median, mean, SD, IQR, CV, bootstrap 95% CI
  best_cpu4.csv      best CPU-4 per configuration with SD and campaign-to-campaign spread
  energy.csv         energy per target and K (mean +- SD over repetitions)
  tab_*.tex          LaTeX tables with the same \\label as in the manuscript (median +- SD)
  temperatures.csv   min/max of every sensor and of the power, per phase
<results_dir>/figdata/   compact data read by make_figures.py (restyle the figures without re-analysing)
Standard library only (runs on the KV260 or on a PC).
"""
import argparse, csv, decimal, glob, json, math, os, random, re, statistics
from collections import defaultdict

KS = [1, 4, 16, 64, 256]
N20 = 1 << 20
# Per-element costs (ns) at N = 2^20 in the submitted manuscript (accelerators: kernel time).
PAPER_N20 = {
    'CPU1': [12.54, 16.62, 42.24, 140.33, 501.94],
    'CPU4best': [3.34, 10.52, 21.73, 35.54, 194.15],
    'P1': [60.00, 90.00, 210.00, 690.01, 2610.02],
    'P4': [1033.77, 1047.78, 1079.70, 1192.77, 1676.96],
}
# Kernel times (us) of the single-lane accelerator in the first campaign (Table 4 of the submitted manuscript).
FIRST_P1 = {1024: [62.9, 93.8, 216.6, 708.1, 2674.2], 4096: [247.4, 370.1, 861.8, 2827.8, 10692.2],
            16384: [984.6, 1476.1, 3442.2, 11306.6, 42764.0], 65536: [3933.7, 5899.8, 13764.2, 45221.6, 171051.5],
            262144: [15730.3, 23594.7, 55052.2, 180881.9, 684201.3],
            1048576: [62916.5, 94373.9, 220203.7, 723523.1, 2736801.6]}
NAMES = {'CPU1': 'CPU-1', 'CPU4': 'CPU-4 (bound)', 'CPU4b': 'CPU-4 (bound)', 'CPU4u': 'CPU-4 (unbound)',
         'CPU4best': 'CPU-4 (best)'}


# ------------------------------------------------------------------------------------------ helpers
def nice_name(tag, tex=False):
    if tag in NAMES:
        return NAMES[tag]
    m = re.match(r'P(\d+)(.*)', tag)
    if not m:
        return tag
    p, rest = m.group(1), m.group(2).strip('_')
    base = f'Accelerator $P={p}$' if tex else f'Accelerator P={p}'
    clk = re.match(r'c([\d.]+)(.*)', rest)
    if clk:
        base += f' @ {clk.group(1)} MHz'
        rest = clk.group(2).strip('_')
    return base + (' (sleep polling)' if rest == 'sleep' else (f' ({rest})' if rest else ''))


def lanes(tag):
    m = re.match(r'P(\d+)', tag)
    return int(m.group(1)) if m else None


def read_csv(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def quantile(xs, q):
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def sd(xs):
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


def describe(xs):
    rng = random.Random(12345)
    meds = sorted(statistics.median(rng.choices(xs, k=len(xs))) for _ in range(2000))
    mean = statistics.fmean(xs)
    s = sd(xs)
    return {'runs': len(xs), 'median': statistics.median(xs), 'mean': mean, 'sd': s,
            'q1': quantile(xs, 0.25), 'q3': quantile(xs, 0.75), 'iqr': quantile(xs, 0.75) - quantile(xs, 0.25),
            'cv_pct': 100.0 * s / mean if mean else 0.0,
            'ci_lo': quantile(meds, 0.025), 'ci_hi': quantile(meds, 0.975)}


def ratio_sd(a, sa, b, sb):
    """SD of a/b by first-order propagation of independent errors."""
    if a == 0 or b == 0:
        return 0.0
    return abs(a / b) * math.sqrt((sa / a) ** 2 + (sb / b) ** 2)


DEC = 2   # decimals of every value in the tables, in the figure labels and in the values quoted in the text
TEXT = []  # (where in the paper, what, value) -> tables/text_values.txt


def fnum(x, d=DEC, tex=True):
    """x with d decimals, rounded half up (13.125 -> 13.13) and with thousands separators"""
    if x != x or x in (float('inf'), float('-inf')):
        return '--'
    q = decimal.Decimal(repr(x)).quantize(decimal.Decimal(1).scaleb(-d), rounding=decimal.ROUND_HALF_UP)
    s = f'{q:,.{d}f}'
    if s.startswith('-') and float(s.replace(',', '')) == 0:
        s = s[1:]
    return s.replace(',', '{,}') if tex else s


def fsd(x, d=DEC, tex=True):
    """value with DEC decimals; a positive value that rounds to zero is written '<0.01'"""
    if x == x and 0 < abs(x) and abs(x) < 0.5 * 10 ** -d:
        return ('$<$' if tex else '<') + f'{10 ** -d:.{d}f}'
    return fnum(x, d, tex)


def pm(m, s, tex=True, newline=False, d=DEC):
    """'value +- SD', both with d decimals; an SD that rounds to zero is written '<0.01'."""
    if m != m:
        return '--'
    mid = (('\\newline ' if newline else '\\,') + '$\\pm$\\,') if tex else ' +- '
    if s is None or s != s:
        return fnum(m, d, tex)
    return f'{fnum(m, d, tex)}{mid}{fsd(s, d, tex)}'


def note(where, what, value, unit=''):
    """record a value quoted in the text, formatted like the tables"""
    TEXT.append((where, what, value + (f' {unit}' if unit else '')))


def rng(vals, tex=False):
    return f'{fnum(min(vals), tex=tex)}-{fnum(max(vals), tex=tex)}'


def write_tex(path, caption, label, colspec, header, rows, small=False, colsep=None):
    with open(path, 'w') as f:
        f.write('% generated by analyze.py - do not edit by hand, re-run the analysis instead\n')
        f.write('\\begin{table}[H]\n')
        f.write(f'\\caption{{{caption}\\label{{{label}}}}}\n')
        if small:
            f.write('\\footnotesize\n')
        if colsep:
            f.write(f'\\setlength{{\\tabcolsep}}{{{colsep}}}\n')
        f.write(f'\\begin{{tabularx}}{{\\textwidth}}{{{colspec}}}\n\\toprule\n')
        f.write(' & '.join(f'\\textbf{{{h}}}' for h in header) + '\\\\\n\\midrule\n')
        for r in rows:
            f.write(r[0] + '\n' if r[0] in ('\\addlinespace', '\\midrule') else ' & '.join(r) + '\\\\\n')
        f.write('\\bottomrule\n\\end{tabularx}\n\\end{table}\n')


def e2e_cols(n_acc):
    """tabularx columns of the end-to-end table: relative widths (summing to the number of columns) so that
    the seven-digit kernel times of slow designs fit with two decimals"""
    w = [1.1, 0.85, 0.85, 0.9, 0.95] + [1.1, 0.95] * n_acc
    if n_acc > 1:
        w[7] = 1.35                                 # kernel time of the second (slower) design
    w = [x * len(w) / sum(w) for x in w]
    return ''.join(f'>{{\\hsize={x:.3f}\\hsize}}C' for x in w)


def ntex(n):
    return f'{n:,}'.replace(',', '{,}')


def data_size(n):
    b = 8 * n
    return f'{b // (1 << 20)}~MiB' if b >= (1 << 20) else f'{b // 1024}~KiB'


# ------------------------------------------------------------------------------------------ energy
def energy(outdir, tables, figdata):
    idx_path = os.path.join(outdir, 'energy', 'index.csv')
    if not os.path.exists(idx_path) or not read_csv(idx_path):
        return None
    runs = defaultdict(list)
    idle_cache = {}
    for e in read_csv(idx_path):
        txt = open(os.path.join(outdir, e['out'])).read()
        m = re.search(r'^ENERGY (.*)$', txt, flags=re.M)
        if not m:
            print(f'WARNING: no ENERGY line in {e["out"]}')
            continue
        kv = dict(x.split('=', 1) for x in m.group(1).split())
        t0, t1 = float(kv['loop_start']), float(kv['loop_end'])
        rows = read_csv(os.path.join(outdir, e['power']))
        inside = [float(r['power_W']) for r in rows if t0 <= float(r['mono_s']) <= t1]
        if len(inside) < 20:
            print(f'WARNING: only {len(inside)} power samples inside the loop of {e["out"]}')
        if e['idle'] not in idle_cache:
            ip = [float(r['power_W']) for r in read_csv(os.path.join(outdir, e['idle']))]
            idle_cache[e['idle']] = (statistics.fmean(ip), sd(ip))
        p_idle = idle_cache[e['idle']][0]
        p_mean = statistics.fmean(inside) if inside else float('nan')
        reps, n, dur = int(kv['reps']), int(kv['n']), t1 - t0
        t_inv = float(kv['mean_e2e_ns']) * 1e-9
        e_inv = p_mean * dur / reps
        runs[(e['tag'], int(kv['k']))].append({
            'reps': reps, 'loop_s': dur, 't_inv_ms': t_inv * 1e3,
            't_inv_sd_within_ms': float(kv.get('sd_e2e_ns', 'nan')) * 1e-6,
            'p_mean_W': p_mean, 'p_sd_within_W': sd(inside), 'p_idle_W': p_idle,
            'e_elem_nJ': e_inv / n * 1e9, 'edyn_elem_nJ': (p_mean - p_idle) * dur / reps / n * 1e9,
            'edp_mJs': e_inv * t_inv * 1e3, 'mismatches': int(kv['mismatches']), 'n': n})
    metrics = ['t_inv_ms', 'p_mean_W', 'p_idle_W', 'e_elem_nJ', 'edyn_elem_nJ', 'edp_mJs']
    res = []
    for (tag, k), lst in runs.items():
        r = {'tag': tag, 'k': k, 'n': lst[0]['n'], 'repeats': len(lst),
             'mismatches': sum(x['mismatches'] for x in lst),
             'invocations': sum(x['reps'] for x in lst),
             't_inv_sd_within_ms': statistics.fmean(x['t_inv_sd_within_ms'] for x in lst),
             'p_sd_within_W': statistics.fmean(x['p_sd_within_W'] for x in lst)}
        for mtr in metrics:
            vals = [x[mtr] for x in lst]
            r[mtr] = statistics.fmean(vals)
            r[mtr + '_sd'] = sd(vals)
        res.append(r)
    order = lambda r: (0 if r['tag'] == 'CPU1' else 1 if r['tag'] == 'CPU4' else 2, r['tag'], r['k'])
    res.sort(key=order)
    ref = {r['k']: r for r in res if r['tag'] == 'CPU4'}
    for r in res:
        c = ref.get(r['k'])
        for mtr, out in (('e_elem_nJ', 'e_vs_cpu4'), ('edp_mJs', 'edp_vs_cpu4')):
            if c:
                r[out] = r[mtr] / c[mtr]
                r[out + '_sd'] = ratio_sd(r[mtr], r[mtr + '_sd'], c[mtr], c[mtr + '_sd'])
            else:
                r[out] = r[out + '_sd'] = float('nan')
    cols = list(res[0].keys())
    for path in (os.path.join(tables, 'energy.csv'), os.path.join(figdata, 'energy.csv')):
        with open(path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in res:
                w.writerow({c: (f'{r[c]:.6g}' if isinstance(r[c], float) else r[c]) for c in cols})
    nrep = max(r['repeats'] for r in res)
    # main table: processor baselines and accelerators with busy polling; the sleep-polling variants go to a
    # second (supplementary) table so that the main table fits on one page
    for fname, label, sel, extra in (
            ('tab_energy.tex', 'tab:energy', lambda tg: not tg.endswith('_sleep'),
             ' Accelerators: host polling \\texttt{ap\\_done} continuously (sleep-polling variants in Table~S2).'),
            ('tab_energy_sleep.tex', 'tab:energy_sleep', lambda tg: tg.endswith('_sleep'),
             ' Accelerators with the host sleeping for 20~$\\mu$s between polls.')):
        rows, last = [], None
        for r in [x for x in res if sel(x['tag'])]:
            if r['tag'] != last:
                if last is not None:
                    rows.append(['\\addlinespace'])
                rows.append([f"\\multicolumn{{7}}{{l}}{{\\textit{{{nice_name(r['tag'], tex=True)}}}}}"])
                last = r['tag']
            nl = dict(newline=True)
            rows.append([str(r['k']), pm(r['t_inv_ms'], r['t_inv_ms_sd'], **nl), pm(r['p_mean_W'], r['p_mean_W_sd'], **nl),
                         pm(r['e_elem_nJ'], r['e_elem_nJ_sd'], **nl), pm(r['edyn_elem_nJ'], r['edyn_elem_nJ_sd'], **nl),
                         pm(r['e_vs_cpu4'], r['e_vs_cpu4_sd'], **nl), pm(r['edp_vs_cpu4'], r['edp_vs_cpu4_sd'], **nl)])
        if not rows:
            continue
        write_tex(os.path.join(tables, fname),
                  f'Energy at $N=2^{{20}}$ (mean $\\pm$ standard deviation over {nrep} independent repetitions of '
                  f'at least 10~s each). Power is the mean SOM power during the measurement loop; dynamic energy '
                  f'subtracts the idle power with the same design loaded. Ratios refer to CPU-4 at the same $K$; '
                  f'EDP is the energy--delay product per invocation.' + extra,
                  label, 'cCCCCCC',
                  ['$K$', 'Time (ms)', 'Power (W)', 'Energy per element (nJ)', 'Dynamic (nJ)',
                   'Energy vs. CPU-4', 'EDP vs. CPU-4'], rows, small=True)

    # ---- values quoted in the text (Section 4.8, abstract, discussion)
    by = defaultdict(dict)
    for r in res:
        by[r['tag']][r['k']] = r
    cpu4, cpu1 = by.get('CPU4', {}), by.get('CPU1', {})
    busy = [t for t in by if lanes(t) and not t.endswith('_sleep')]
    accs = [t for t in by if lanes(t)]
    ks = sorted(cpu4)
    if cpu4 and busy:
        T = lambda x: fnum(x, tex=False)
        note('4.8', 'idle power (mean over the idle measurements)', T(statistics.fmean(r['p_idle_W'] for r in res)), 'W')
        note('4.8', 'CPU-4: power / energy per element', f"{rng([cpu4[k]['p_mean_W'] for k in ks])} W / "
                                                          f"{rng([cpu4[k]['e_elem_nJ'] for k in ks])} nJ")
        if cpu1:
            note('4.8', 'CPU-1: energy vs CPU-4', rng([cpu1[k]['e_vs_cpu4'] for k in ks]))
            d = [100 * abs(cpu1[k]['edyn_elem_nJ'] / cpu4[k]['edyn_elem_nJ'] - 1) for k in ks]
            note('4.8', 'CPU-1 vs CPU-4 dynamic energy: largest difference', T(max(d)), '%')
        if cpu1:
            note('4.8', 'energy loops: CPU-1 time / CPU-4 time', rng([cpu1[k]['t_inv_ms'] / cpu4[k]['t_inv_ms'] for k in ks]))
        note('4.8', 'accelerators (busy polling): power', rng([by[t][k]['p_mean_W'] for t in busy for k in ks]), 'W')
        note('4.8', 'accelerators (busy polling): time vs CPU-4',
             rng([by[t][k]['t_inv_ms'] / cpu4[k]['t_inv_ms'] for t in busy for k in ks]))
        for t in busy:
            note('4.8', f'{t} (busy polling): energy vs CPU-4', rng([by[t][k]['e_vs_cpu4'] for k in ks]))
            if t + '_sleep' in by:
                sl = by[t + '_sleep']
                note('4.8', f'{t}: sleep polling lowers the power by / the energy by',
                     f"{rng([by[t][k]['p_mean_W'] - sl[k]['p_mean_W'] for k in ks])} W / "
                     f"{rng([100 * (1 - sl[k]['e_elem_nJ'] / by[t][k]['e_elem_nJ']) for k in ks])} %")
                note('4.8', f'{t}: dynamic energy vs CPU-4, sleep / busy polling',
                     f"{rng([sl[k]['edyn_elem_nJ'] / cpu4[k]['edyn_elem_nJ'] for k in ks])} / "
                     f"{rng([by[t][k]['edyn_elem_nJ'] / cpu4[k]['edyn_elem_nJ'] for k in ks])}")
        note('4.8', 'better accelerator (any polling): energy vs CPU-4',
             rng([min(by[t][k]['e_vs_cpu4'] for t in accs) for k in ks]))
        note('4.8', 'better accelerator (any polling): EDP vs CPU-4',
             rng([min(by[t][k]['edp_vs_cpu4'] for t in accs) for k in ks]))
    return res


# ------------------------------------------------------------------------------------------ timing grid
def grid(outdir, tables, figdata, fpl):
    path = os.path.join(outdir, 'grid', 'grid.csv')
    if not os.path.exists(path):
        return None
    raw = defaultdict(lambda: defaultdict(list))
    modes, mism = {}, defaultdict(int)
    for r in read_csv(path):
        key = (r['tag'], int(r['campaign']), int(r['n']), int(r['k']))
        for m in ('kern', 'e2e', 'cfg', 's2d', 's2c'):
            raw[key][m].append(int(r[f't_{m}_ns']))
        e2e, kern = int(r['t_e2e_ns']), int(r['t_kern_ns'])
        raw[key]['outside'].append(100.0 * (e2e - kern) / e2e if e2e else 0.0)
        modes[key] = r['mode']
        mism[key] += int(r['mismatches'])
    st = {key: {m: describe(v) for m, v in d.items()} for key, d in raw.items()}

    # ---- grid_stats.csv
    sub = ['median', 'mean', 'sd', 'q1', 'q3', 'iqr', 'cv_pct', 'ci_lo', 'ci_hi']
    with open(os.path.join(tables, 'grid_stats.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['tag', 'mode', 'campaign', 'n', 'k', 'runs', 'mismatches'] +
                   [f'{m}_{s}' for m in ('kern', 'e2e') for s in sub] +
                   [f'{m}_{s}' for m in ('cfg', 's2d', 's2c') for s in ('median', 'sd')])
        for key in sorted(st):
            d = st[key]
            w.writerow(list(key[:1]) + [modes[key]] + list(key[1:]) + [d['kern']['runs'], mism[key]] +
                       [f"{d[m][s]:.6g}" for m in ('kern', 'e2e') for s in sub] +
                       [f"{d[m][s]:.6g}" for m in ('cfg', 's2d', 's2c') for s in ('median', 'sd')])

    def one(tag, n, k):
        keys = [x for x in st if x[0] == tag and x[2] == n and x[3] == k]
        return st[keys[0]] if keys else None

    cells = sorted({(x[2], x[3]) for x in st})
    acc_tags = sorted({x[0] for x in st if lanes(x[0])}, key=lambda t: (lanes(t), t))

    # ---- best CPU-4 and campaign spread
    best, spread = {}, {}
    for (n, k) in cells:
        cand = [(st[x]['e2e']['median'], x) for x in st if x[0] in ('CPU4b', 'CPU4u') and x[2] == n and x[3] == k]
        if cand:
            best[(n, k)] = min(cand)[1]
        b = [st[x]['e2e']['median'] for x in st if x[0] == 'CPU4b' and x[2] == n and x[3] == k]
        if len(b) > 1:
            spread[(n, k)] = (max(b) / min(b), statistics.fmean(b), sd(b), len(b))
    with open(os.path.join(tables, 'best_cpu4.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['n', 'k', 'best_median_ns', 'best_sd_ns', 'best_source', 'campaign',
                    'bound_campaigns', 'bound_medians_mean_ns', 'bound_medians_sd_ns', 'bound_spread_max_over_min'])
        for c in cells:
            if c in best:
                x = best[c]
                sp = spread.get(c, (float('nan'), float('nan'), float('nan'), 1))
                w.writerow([c[0], c[1], f"{st[x]['e2e']['median']:.6g}", f"{st[x]['e2e']['sd']:.6g}", x[0], x[1],
                            sp[3], f'{sp[1]:.6g}', f'{sp[2]:.6g}', f'{sp[0]:.4g}'])

    # ---- model fits: T = T0 + ceil(N/P) (K + c_P) / f
    # single-lane designs: T0 and c_P fitted; multi-lane designs: T0 fixed to the single-lane value at the same
    # clock (as in the paper), c_P fitted. 'free' = unconstrained fit T = T0 + G (a K + c)/f (a = cycles/iteration).
    def solve3(rows):
        # least squares for y = b0 + b1 x1 + b2 x2
        n = len(rows)
        s = [[0.0] * 3 for _ in range(3)]
        v = [0.0] * 3
        for x1, x2, y in rows:
            z = (1.0, x1, x2)
            for i in range(3):
                v[i] += z[i] * y
                for j in range(3):
                    s[i][j] += z[i] * z[j]
        for i in range(3):                                   # Gauss-Jordan
            piv = s[i][i]
            for j in range(3):
                s[i][j] /= piv
            v[i] /= piv
            for r in range(3):
                if r != i:
                    f_ = s[r][i]
                    for j in range(3):
                        s[r][j] -= f_ * s[i][j]
                    v[r] -= f_ * v[i]
        return v

    fits = {}
    for t in sorted(acc_tags, key=lambda x: lanes(x)):
        p, f_hz = lanes(t), fpl.get(t, fpl['default']) * 1e6
        pts = [(n, k, st[x]['kern']['median'] * 1e-9) for x in st if x[0] == t for n, k in [(x[2], x[3])]]
        base = [f for f in fits.values() if f['P'] == 1 and abs(f['fpl_mhz'] - f_hz / 1e6) < 1e-6]
        if p > 1 and base:
            t0 = base[0]['T0_us'] * 1e-6
            xs = [math.ceil(n / p) / f_hz for n, k, _ in pts]
            ys = [T - t0 - math.ceil(n / p) * k / f_hz for n, k, T in pts]
            c = sum(a * b for a, b in zip(xs, ys)) / sum(a * a for a in xs)
        else:
            xs = [math.ceil(n / p) / f_hz for n, k, _ in pts]
            ys = [T - math.ceil(n / p) * k / f_hz for n, k, T in pts]
            mx, my = statistics.fmean(xs), statistics.fmean(ys)
            c = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / sum((a - mx) ** 2 for a in xs)
            t0 = my - c * mx
        err = max(abs((t0 + math.ceil(n / p) * (k + c) / f_hz) / T - 1) for n, k, T in pts) * 100
        b0, b1, b2 = solve3([(math.ceil(n / p) * k / f_hz, math.ceil(n / p) / f_hz, T) for n, k, T in pts])
        fits[t] = {'P': p, 'fpl_mhz': f_hz / 1e6, 'c': c, 'T0_us': t0 * 1e6, 'max_err_pct': err,
                   'free_cycles_per_iter': b1, 'free_c': b2, 'free_T0_us': b0 * 1e6}
    # ---- end-to-end terms: affine fits of the synchronization times vs synchronized size (KiB)
    e2e_fit = {}
    main_tags = [t for t in acc_tags if '_' not in t]
    for m, nbytes in (('s2d', 8), ('s2c', 4)):
        pts = []
        for n in sorted({c[0] for c in cells}):
            pool = [v for x in raw if x[0] in main_tags and x[2] == n for v in raw[x][m]]
            if pool:
                pts.append((nbytes * n / 1024, statistics.median(pool) / 1e3))
        if len(pts) > 1:
            mx, my = statistics.fmean(p_[0] for p_ in pts), statistics.fmean(p_[1] for p_ in pts)
            b = sum((a - mx) * (y - my) for a, y in pts) / sum((a - mx) ** 2 for a, _ in pts)
            e2e_fit[m] = {'fixed_us': my - b * mx, 'us_per_KiB': b}
    cfg_all = [v for x in raw if x[0] in main_tags for v in raw[x]['cfg']]
    if cfg_all:
        e2e_fit['cfg_us'] = statistics.median(cfg_all) / 1e3
    cpu = {}
    v1 = [one('CPU1', N20, k) for k in KS]
    if all(v1):
        ys = [v['kern']['median'] / N20 for v in v1]
        mx, my = statistics.fmean(KS), statistics.fmean(ys)
        b = sum((a - mx) * (y - my) for a, y in zip(KS, ys)) / sum((a - mx) ** 2 for a in KS)
        cpu.update(alpha1_ns=my - b * mx, beta1_ns=b)
    if all((N20, k) in best for k in KS):
        ks = [16, 64, 256]
        ys = [st[best[(N20, k)]]['e2e']['median'] / N20 for k in ks]
        mx, my = statistics.fmean(ks), statistics.fmean(ys)
        cpu['beta4_ns_Kge16'] = sum((a - mx) * (y - my) for a, y in zip(ks, ys)) / sum((a - mx) ** 2 for a in ks)

    # ---- figure data
    with open(os.path.join(figdata, 'kernel_vs_N.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['tag', 'P', 'fpl_mhz', 'n', 'k', 'median_us', 'sd_us'])
        for t in acc_tags:
            for (n, k) in cells:
                d = one(t, n, k)
                if d:
                    w.writerow([t, lanes(t), fits[t]['fpl_mhz'], n, k, f"{d['kern']['median']/1e3:.6g}",
                                f"{d['kern']['sd']/1e3:.6g}"])
    with open(os.path.join(figdata, 'cost_N2e20.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['series', 'k', 'median_ns', 'sd_ns'])
        for k in KS:
            d = one('CPU1', N20, k)
            if d:
                w.writerow(['CPU1', k, f"{d['kern']['median']/N20:.6g}", f"{d['kern']['sd']/N20:.6g}"])
            if (N20, k) in best:
                d = st[best[(N20, k)]]
                w.writerow(['CPU4best', k, f"{d['e2e']['median']/N20:.6g}", f"{d['e2e']['sd']/N20:.6g}"])
            for t in acc_tags:
                d = one(t, N20, k)
                if d:
                    w.writerow([t, k, f"{d['kern']['median']/N20:.6g}", f"{d['kern']['sd']/N20:.6g}"])
    with open(os.path.join(figdata, 'map.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['n', 'k', 'best_acc', 'ratio', 'ratio_sd', 'unbound_factor', 'unbound_factor_sd'])
        for (n, k) in cells:
            if (n, k) not in best:
                continue
            accs = [(one(t, n, k)['e2e'], t) for t in acc_tags if one(t, n, k) and '_' not in t]
            if not accs:
                continue
            a, t = min(accs, key=lambda x: x[0]['median'])
            c = st[best[(n, k)]]['e2e']
            u = one('CPU4u', n, k)
            uf = usd = float('nan')
            if u:
                uf = u['e2e']['median'] / a['median']
                usd = ratio_sd(u['e2e']['median'], u['e2e']['sd'], a['median'], a['sd'])
            w.writerow([n, k, t, f"{a['median']/c['median']:.6g}",
                        f"{ratio_sd(a['median'], a['sd'], c['median'], c['sd']):.6g}", f'{uf:.6g}', f'{usd:.6g}'])
    with open(os.path.join(figdata, 'params.json'), 'w') as f:
        json.dump({'fits': fits, 'cpu': cpu, 'fpl_mhz': fpl}, f, indent=2)

    # ---- LaTeX tables (median +- SD)
    for t in acc_tags:
        if '_' in t:
            continue
        rows = []
        for n in sorted({c[0] for c in cells}):
            row = [ntex(n)]
            for k in KS:
                d = one(t, n, k)
                row.append(pm(d['kern']['median'] / 1e3, d['kern']['sd'] / 1e3, newline=True) if d else '--')
            rows.append(row)
        ft = fits[t]
        mm = sum(mism[x] for x in mism if x[0] == t)
        nruns = max(st[x]['kern']['runs'] for x in st if x[0] == t)
        model = (f'$T = T_0 + N(K+c_1)/f_{{\\mathrm{{PL}}}}$' if ft['P'] == 1 else
                 f'$T = T_0 + \\lceil N/{ft["P"]}\\rceil (K+c_{{{ft["P"]}}})/f_{{\\mathrm{{PL}}}}$')
        label = {'P1': 'tab:p1', 'P4': 'tab:p4'}.get(t, f'tab:{t.lower()}')
        write_tex(os.path.join(tables, f'tab_kernel_{t}.tex'),
                  f'Kernel time ($\\mu$s, median $\\pm$ standard deviation of {nruns} runs) of the accelerator with '
                  f'$P={ft["P"]}$ measured on the KV260 at $f_{{\\mathrm{{PL}}}}={ft["fpl_mhz"]:g}$~MHz. '
                  + ('All runs produced zero mismatching elements. ' if mm == 0 else f'{mm} mismatching elements. ')
                  + f'The model {model} with $T_0={fnum(ft["T0_us"])}~\\mu$s and $c_{{{ft["P"]}}}={fnum(ft["c"])}$ '
                  f'reproduces every median within {fnum(ft["max_err_pct"])}\\%.',
                  label, 'CCCCCC', ['$N$'] + [f'$K={k}$' for k in KS], rows, small=True)
    # processor baselines at N = 2^20
    if all(v1) and all((N20, k) in best for k in KS):
        rows = []
        ncamp = max((spread.get((N20, k), (0, 0, 0, 1))[3] for k in KS), default=1)
        for k in KS:
            c1 = one('CPU1', N20, k)['kern']
            u = one('CPU4u', N20, k)
            b = st[best[(N20, k)]]['e2e']
            bound = [st[x]['e2e'] for x in st if x[0] == 'CPU4b' and x[2] == N20 and x[3] == k]
            bb = min(bound, key=lambda d: d['median']) if bound else None
            sp = spread.get((N20, k))
            rows.append([str(k), pm(c1['median'] / 1e6, c1['sd'] / 1e6, newline=True),
                         pm(u['e2e']['median'] / 1e6, u['e2e']['sd'] / 1e6, newline=True) if u else '--',
                         pm(bb['median'] / 1e6, bb['sd'] / 1e6, newline=True) if bb else '--',
                         fnum(sp[0]) if sp else '--',
                         pm(c1['median'] / b['median'], ratio_sd(c1['median'], c1['sd'], b['median'], b['sd']),
                            newline=True)])
        write_tex(os.path.join(tables, 'tab_cpu.tex'),
                  f'Processor baselines for $N = 1{{,}}048{{,}}576$ elements (median $\\pm$ standard deviation of '
                  f'the timed runs after one warm-up). CPU-4 was measured once with unbound threads and in {ncamp} '
                  f'independent campaigns with threads bound to cores; the bound column reports the best campaign '
                  f'and the spread is the ratio between the slowest and the fastest campaign median. The speedup '
                  f'refers to the best CPU-4 configuration.',
                  'tab:cpu', 'CCCCCC', ['$K$', 'CPU-1 (ms)', 'CPU-4, unbound (ms)', 'CPU-4, bound (ms)',
                                        'Spread of bound campaigns', 'Best CPU-4 speedup'], rows, small=True)
        for t in acc_tags:
            if '_' in t or not all(one(t, N20, k) for k in KS):
                continue
            rows = []
            for k in KS:
                c1 = one('CPU1', N20, k)['kern']
                b = st[best[(N20, k)]]['e2e']
                a = one(t, N20, k)['kern']
                # slowdown T_acc / T_CPU (= 1/S): with two decimals, S itself would read 0.00 for most cells
                rows.append([str(k), pm(c1['median'] / 1e6, c1['sd'] / 1e6, newline=True),
                             pm(b['median'] / 1e6, b['sd'] / 1e6, newline=True),
                             pm(a['median'] / 1e6, a['sd'] / 1e6, newline=True),
                             pm(a['median'] / c1['median'], ratio_sd(a['median'], a['sd'], c1['median'], c1['sd']),
                                newline=True),
                             pm(a['median'] / b['median'], ratio_sd(a['median'], a['sd'], b['median'], b['sd']),
                                newline=True)])
            p = lanes(t)
            write_tex(os.path.join(tables, f'tab_vscpu_{t}.tex'),
                      f'Accelerator with $P={p}$ versus processor baselines at $N = 1{{,}}048{{,}}576$ (kernel time; '
                      f'median $\\pm$ standard deviation). The slowdown is $T_{{\\mathrm{{acc}}}}/T_{{\\mathrm{{CPU}}}} '
                      f'= 1/S$, so that values above 1 indicate that offloading is not profitable.',
                      {'P4': 'tab:p4cpu'}.get(t, f'tab:{t.lower()}cpu'), 'CCCCCC',
                      ['$K$', 'CPU-1 (ms)', 'CPU-4 (ms)', f'Accelerator $P={p}$ (ms)', 'Slowdown vs. CPU-1',
                       'Slowdown vs. CPU-4'], rows, small=True)
    # end-to-end decomposition at K = 1 (cfg/s2d/s2c pooled over designs and K)
    main_acc = [t for t in acc_tags if '_' not in t]
    if main_acc:
        rows = []
        for n in sorted({c[0] for c in cells}):
            pool = defaultdict(list)
            for x in raw:
                if x[0] in main_acc and x[2] == n:
                    for m in ('cfg', 's2d', 's2c'):
                        pool[m] += raw[x][m]
            row = [ntex(n), data_size(n)]
            for m in ('cfg', 's2d', 's2c'):
                row.append(pm(statistics.median(pool[m]) / 1e3, sd(pool[m]) / 1e3, newline=True) if pool[m] else '--')
            for t in main_acc:
                d = one(t, n, 1)
                row += ([pm(d['kern']['median'] / 1e3, d['kern']['sd'] / 1e3, newline=True),
                         pm(d['outside']['median'], d['outside']['sd'], newline=True)] if d else ['--', '--'])
            rows.append(row)
        hdr = ['$N$', 'Data', '$T_{\\mathrm{cfg}}$', '$T_{\\mathrm{s2d}}$', '$T_{\\mathrm{s2c}}$']
        for t in main_acc:
            hdr += [f'$T_{{\\mathrm{{kern}}}}$ $P{{=}}{lanes(t)}$', 'Outside (\\%)']
        write_tex(os.path.join(tables, 'tab_e2e.tex'),
                  'Decomposition of the end-to-end offloading time (Equation~\\eqref{eq:e2e}) for $K=1$, in $\\mu$s '
                  '(median $\\pm$ standard deviation). $T_{\\mathrm{cfg}}$, $T_{\\mathrm{s2d}}$ and '
                  '$T_{\\mathrm{s2c}}$ do not depend on the kernel and are pooled over all designs and all $K$. '
                  '``Outside\'\' is the fraction of $T_{\\mathrm{e2e}}$ spent outside the kernel.',
                  'tab:e2e', e2e_cols(len(main_acc)), hdr, rows, small=True, colsep='4pt')
    # dispersion at N = 2^20 (supplementary)
    rows = []
    series = [('CPU1', lambda k: one('CPU1', N20, k), 'kern')] + \
             [('CPU4best', lambda k: st[best[(N20, k)]] if (N20, k) in best else None, 'e2e')] + \
             [(t, (lambda tt: (lambda k: one(tt, N20, k)))(t), 'e2e') for t in main_acc]
    for name, getter, m in series:
        first = True
        for k in KS:
            d = getter(k)
            if not d:
                continue
            d = d[m]
            rows.append([(('CPU-4 (best)' if name == 'CPU4best' else nice_name(name, tex=True)) if first else ''),
                         str(k), pm(d['median'] / 1e6, d['sd'] / 1e6), fsd(d['cv_pct']),
                         f"[{fnum(d['ci_lo']/1e6)}, {fnum(d['ci_hi']/1e6)}]",
                         str(d['runs'])])
            first = False
    write_tex(os.path.join(tables, 'tab_dispersion.tex'),
              'Dispersion of the measurements at $N=2^{20}$: median $\\pm$ standard deviation, coefficient of '
              'variation and bootstrap 95\\% confidence interval of the median over the timed runs (accelerators: '
              'end-to-end time; CPU-4: best configuration).',
              'tab:dispersion', '>{\\raggedright\\arraybackslash}p{3.2cm}cCCCc',
              ['Target', '$K$', 'Median $\\pm$ SD (ms)', 'CV (\\%)', '95\\% CI (ms)', 'Runs'], rows, small=True)

    # ---- per-element cost at N = 2^20: measured rows and model predictions (Table tab:predict)
    have20 = all(one('CPU1', N20, k) for k in KS) and all((N20, k) in best for k in KS)
    if have20 and 'P1' in fits:
        f_mhz = fits['P1']['fpl_mhz']
        c1N = {k: one('CPU1', N20, k)['kern'] for k in KS}
        c4N = {k: st[best[(N20, k)]]['e2e'] for k in KS}
        cost4 = {k: c4N[k]['median'] / N20 for k in KS}

        def meas(d):
            return [pm(d[k]['median'] / N20, d[k]['sd'] / N20, newline=True) for k in KS]

        def pred(p, w=32, fm=f_mhz, c=5):
            vals = [max((k + c) / (p * fm * 1e-3), 4 / (w / 8 * fm * 1e-3)) for k in KS]
            return [('\\textbf{' + fnum(v) + '}') if v < cost4[k] else fnum(v) for v, k in zip(vals, KS)]

        nr = max(st[x]['kern']['runs'] for x in st)
        rows = [['CPU-1 (meas.)'] + meas(c1N), ['CPU-4 (meas.)'] + meas(c4N),
                ['Ideal 4-core (CPU-1/4)'] + [pm(c1N[k]['median'] / N20 / 4, c1N[k]['sd'] / N20 / 4, newline=True)
                                              for k in KS], ['\\midrule']]
        if one('P1', N20, 1):
            rows.append(['Accelerator $P=1$ (meas.)'] + meas({k: one('P1', N20, k)['kern'] for k in KS}))
        rows += [['Accelerator $P=4$, $c_P=5$'] + pred(4), ['Accelerator $P=8$'] + pred(8),
                 ['Accelerator $P=16$'] + pred(16), ['Accelerator $P=16$, 128-bit'] + pred(16, 128),
                 [f'Accelerator $P=16$, 128-bit, {2 * f_mhz:g}~MHz'] + pred(16, 128, 2 * f_mhz)]
        if one('P4', N20, 1):
            rows += [['\\midrule'], ['Accelerator $P=4$ (meas.)'] + meas({k: one('P4', N20, k)['kern'] for k in KS})]
        write_tex(os.path.join(tables, 'tab_predict.tex'),
                  f'Per-element cost (ns) at $N = 2^{{20}}$ of the processor baselines and of accelerators with $P$ '
                  f'lanes at {f_mhz:g}~MHz unless stated otherwise. Rows marked ``meas.\'\' are measured (median '
                  f'$\\pm$ standard deviation of {nr} runs); the ideal four-core row is CPU-1 divided by four; the '
                  f'other accelerator rows are model predictions (Equation~\\eqref{{eq:fpga_model}} with $c_P = 5$, '
                  f'bounded by Equation~\\eqref{{eq:if_bound}} for a 32-bit interface unless 128-bit is indicated). '
                  f'Values in bold are lower than the measured CPU-4 cost at the same $K$.',
                  'tab:predict', '>{\\raggedright\\arraybackslash}p{3.6cm}CCCCC',
                  ['Configuration'] + [f'$K={k}$' for k in KS], rows, small=True)

    # ---- values quoted in the text, formatted like the tables (tables/text_values.txt)
    if have20 and 'P1' in fits:
        T = lambda x: fnum(x, tex=False)
        f_mhz = fits['P1']['fpl_mhz']
        cost1 = {k: c1N[k]['median'] / N20 for k in KS}
        acc20 = {t: {k: one(t, N20, k)['kern']['median'] for k in KS} for t in main_tags if one(t, N20, 1)}
        note('4.2', 'CPU-1 cost per element at N=2^20, K=1 / K=256', f'{T(cost1[1])} / {T(cost1[256])}', 'ns')
        note('4.2', 'CPU-1 cost ratio K=256 / K=64 (N=2^20)', T(cost1[256] / cost1[64]))
        note('4.2', 'speedup of the best CPU-4 over CPU-1 at N=2^20', rng([c1N[k]['median'] / c4N[k]['median'] for k in KS]))
        cand = [c for c in cells if c in best and one('CPU1', *c)]
        ideal = [c for c in cand if st[best[c]]['e2e']['median'] <= 1.1 * one('CPU1', *c)['kern']['median'] / 4]
        note('4.2', 'configurations with the best CPU-4 within 10% of ideal four-core scaling', f'{len(ideal)} of {len(cand)}')
        if (1024, 1) in best:
            note('4.2', 'N=1024, K=1: best CPU-4 / CPU-1',
                 f"{T(st[best[(1024, 1)]]['e2e']['median'] / 1e3)} / {T(one('CPU1', 1024, 1)['kern']['median'] / 1e3)}", 'us')
        if spread:
            note('4.2', 'largest difference between the medians of the bound CPU-4 campaigns',
                 T(100 * (max(v[0] for v in spread.values()) - 1)), '%')
        ub = []
        for c in cells:
            bb = [st[x]['e2e']['median'] for x in st if x[0] == 'CPU4b' and (x[2], x[3]) == c]
            if one('CPU4u', *c) and bb:
                ub.append(one('CPU4u', *c)['e2e']['median'] / min(bb))
        if ub:
            note('4.2', 'unbound / best bound CPU-4 median (all cells)', rng(ub))
        cvb = [st[x]['e2e']['cv_pct'] for x in st if x[0] == 'CPU4b' and x[2] == N20]
        if cvb:
            note('4.2', 'CV of the bound CPU-4 runs at N=2^20: min-max (median)',
                 f'{rng(cvb)} ({T(statistics.median(cvb))})', '%')
        note('4.2', 'CV of the CPU-1 runs at N=2^20 (median)', T(statistics.median(c1N[k]['cv_pct'] for k in KS)), '%')
        lin = [abs((cpu['alpha1_ns'] + cpu['beta1_ns'] * k) / cost1[k] - 1) * 100 for k in KS]
        ks16 = [16, 64, 256]
        mx, my = statistics.fmean(ks16), statistics.fmean(cost1[k] for k in ks16)
        b16 = sum((a - mx) * (cost1[a] - my) for a in ks16) / sum((a - mx) ** 2 for a in ks16)
        note('4.2', 'alpha_1 / beta_1 (all K)', f"{T(cpu['alpha1_ns'])} / {T(cpu['beta1_ns'])}", 'ns')
        note('4.2', 'largest deviation of the linear CPU-1 model', T(max(lin)), '%')
        note('4.2', 'beta_1 (K>=16) / beta_1(K>=16)/4', f'{T(b16)} / {T(b16 / 4)}', 'ns')
        note('4.2', 'beta_4 (K>=16) and its deviation from beta_1(K>=16)/4',
             f"{T(cpu['beta4_ns_Kge16'])} ns, {T(100 * (cpu['beta4_ns_Kge16'] / (b16 / 4) - 1))}%")
        p1 = fits['P1']
        note('4.3', 'P1 cycles per element at N=2^20 for K=1..256',
             ', '.join(T(acc20['P1'][k] * 1e-9 / N20 * f_mhz * 1e6) for k in KS))
        note('4.3', 'P1 free fit: overhead cycles, cycles per iteration, T0',
             f"{T(p1['free_c'])}, {T(p1['free_cycles_per_iter'])}, {T(p1['free_T0_us'])} us")
        note('4.3', 'P1 fit (T0 and c free): c_1, T0, max error', f"{T(p1['c'])}, {T(p1['T0_us'])} us, {T(p1['max_err_pct'])}%")
        nT0 = [abs(n * (k + 5) / (f_mhz * 1e6) / (st[x]['kern']['median'] * 1e-9) - 1) * 100
               for x in st if x[0] == 'P1' for n, k in [(x[2], x[3])]]
        note('4.3', 'P1: largest error of T = N(K+5)/f (no T0)', T(max(nT0)), '%')
        cvp = {t: [st[x]['kern']['cv_pct'] for x in st if x[0] == t] for t in main_tags}
        note('4.3', 'P1: largest CV of the kernel runs (all N / N>=2^16)',
             f"{fsd(max(cvp['P1']), tex=False)} / {fsd(max(st[x]['kern']['cv_pct'] for x in st if x[0] == 'P1' and x[2] >= 1 << 16), tex=False)}", '%')
        dev = [(abs(st[x]['kern']['median'] / 1e3 / FIRST_P1[x[2]][KS.index(x[3])] - 1) * 100, x[2])
               for x in st if x[0] == 'P1' and x[2] in FIRST_P1 and x[3] in KS]
        if dev:
            note('5.4', 'P1 kernel times vs first campaign: largest difference (all N / N>=2^16)',
                 f"{fsd(max(d for d, _ in dev), tex=False)} / {fsd(max(d for d, n in dev if n >= 1 << 16), tex=False)}", '%')
        note('5.1', 'iterations per second: one core / four cores', f"{T(10 / cpu['beta1_ns'])} / "
             f"{T(1 / cpu['beta4_ns_Kge16'])}", 'x10^8 / x10^9')
        note('4.3', 'P1 at N=2^20: cost at K=1 and bytes moved per second',
             f"{T(acc20['P1'][1] / N20)} ns, {T(8 / (acc20['P1'][1] / N20 * 1e-9) / 1e6)} MB/s")
        note('4.3', 'P1 at N=2^20 (kernel): slowdown vs CPU-1 / vs best CPU-4',
             f"{rng([acc20['P1'][k] / c1N[k]['median'] for k in KS])} / {rng([acc20['P1'][k] / c4N[k]['median'] for k in KS])}")
        b4 = cpu['beta4_ns_Kge16']
        note('4.4', 'P_min,1 / P_min,4 at f_PL', f"{T(1 / (cpu['beta1_ns'] * 1e-3 * f_mhz))} / {T(1 / (b4 * 1e-3 * f_mhz))}")
        note('4.4', 'P_min,1 / P_min,4 at 2 f_PL', f"{T(1 / (cpu['beta1_ns'] * 2e-3 * f_mhz))} / {T(1 / (b4 * 2e-3 * f_mhz))}")
        note('4.4', 'P_min,4 / P_min,1', T(cpu['beta1_ns'] / b4))
        note('4.4', 'P_min,4 with the first-campaign slope (0.75 ns)', T(1 / (0.75e-3 * f_mhz)))
        p16 = [max((k + 5) / (16 * f_mhz * 1e-3), 4 / (16 * f_mhz * 1e-3)) for k in KS]
        p16x2 = [max((k + 5) / (32 * f_mhz * 1e-3), 4 / (32 * f_mhz * 1e-3)) for k in KS]
        note('4.4', 'P=16, 128-bit, f_PL: cost above CPU-4', rng([100 * (v / cost4[k] - 1) for v, k in zip(p16, KS)]), '%')
        note('4.4', 'P=16, 128-bit, 2 f_PL: speedup over CPU-4', rng([cost4[k] / v for v, k in zip(p16x2, KS)]))
        if 'P4' in fits and 'P4' in acc20:
            p4 = fits['P4']
            ys = [acc20['P4'][k] / N20 for k in KS]
            mx, my = statistics.fmean(KS), statistics.fmean(ys)
            slope = sum((a - mx) * (y - my) for a, y in zip(KS, ys)) / sum((a - mx) ** 2 for a in KS)
            kx = (p4['c'] / (4 * f_mhz * 1e-3) - p1['c'] / (f_mhz * 1e-3)) / (1 / (f_mhz * 1e-3) - 1 / (4 * f_mhz * 1e-3))
            note('4.5', 'P4 free fit: cycles per iteration', T(p4['free_cycles_per_iter']))
            note('4.5', 'P4 slope at N=2^20 / 1/(P f)', f'{T(slope)} / {T(1 / (4 * f_mhz * 1e-3))}', 'ns per iteration')
            note('4.5', 'P4: c_4, max error, largest CV',
                 f"{T(p4['c'])}, {T(p4['max_err_pct'])}%, {T(max(cvp['P4']))}%")
            note('4.5', 'c_4 / c_1 and c_4 / 8 accesses',
                 f"{T(p4['c'] / p1['c'])}; {T(p4['c'] / 8)} cycles = {T(p4['c'] / 8 / f_mhz)} us")
            note('4.5', 'P4 cost at N=2^20, K=1 (measured / predicted with c=5)',
                 f"{T(acc20['P4'][1] / N20)} / {T((1 + 5) / (4 * f_mhz * 1e-3))}", 'ns')
            note('4.5', 'break-even intensity P4 = P1 (model)', T(kx))
            note('4.5', 'P4 slope vs 1/(P f): deviation; measured / predicted cost at K=1; c_4 per element',
                 f"{T(100 * (slope / (1 / (4 * f_mhz * 1e-3)) - 1))}%; "
                 f"{T(acc20['P4'][1] / N20 / ((1 + 5) / (4 * f_mhz * 1e-3)))}; {T(p4['c'] / 4)} cycles")
            for k in KS:
                r_ = [one('P4', n, k)['kern']['median'] / one('P1', n, k)['kern']['median']
                      for n in sorted({c[0] for c in cells}) if one('P4', n, k) and one('P1', n, k)]
                note('4.5', f'P4/P1 kernel time over N at K={k} (and P1/P4)', f'{rng(r_)} ({rng([1 / x for x in r_])})')
            note('4.5', 'P4 at N=2^20 (kernel): slowdown vs CPU-1 / vs best CPU-4 (K=1 ... K=256)',
                 f"{T(acc20['P4'][1] / c1N[1]['median'])}-{T(acc20['P4'][256] / c1N[256]['median'])} / "
                 f"{T(acc20['P4'][1] / c4N[1]['median'])}-{T(acc20['P4'][256] / c4N[256]['median'])}")
        if e2e_fit and 's2d' in e2e_fit:
            fixed = e2e_fit.get('cfg_us', 0) + sum(d['fixed_us'] for d in e2e_fit.values() if isinstance(d, dict))
            per_el = (e2e_fit['s2d']['us_per_KiB'] * 8 + e2e_fit['s2c']['us_per_KiB'] * 4) / 1024 * 1e3
            note('4.6', 'T_cfg (median)', T(e2e_fit['cfg_us']), 'us')
            note('4.6', 'T_s2d: fixed + slope', f"{T(e2e_fit['s2d']['fixed_us'])} us + {T(e2e_fit['s2d']['us_per_KiB'])} us/KiB")
            note('4.6', 'T_s2c: fixed + slope', f"{T(e2e_fit['s2c']['fixed_us'])} us + {T(e2e_fit['s2c']['us_per_KiB'])} us/KiB")
            note('4.6', 'fixed cost per invocation / size-dependent cost per element', f'{T(fixed)} us / {T(per_el)} ns')
            for t in main_tags:
                o = [one(t, n, 1)['outside']['median'] for n in (1024, N20) if one(t, n, 1)]
                if len(o) == 2:
                    note('4.6', f'{t}: time outside the kernel at K=1, N=1024 / N=2^20', f'{T(o[0])} / {T(o[1])}', '%')
        ratios = []
        for c in cells:
            if c in best:
                accs = [one(t, *c)['e2e']['median'] for t in main_tags if one(t, *c)]
                if accs:
                    ratios.append(min(accs) / st[best[c]]['e2e']['median'])
        if ratios:
            note('4.7', 'better accelerator end to end vs best CPU-4 (slowdown)', rng(ratios))

    # ---- summary
    print('\n=== Timing grid ===')
    print(f'groups: {len(st)}, total mismatching elements: {sum(mism.values())}')
    if spread:
        wk, wv = max(spread.items(), key=lambda x: x[1][0])
        print(f'CPU-4 bound, campaign-to-campaign spread of the median: max {wv[0]:.2f}x at N={wk[0]}, K={wk[1]}; '
              f'median spread {statistics.median(v[0] for v in spread.values()):.2f}x (submitted version: up to 13.6x)')
    for t in main_acc:
        overlap = faster = total = 0
        for (n, k) in cells:
            d = one(t, n, k)
            if not d or (n, k) not in best:
                continue
            a, c = d['e2e'], st[best[(n, k)]]['e2e']
            total += 1
            overlap += (a['ci_lo'] <= c['ci_hi'] and c['ci_lo'] <= a['ci_hi'])
            faster += a['median'] < c['median']
        print(f'{t}: end-to-end vs best CPU-4 in {total} configurations -> accelerator faster in {faster}, '
              f'95% CIs overlapping in {overlap}')
    print('\nConsistency with the submitted values at N=2^20 (per-element cost, deviation from the paper):')
    for tag in ('CPU1', 'P1', 'P4', 'CPU4best'):
        if tag == 'CPU4best':
            vals = [st[best[(N20, k)]]['e2e']['median'] / N20 for k in KS if (N20, k) in best]
        else:
            vals = [one(tag, N20, k)['kern']['median'] / N20 for k in KS if one(tag, N20, k)]
        if len(vals) == len(KS):
            print(f'  {tag:9s} ' + '  '.join(f'K={k}: {v:.2f} ns ({100*(v/r-1):+.1f}%)'
                                              for k, v, r in zip(KS, vals, PAPER_N20[tag])))
    print('\nModel fits  T = T0 + ceil(N/P)(K + c_P)/f_PL  (all N, K; multi-lane: T0 of the single-lane design):')
    for t, ft in fits.items():
        print(f"  {t}: P={ft['P']} f={ft['fpl_mhz']:g} MHz  c_P={ft['c']:.2f}  T0={ft['T0_us']:.2f} us  "
              f"max error {ft['max_err_pct']:.2f}%   | free fit: {ft['free_cycles_per_iter']:.2f} cycles/iteration, "
              f"c={ft['free_c']:.2f}, T0={ft['free_T0_us']:.2f} us")
    if cpu:
        print('  processor: ' + ', '.join(f'{k}={v:.2f}' for k, v in cpu.items()))
        f0 = next(iter(fits.values()))['fpl_mhz'] if fits else 100.0
        for key, lab in (('beta1_ns', 'CPU-1'), ('beta4_ns_Kge16', 'CPU-4')):
            if key in cpu:
                for f_mhz in sorted({f0, 200.0}):
                    print(f"  P_min vs {lab} at {f_mhz:g} MHz = {1/(cpu[key]*1e-9*f_mhz*1e6):.2f}")
    if e2e_fit:
        print('\nEnd-to-end terms (pooled over designs and K): ' +
              ', '.join(f"{m}: {d['fixed_us']:.2f} us + {d['us_per_KiB']:.2f} us/KiB" for m, d in e2e_fit.items()
                        if isinstance(d, dict)) + f"; cfg median {e2e_fit.get('cfg_us', float('nan')):.2f} us")
        fixed = e2e_fit.get('cfg_us', 0) + sum(d['fixed_us'] for d in e2e_fit.values() if isinstance(d, dict))
        print(f'  fixed cost per invocation ~ {fixed:.2f} us')
        for k in KS:
            ns_ = [n for (n, kk) in cells if kk == k and (n, k) in best and st[best[(n, k)]]['e2e']['median'] / 1e3 < fixed]
            print(f'  K={k}: fixed cost exceeds the whole best CPU-4 execution for N up to '
                  f'{max(ns_) if ns_ else "none"}')
    for t in main_tags:
        outs = [(n, one(t, n, 1)['outside']['median']) for n in sorted({c[0] for c in cells}) if one(t, n, 1)]
        if outs:
            print(f'  {t}: time outside the kernel at K=1: ' + ', '.join(f'N={n}: {o:.2f}%' for n, o in outs))
    if 'P1' in main_tags and 'P4' in main_tags:
        print('\nP4 / P1 kernel-time ratio per K (min-max over N):')
        for k in KS:
            r_ = [one('P4', n, k)['kern']['median'] / one('P1', n, k)['kern']['median']
                  for n in sorted({c[0] for c in cells}) if one('P4', n, k) and one('P1', n, k)]
            print(f'  K={k}: {min(r_):.2f} - {max(r_):.2f}')
    for t in main_tags:
        if all(one(t, N20, k) for k in KS) and all((N20, k) in best for k in KS):
            print(f'\n{t} at N=2^20, kernel time: slower than CPU-1 by ' +
                  ', '.join(f"{one(t, N20, k)['kern']['median']/one('CPU1', N20, k)['kern']['median']:.2f}" for k in KS) +
                  '; slower than best CPU-4 by ' +
                  ', '.join(f"{one(t, N20, k)['kern']['median']/st[best[(N20, k)]]['e2e']['median']:.2f}" for k in KS))
    ratios = []
    for (n, k) in cells:
        if (n, k) in best:
            accs = [one(t, n, k)['e2e']['median'] for t in main_tags if one(t, n, k)]
            if accs:
                ratios.append(min(accs) / st[best[(n, k)]]['e2e']['median'])
    if ratios:
        print(f'\nBest accelerator end to end vs best CPU-4: {min(ratios):.2f} - {max(ratios):.2f} times slower')
    with open(os.path.join(figdata, 'params.json')) as f:
        pj = json.load(f)
    pj['e2e'] = e2e_fit
    with open(os.path.join(figdata, 'params.json'), 'w') as f:
        json.dump(pj, f, indent=2)
    return st


# ------------------------------------------------------------------------------------------ temperatures
def temperatures(outdir, tables):
    out = []
    for path in sorted(glob.glob(os.path.join(outdir, 'power', '*.csv'))):
        rows = read_csv(path)
        if not rows:
            continue
        for col in rows[0]:
            if col in ('t_s', 'mono_s'):
                continue
            vals = [float(r[col]) for r in rows]
            out.append([os.path.basename(path)[:-4], col, f'{min(vals):.2f}', f'{max(vals):.2f}',
                        f'{statistics.fmean(vals):.2f}', f'{sd(vals):.2f}'])
    with open(os.path.join(tables, 'temperatures.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['phase', 'sensor', 'min', 'max', 'mean', 'sd'])
        w.writerows(out)
    print('\n=== Temperatures and power over all phases (min - max) ===')
    agg = defaultdict(lambda: [float('inf'), float('-inf')])
    for _, s, lo, hi, _, _ in out:
        agg[s][0] = min(agg[s][0], float(lo))
        agg[s][1] = max(agg[s][1], float(hi))
    for s, (lo, hi) in agg.items():
        print(f'  {s}: {lo:.2f} - {hi:.2f}')
    temps = [v for s, v in agg.items() if 'temp' in s]
    pl = [v for s, v in agg.items() if 'pl_temp' in s]
    if temps:
        note('3.8', 'on-chip temperatures, all sensors (min-max)',
             f"{fnum(min(v[0] for v in temps), tex=False)}-{fnum(max(v[1] for v in temps), tex=False)}", 'degC')
    if pl:
        note('3.8', 'PL temperature (min-max)', f"{fnum(pl[0][0], tex=False)}-{fnum(pl[0][1], tex=False)}", 'degC')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--fpl', type=float, default=100.0, help='default PL clock in MHz')
    a = ap.parse_args()
    tables = os.path.join(a.outdir, 'tables')
    figdata = os.path.join(a.outdir, 'figdata')
    os.makedirs(tables, exist_ok=True)
    os.makedirs(figdata, exist_ok=True)
    fpl = {'default': a.fpl}
    fpath = os.path.join(a.outdir, 'energy', 'fpl.csv')
    if os.path.exists(fpath):
        for r in read_csv(fpath):
            fpl[r['tag']] = float(r['fpl_mhz'])
    res = energy(a.outdir, tables, figdata)
    if res:
        print('=== Energy at N=2^20 (mean +- SD over repetitions) ===')
        for r in res:
            print(f"  {nice_name(r['tag']):34s} K={r['k']:3d}  t={pm(r['t_inv_ms'], r['t_inv_ms_sd'], tex=False)} ms  "
                  f"P={pm(r['p_mean_W'], r['p_mean_W_sd'], tex=False)} W  "
                  f"E={pm(r['e_elem_nJ'], r['e_elem_nJ_sd'], tex=False)} nJ/elem  "
                  f"E/CPU4={pm(r['e_vs_cpu4'], r['e_vs_cpu4_sd'], tex=False)}")
    grid(a.outdir, tables, figdata, fpl)
    temperatures(a.outdir, tables)
    if TEXT:
        TEXT.sort(key=lambda x: x[0])
        with open(os.path.join(tables, 'text_values.txt'), 'w') as f:
            f.write(f'# values quoted in the text, {DEC} decimals, same rounding as the tables (generated by analyze.py)\n')
            for where, what, val in TEXT:
                f.write(f'Sec. {where:4s} {what}: {val}\n')
        print('\n=== Values quoted in the text (tables/text_values.txt) ===')
        for where, what, val in TEXT:
            print(f'  Sec. {where:4s} {what}: {val}')
    print(f'\nTables: {tables}\nFigure data: {figdata}  (python3 make_figures.py {a.outdir})')


if __name__ == '__main__':
    main()
