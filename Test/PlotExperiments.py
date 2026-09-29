"""Plot measured cluster results as PNG and SVG. No benchmark data are synthesized."""
import argparse
import collections
import json
from pathlib import Path
import statistics
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator, EngFormatter

COLORS = ['#167D9A', '#EE964B', '#7B61A8', '#38977F', '#BD536B']
MODES = {'latency': 'Low latency', 'throughput': 'High throughput'}
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'axes.titlesize': 13,
                     'axes.titleweight': 'bold', 'axes.labelcolor': '#334155', 'text.color': '#172A3A',
                     'axes.spines.top': False, 'axes.spines.right': False, 'axes.spines.left': False,
                     'axes.spines.bottom': False, 'axes.grid': True, 'grid.alpha': .16,
                     'grid.color': '#64748B', 'axes.prop_cycle': plt.cycler(color=COLORS),
                     'legend.frameon': False, 'figure.facecolor': 'white', 'savefig.facecolor': 'white'})


def Load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def Save(fig, output, name):
    fig.savefig(output/(name+'.png'), dpi=220, bbox_inches='tight')
    fig.savefig(output/(name+'.svg'), bbox_inches='tight')
    plt.close(fig)


def Curve(ax, rows, xkey, ykey, label, color, style='-'):
    groups = collections.defaultdict(list)
    for row in rows:
        if row['metrics'].get(ykey) is not None:
            groups[row['case'][xkey]].append(row['metrics'][ykey])
    if not groups:
        return
    xs = sorted(groups)
    middle = [statistics.median(groups[x]) for x in xs]
    ax.plot(xs, middle, style, color=color, marker='s' if style == '--' else 'o', markersize=5, linewidth=2, label=label)
    ax.fill_between(xs, [min(groups[x]) for x in xs], [max(groups[x]) for x in xs], color=color, alpha=.12)
    ax.autoscale(enable=True, axis='both')
    ax.set_ylim(bottom=0)


def PlotClock(root, rows, output):
    selected = [row for row in rows if row['case']['suite'] == 'sync']
    if not selected:
        return
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.5), layout='constrained')
    for enabled, color, label in [(False, COLORS[1], 'Correction off'), (True, COLORS[0], 'Correction on')]:
        runs = [Load(root/row['directory']/'clock.json') for row in selected if row['case']['sync'] == enabled]
        if not runs:
            continue
        grid = np.linspace(max(run[0]['seconds'] for run in runs), min(run[-1]['seconds'] for run in runs), 160)
        for axis, key in zip(axes, ['spread_ms', 'probe_uncertainty_ms']):
            values = np.array([np.interp(grid, [p['seconds'] for p in run], [p[key] for p in run]) for run in runs])
            axis.plot(grid, np.median(values, axis=0), color=color, label=label, linewidth=2)
            axis.fill_between(grid, values.min(axis=0), values.max(axis=0), color=color, alpha=.12)
            axis.set(xlabel='Elapsed time (s)', ylim=(0, None))
    axes[0].set(title='Clock synchronization', ylabel='Inter-node clock spread (ms)')
    axes[1].set(title='Probe uncertainty', ylabel='Maximum half round-trip time (ms)')
    axes[0].legend()
    Save(fig, output, '01-clock')


def PlotLatency(rows, output):
    selected = [row for row in rows if row['case']['suite'] == 'latency']
    patterns = sorted({row['case']['pattern'] for row in selected})
    if not patterns:
        return
    fig, axes = plt.subplots(1, len(patterns), figsize=(5*len(patterns), 3.7), squeeze=False, layout='constrained')
    for ax, pattern in zip(axes[0], patterns):
        for index, (mode, label) in enumerate(MODES.items()):
            group = [row for row in selected if row['case']['mode'] == mode and row['case']['pattern'] == pattern]
            Curve(ax, group, 'rate', 'p99_ms', label+' · 99th percentile', COLORS[index])
            Curve(ax, group, 'rate', 'p50_ms', label+' · median', COLORS[index], '--')
        ax.set(title=pattern.capitalize()+' arrivals', xlabel='Offered transactions / s', ylabel='Common inclusion latency (ms)')
        ax.xaxis.set_major_formatter(EngFormatter())
        ax.legend(fontsize=8)
    Save(fig, output, '02-latency')


def PlotThroughput(rows, output):
    selected = [row for row in rows if row['case']['suite'] == 'throughput']
    if not selected:
        return
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.7), layout='constrained')
    for index, (mode, label) in enumerate(MODES.items()):
        group = [row for row in selected if row['case']['mode'] == mode]
        Curve(axes[0], group, 'rate', 'common_commit_tps', label, COLORS[index])
        Curve(axes[1], group, 'rate', 'achieved_send_tps', label, COLORS[index])
    maximum = max(row['case']['rate'] for row in selected)*1.05
    for ax in axes:
        ax.plot([0, maximum], [0, maximum], '--', color='#94A3B8', linewidth=1, label='Offered load')
        ax.set(xlabel='Offered transactions / s', xlim=(0, maximum))
        ax.xaxis.set_major_formatter(EngFormatter()); ax.yaxis.set_major_formatter(EngFormatter())
        ax.legend(fontsize=8)
    axes[0].set(title='Persisted common-chain throughput', ylabel='Unique transactions / s')
    axes[1].set(title='Load generator delivery', ylabel='Sent unique transactions / s')
    Save(fig, output, '03-throughput')


def PlotScale(rows, output):
    selected = [row for row in rows if row['case']['suite'] == 'scale']
    if not selected:
        return
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.7), layout='constrained')
    rates = sorted({row['case']['rate'] for row in selected})
    for index, rate in enumerate(rates):
        group = [row for row in selected if row['case']['rate'] == rate]
        Curve(axes[0], group, 'nodes', 'common_commit_tps', f'Offered {rate:,} / s', COLORS[index % len(COLORS)])
    best = collections.defaultdict(list)
    for size in sorted({row['case']['nodes'] for row in selected}):
        for repeat in sorted({row['case']['repeat'] for row in selected}):
            group = [row for row in selected if row['case']['nodes'] == size and row['case']['repeat'] == repeat and row['metrics']['sustainable']]
            if group:
                best[size].append(max(row['metrics']['common_commit_tps'] for row in group))
    if best:
        measured = [{'case': {'nodes': size}, 'metrics': {'value': value}} for size, values in best.items() for value in values]
        Curve(axes[1], measured, 'nodes', 'value', 'Highest passing measured load', COLORS[0])
    else:
        axes[1].text(.5, .5, 'No load passed the sustainability criteria', ha='center', transform=axes[1].transAxes, fontsize=9)
    for ax in axes:
        ax.set(xlabel='Consensus nodes', ylabel='Unique transactions / s')
        ax.xaxis.set_major_locator(MaxNLocator(integer=True)); ax.yaxis.set_major_formatter(EngFormatter())
    axes[0].set_title('Scaling at fixed offered loads'); axes[0].legend(fontsize=8)
    axes[1].set_title('Sustainable measured throughput')
    Save(fig, output, '04-scale')


def PlotFault(root, rows, output):
    selected = [row for row in rows if row['case']['suite'] == 'fault']
    kinds = sorted({row['case']['fault']['kind'] for row in selected})
    if not kinds:
        return
    fig, axes = plt.subplots(len(kinds), 1, figsize=(9, 3*len(kinds)), squeeze=False, layout='constrained')
    for ax, kind in zip(axes[:, 0], kinds):
        # Show the run nearest the median recovery duration; every run remains in the raw output.
        group = [row for row in selected if row['case']['fault']['kind'] == kind]
        recovery = [row['metrics']['recovery_seconds'] for row in group if row['metrics']['recovery_seconds'] is not None]
        middle = statistics.median(recovery) if recovery else 0
        chosen = min(group, key=lambda row: abs(row['metrics']['recovery_seconds']-middle) if row['metrics']['recovery_seconds'] is not None else float('inf'))
        points = [json.loads(line) for line in (root/chosen['directory']/'timeline.jsonl').read_text().splitlines()]
        xs, rates = [], []
        for before, after in zip(points, points[1:]):
            xs.append(after['seconds'])
            # An offline node prevents observing an all-node common prefix; leave a gap.
            rates.append((after['common']['transactions']-before['common']['transactions'])/(after['seconds']-before['seconds'])
                         if before['common'] and after['common'] else np.nan)
        ax.plot(xs, rates, color=COLORS[0], linewidth=1.3, label='Common-chain throughput')
        twin = ax.twinx(); twin.grid(False)
        twin.plot([p['seconds'] for p in points], [sum(n['pool']+n['pending'] for n in p['nodes'] if n) for p in points],
                  color=COLORS[1], linewidth=1.3, label='Queued transaction copies')
        twin.set_ylabel('Queued copies', color=COLORS[1]); twin.set_ylim(bottom=0)
        events = chosen['metrics']['events']
        if len(events) >= 2:
            ax.axvspan(events[0]['seconds'], events[1]['seconds'], color='#94A3B8', alpha=.2, label='Fault active')
        ax.set(title=kind.capitalize()+f' · repetition {chosen["case"]["repeat"]}', xlabel='Elapsed time (s)', ylabel='Unique transactions / s')
        ax.legend(loc='upper left', fontsize=8); twin.legend(loc='upper right', fontsize=8)
    Save(fig, output, '05-recovery')


def PlotResources(rows, output):
    selected = [row for row in rows if row['case']['suite'] == 'throughput']
    if not selected:
        return
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.6), layout='constrained')
    fields = [('cpu_core_equivalents', 1, 'Busy processor cores'), ('peak_resident_bytes', 1024**3, 'Resident memory (GiB)'),
              ('sent_bytes_per_second', 1e6, 'Node traffic sent (MB/s)')]
    for ax, (field, divisor, label) in zip(axes, fields):
        for index, (mode, name) in enumerate(MODES.items()):
            group = []
            for row in selected:
                values = [r[field]/divisor for r in row['metrics'].get('resources', []) if r.get(field) is not None]
                if row['case']['mode'] == mode and values:
                    group.append({'case': row['case'], 'metrics': {'value': max(values)}})
            Curve(ax, group, 'rate', 'value', name, COLORS[index])
        ax.set(xlabel='Offered transactions / s', ylabel=label, title='Busiest node')
        ax.xaxis.set_major_formatter(EngFormatter())
    axes[0].legend(fontsize=8)
    Save(fig, output, '06-resources')


def Main(args):
    all_rows = Load(args.work/'results.json')
    rows = [row for row in all_rows if row['status'] == 'complete']
    output = args.output or args.work/'figures'; output.mkdir(parents=True, exist_ok=True)
    PlotClock(args.work, rows, output); PlotLatency(rows, output); PlotThroughput(rows, output)
    PlotScale(rows, output); PlotFault(args.work, rows, output); PlotResources(rows, output)
    (output/'README.md').write_text(
        '# Experiment figures\n\n'
        f'Completed cases: {len(rows)} / {len(all_rows)}. Source: `../results.json`.\n\n'
        'Lines show repetition medians; shaded regions show the observed minimum and maximum. '
        'A single repetition has no dispersion estimate. Sustainable throughput is the highest tested passing load. '
        'Recovery plots show the run nearest the median recovery time; gaps denote unavailable all-node observations. '
        'Latency includes polling and scheduled-arrival queueing. See EXPERIMENTS.md for definitions.\n', encoding='utf-8')
    print(json.dumps({'figures': [str(path) for path in sorted(output.glob('*.png'))]}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    Main(parser.parse_args())
