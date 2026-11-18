#!/usr/bin/env python3
"""make_figures.py <results_dir> [--out DIR]

Draws the figures of the paper from <results_dir>/figdata (written by analyze.py) and saves each one
as PDF (vector, for \\includegraphics) and PNG (preview) in <results_dir>/figures, or in --out.
Error bars are +-1 standard deviation of the repeated trials. To restyle a figure, edit this file and
re-run it: it takes seconds and does not touch the measurements.

  fig_kernel_<tag>.pdf     kernel time vs N for one accelerator design, with the calibrated model
  fig_profit.pdf           per-element cost vs K at N = 2^20: (a) measured + models, (b) predictions
  fig_offloading_map.pdf   end-to-end accelerator / best CPU-4 over the (N, K) grid
  fig_energy.pdf           energy per element vs K at N = 2^20: (a) total, (b) dynamic

Figure width matches the MDPI text width (13.9 cm): include with \\includegraphics[width=\\textwidth]{...}.
Requires matplotlib (pip install matplotlib).
"""
import argparse, csv, decimal, json, math, os, re
from collections import defaultdict

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle

# ------------------------------------------------------------------ style (validated palette, print-safe)
INK, INK2, MUTED = '#0b0b0b', '#52514e', '#898781'
GRID, AXIS = '#e1e0d9', '#c3c2b7'
SLOTS = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948']
ORDINAL = ['#86b6ef', '#5598e7', '#2a78d6', '#1c5cab', '#0d366b']           # K = 1 ... 256
SEQ = ['#cde2fb', '#86b6ef', '#2a78d6', '#184f95']                          # map: slower bins
DIV = ['#f6c9c8', '#e34948']                                                 # map: accelerator faster
MARKERS = ['o', 's', '^', 'D', 'v', 'p', 'h', 'P']
TEXTW = 5.46                                                                 # inches (MDPI \textwidth)
MS = 3.2              # marker size (pt): small enough not to hide the +-1 SD error bars
DEC = 2               # decimals of the numbers written in the figures (same as the tables)
KS = [1, 4, 16, 64, 256]
N20 = 1 << 20

plt.rcParams.update({
    'font.family': 'sans-serif', 'font.size': 8, 'axes.titlesize': 8.5, 'axes.labelsize': 8.5,
    'xtick.labelsize': 7.5, 'ytick.labelsize': 7.5, 'legend.fontsize': 7.5, 'text.color': INK,
    'axes.labelcolor': INK, 'axes.edgecolor': AXIS, 'axes.linewidth': 0.8, 'xtick.color': INK2,
    'ytick.color': INK2, 'axes.grid': True, 'grid.color': GRID, 'grid.linewidth': 0.6,
    'axes.axisbelow': True, 'legend.frameon': False, 'lines.linewidth': 1.4, 'lines.markersize': MS,
    'lines.markeredgewidth': 0.8, 'errorbar.capsize': 2.2, 'savefig.bbox': 'tight', 'savefig.pad_inches': 0.03, 'pdf.fonttype': 42,
})

# fixed entity -> slot assignment (colour follows the entity, never its rank)
ENTITY_SLOT = {'CPU1': 0, 'CPU4': 1, 'P1': 2, 'P4': 3, 'P8': 4, 'P16': 5}


def entity(tag):
    if tag.startswith('CPU4'):
        return 'CPU4'
    m = re.match(r'(P\d+)', tag)
    return m.group(1) if m else tag


def style(tag):
    e = entity(tag)
    slot = ENTITY_SLOT.get(e, 6)
    hollow = tag.endswith('_sleep')
    return {'color': SLOTS[slot], 'marker': MARKERS[slot], 'mfc': 'white' if hollow else SLOTS[slot],
            'ls': '--' if hollow else '-'}


def label(tag):
    if tag == 'CPU1':
        return 'CPU-1'
    if tag in ('CPU4', 'CPU4best', 'CPU4b'):
        return 'CPU-4'
    m = re.match(r'P(\d+)(.*)', tag)
    if not m:
        return tag
    rest = m.group(2).strip('_')
    s = f'Accelerator $P={m.group(1)}$'
    clk = re.match(r'c([\d.]+)(.*)', rest)
    if clk:
        s += f', {clk.group(1)} MHz'
        rest = clk.group(2).strip('_')
    return s + (' (sleep polling)' if rest == 'sleep' else (f' ({rest})' if rest else ''))


def fmt(x, d=DEC):
    """x with d decimals, rounded half up like the tables written by analyze.py"""
    q = decimal.Decimal(repr(float(x))).quantize(decimal.Decimal(1).scaleb(-d), rounding=decimal.ROUND_HALF_UP)
    return f'{q:.{d}f}'


def fval(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def read_csv(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def save(fig, outdir, name):
    for ext in ('pdf', 'png'):
        fig.savefig(os.path.join(outdir, f'{name}.{ext}'), dpi=220 if ext == 'png' else None)
    plt.close(fig)
    print(f'  {name}.pdf / .png')


def log2_axis(ax, ticks, labels=None):
    ax.set_xscale('log', base=2)
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels or [str(t) for t in ticks])
    ax.minorticks_off()


def errbar(ax, x, y, s, st, lab=None, ls='none', **kw):
    yerr = [v if v is not None else 0 for v in s] if any(v for v in s if v) else None
    # barsabove: the error bars are drawn over the (small) markers, so they stay visible even when short
    return ax.errorbar(x, y, yerr=yerr, color=st['color'], marker=st['marker'], ms=MS, mfc=st['mfc'],
                       mec=st['color'], ls=ls, elinewidth=0.9, capthick=0.9, barsabove=True, label=lab, **kw)


# ------------------------------------------------------------------ figures
def fig_kernel(fd, params, outdir):
    path = os.path.join(fd, 'kernel_vs_N.csv')
    if not os.path.exists(path):
        return
    rows = read_csv(path)
    for tag in sorted({r['tag'] for r in rows}):
        if '_' in tag:
            continue
        fit = params['fits'].get(tag)
        fig, ax = plt.subplots(figsize=(TEXTW, 3.2))
        ns = sorted({int(r['n']) for r in rows if r['tag'] == tag})
        for i, k in enumerate(KS):
            pts = sorted((int(r['n']), float(r['median_us']), fval(r['sd_us'])) for r in rows
                         if r['tag'] == tag and int(r['k']) == k)
            if not pts:
                continue
            st = {'color': ORDINAL[i], 'marker': MARKERS[i], 'mfc': ORDINAL[i]}
            errbar(ax, [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts], st, lab=f'$K={k}$')
            if fit:
                xs = [2 ** (math.log2(ns[0]) + j * (math.log2(ns[-1]) - math.log2(ns[0])) / 60) for j in range(61)]
                ys = [fit['T0_us'] + math.ceil(x / fit['P']) * (k + fit['c']) / fit['fpl_mhz'] for x in xs]
                ax.plot(xs, ys, ls=(0, (4, 2)), color=MUTED, lw=1.0, zorder=1,
                        label='Model' if i == 0 else None)
        ax.set_yscale('log')
        log2_axis(ax, ns, [f'$2^{{{int(math.log2(n))}}}$' for n in ns])
        ax.set_xlabel('Problem size $N$ (elements)')
        ax.set_ylabel('Kernel time ($\\mu$s)')
        ax.legend(ncol=3, loc='upper left')
        save(fig, outdir, f'fig_kernel_{tag}')


def fig_profit(fd, params, outdir):
    path = os.path.join(fd, 'cost_N2e20.csv')
    if not os.path.exists(path):
        return
    data = defaultdict(dict)
    for r in read_csv(path):
        data[r['series']][int(r['k'])] = (float(r['median_ns']), fval(r['sd_ns']))
    fits = params.get('fits', {})
    fig, (a, b) = plt.subplots(2, 1, figsize=(TEXTW, 7.8), sharex=True, gridspec_kw={'hspace': 0.75})
    kx = [k for k in KS]
    hl = {a: [], b: []}                                   # legend entries in drawing order

    def series(ax, tag, lab, ls='-'):
        d = data.get(tag)
        if not d:
            return
        ks = [k for k in kx if k in d]
        hl[ax].append((errbar(ax, ks, [d[k][0] for k in ks], [d[k][1] for k in ks], style(tag), ls=ls), lab))

    def ideal(ax):
        d = data.get('CPU1')
        if d:
            ks = [k for k in kx if k in d]
            h, = ax.plot(ks, [d[k][0] / 4 for k in ks], color=SLOTS[1], ls=':', lw=1.6)
            hl[ax].append((h, 'Ideal four-core (CPU-1/4)'))

    def bounds(ax, f_mhz):
        for w, ls in ((32, '-'), (128, '--')):
            y = 4.0 / (w / 8) * 1e3 / f_mhz
            ax.axhline(y, color=MUTED, lw=0.8, ls=ls, zorder=1)
            ax.text(300, y * 1.08, f'{w}-bit interface bound', ha='right', va='bottom', fontsize=7, color=INK2)

    # (a) measured + calibrated models
    series(a, 'CPU1', 'CPU-1')
    series(a, 'CPU4best', 'CPU-4 (best)')
    ideal(a)
    f_ref = 100.0
    for tag in sorted([t for t in data if t.startswith('P') and '_' not in t], key=lambda t: (int(re.match(r'P(\d+)', t).group(1)), t)):
        series(a, tag, f'{label(tag)}, measured', ls='none')
        ft = fits.get(tag)
        if ft:
            ks = [2 ** (j / 10) for j in range(0, 81)]
            h, = a.plot(ks, [(k + ft['c']) / (ft['P'] * ft['fpl_mhz']) * 1e3 for k in ks], color=style(tag)['color'],
                        ls=(0, (4, 2)), lw=1.1)
            cv = fmt(ft['c'])
            hl[a].append((h, f'{label(tag)}, model ($c_{{{ft["P"]}}}={cv}$)'))
            f_ref = ft['fpl_mhz'] if tag == 'P1' else f_ref
    bounds(a, f_ref)
    a.set_title('(a) Measured costs and calibrated accelerator models', loc='left')
    # (b) predictions with the overhead of the single-lane design
    series(b, 'CPU4best', 'CPU-4 (best)')
    ideal(b)
    c1 = fits.get('P1', {}).get('c', 5.0)
    f1 = fits.get('P1', {}).get('fpl_mhz', 100.0)
    ks = [2 ** (j / 10) for j in range(0, 81)]
    for p, ls in ((4, '-.'), (8, '--'), (16, '-')):
        st = style(f'P{p}')
        b.plot(ks, [(k + c1) / (p * f1) * 1e3 for k in ks], color=st['color'], ls=ls, lw=1.4)
        b.plot(KS, [(k + c1) / (p * f1) * 1e3 for k in KS], color=st['color'], ls='none', marker=st['marker'],
               ms=MS + 0.8, mfc='white', mec=st['color'])
    for p, ls in ((4, '-.'), (8, '--'), (16, '-')):
        st = style(f'P{p}')
        h, = b.plot([], [], color=st['color'], ls=ls, marker=st['marker'], ms=MS + 0.8, mfc='white')
        hl[b].append((h, f'Accelerator $P={p}$, model'))
    bounds(b, f1)
    b.set_title(f'(b) Predicted costs with burst access ($c_P={c1:.0f}$, {f1:g} MHz)', loc='left')
    for ax in (a, b):
        ax.set_yscale('log')
        log2_axis(ax, KS)
        ax.set_xlim(0.8, 320)
        ax.set_ylabel('Per-element cost (ns)')
        ax.legend([h for h, _ in hl[ax]], [l for _, l in hl[ax]], ncol=2, loc='upper center',
                  bbox_to_anchor=(0.5, -0.14), fontsize=7.2)
    a.tick_params(labelbottom=True)
    b.set_xlabel('Inner iterations per element $K$')
    a.set_ylim(1, 6000)
    b.set_ylim(1, 1500)
    save(fig, outdir, 'fig_profit')


def fig_map(fd, outdir):
    path = os.path.join(fd, 'map.csv')
    if not os.path.exists(path):
        return
    rows = read_csv(path)
    ns = sorted({int(r['n']) for r in rows})
    fig, ax = plt.subplots(figsize=(TEXTW, 3.9))
    bins = [(1, 10, SEQ[0], INK), (10, 15, SEQ[1], INK), (15, 20, SEQ[2], '#ffffff'), (20, 1e9, SEQ[3], '#ffffff')]
    for r in rows:
        i, j = ns.index(int(r['n'])), KS.index(int(r['k']))
        v, s = float(r['ratio']), fval(r['ratio_sd'])
        if v >= 1:
            fc, tc = next((c, t) for lo, hi, c, t in bins if lo <= v < hi)
            txt = fmt(v)
        else:
            fc, tc = (DIV[0], INK) if v > 0.5 else (DIV[1], '#ffffff')
            txt = f'{fmt(1 / v)}x faster'
        ax.add_patch(Rectangle((i, j), 1, 1, facecolor=fc, edgecolor='white', lw=1.5))
        ax.text(i + 0.5, j + 0.64, txt, ha='center', va='center', fontsize=8.5, fontweight='bold', color=tc)
        if s:
            ax.text(i + 0.5, j + 0.42, f'$\\pm${fmt(s)}', ha='center', va='center', fontsize=6.5, color=tc)
        uf, us = fval(r['unbound_factor']), fval(r['unbound_factor_sd'])
        if uf and uf > 1 and v >= 1:
            ax.add_patch(Rectangle((i + 0.06, j + 0.06), 0.88, 0.88, fill=False, ec=tc, lw=1.1, ls=(0, (3, 2))))
            ax.text(i + 0.5, j + 0.2, f'faster $\\times${fmt(uf)}' + (f'$\\pm${fmt(us)}' if us else ''), ha='center',
                    va='center', fontsize=6.3, color=tc)
    ax.set_xlim(0, len(ns))
    ax.set_ylim(0, len(KS))
    ax.set_xticks([i + 0.5 for i in range(len(ns))])
    ax.set_xticklabels([f'$2^{{{int(math.log2(n))}}}$' for n in ns])
    ax.set_yticks([j + 0.5 for j in range(len(KS))])
    ax.set_yticklabels([str(k) for k in KS])
    ax.set_xlabel('Problem size $N$ (elements)')
    ax.set_ylabel('$K$')
    ax.grid(False)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(length=0)
    handles = [Patch(fc=c, ec=AXIS, label=l) for c, l in
               zip(SEQ, ['<10', '10–15', '15–20', '≥20 times slower'])]
    if any(float(r['ratio']) < 1 for r in rows):
        handles.append(Patch(fc=DIV[1], ec=AXIS, label='accelerator faster'))
    if any((fval(r['unbound_factor']) or 0) > 1 and float(r['ratio']) >= 1 for r in rows):
        handles.append(Patch(fc='white', ec=INK, ls=(0, (3, 2)), label='faster than CPU-4 with unbound threads (factor)'))
    ax.legend(handles=handles, ncol=len(handles) if len(handles) <= 5 else 3, loc='upper center',
              bbox_to_anchor=(0.5, -0.17), handlelength=1.4)
    save(fig, outdir, 'fig_offloading_map')


def fig_energy(fd, outdir):
    path = os.path.join(fd, 'energy.csv')
    if not os.path.exists(path):
        return
    rows = read_csv(path)
    tags = []
    for r in rows:
        if r['tag'] not in tags:
            tags.append(r['tag'])
    fig, (a, b) = plt.subplots(1, 2, figsize=(TEXTW, 3.1), gridspec_kw={'wspace': 0.32})
    for ax, m, title in ((a, 'e_elem_nJ', '(a) Total energy'),
                         (b, 'edyn_elem_nJ', '(b) Dynamic energy (idle power subtracted)')):
        for t in tags:
            d = sorted((int(r['k']), float(r[m]), fval(r[m + '_sd'])) for r in rows if r['tag'] == t)
            d = [x for x in d if x[1] > 0]
            if d:
                st = style(t)
                errbar(ax, [x[0] for x in d], [x[1] for x in d], [x[2] for x in d], st, lab=label(t), ls=st['ls'])
        ax.set_yscale('log')
        log2_axis(ax, KS)
        ax.set_xlim(0.8, 320)
        ax.set_ylabel('Energy per element (nJ)')
        ax.set_xlabel('Inner iterations per element $K$')
        ax.set_title(title, loc='left')
    handles, labels = a.get_legend_handles_labels()
    fig.legend(handles, labels, ncol=3, loc='upper center', bbox_to_anchor=(0.5, 0.0))
    save(fig, outdir, 'fig_energy')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('results_dir')
    ap.add_argument('--out', default=None, help='output directory (default <results_dir>/figures)')
    a = ap.parse_args()
    fd = os.path.join(a.results_dir, 'figdata')
    out = a.out or os.path.join(a.results_dir, 'figures')
    os.makedirs(out, exist_ok=True)
    pp = os.path.join(fd, 'params.json')
    params = json.load(open(pp)) if os.path.exists(pp) else {'fits': {}}
    print(f'Figures -> {out}')
    fig_kernel(fd, params, out)
    fig_profit(fd, params, out)
    fig_map(fd, out)
    fig_energy(fd, out)


if __name__ == '__main__':
    main()
