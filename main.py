

import math
import numpy as np
import pandas as pd
import simpy
import random
import matplotlib.pyplot as plt


BASE = {
    "means":  {"M1": 4.0, "M2": 6.0, "M3": 5.0},        # min per part
    "mtbf":   {"M1": 240, "M2": 180, "M3": 200},        # mean OPERATING min between failures
    "mttr":   {"M1": 15,  "M2": 20,  "M3": 15},         # mean repair time, min
    "counts": {"M1": 1, "M2": 1, "M3": 1},              # machines per station
    "buffer": 5,                                        # parts per buffer
    "breakdowns": True,
    "sim_time": 480 * 20,                               # 20 shifts
}
STATIONS = ["M1", "M2", "M3"]


#functions 

def process_time(rng, mean):
    #processing time random not less than 0.1 min 
    return max(0.1, rng.gauss(mean, 0.25 * mean))


def machine(env, name, station, cfg, in_buf, out_buf, stats, rng):
    time_since_repair = 0.0
    next_fail = rng.expovariate(1 / cfg["mtbf"][station]) if cfg["breakdowns"] else math.inf

    while True:
        if in_buf is not None:
            t0 = env.now
            part = yield in_buf.get()                    # waits here if buffer is empty
            stats["starved"][station] += env.now - t0    # time spent waiting = starved
        else:
            part = "raw"

        work = process_time(rng, cfg["means"][station])
        yield env.timeout(work)
        stats["busy"][station] += work
        time_since_repair += work

        if out_buf is not None:
            t0 = env.now
            yield out_buf.put(part)                      # waits here if next buffer is full
            stats["blocked"][station] += env.now - t0    # time spent waiting = blocked
        else:
            stats["finished"] += 1

        # Breakdown check
        if time_since_repair >= next_fail:
            repair = rng.expovariate(1 / cfg["mttr"][station])
            yield env.timeout(repair)
            stats["down"][station] += repair
            time_since_repair = 0.0
            next_fail = rng.expovariate(1 / cfg["mtbf"][station])


def monitor(env, buffers, levels):
    #record the no of items in buffer every min
    while True:
        for name, buf in buffers.items():
            levels[name].append(len(buf.items))
        yield env.timeout(1)


def run_once(cfg, seed):
    
    rng = random.Random(seed)
    env = simpy.Environment()
    buf1 = simpy.Store(env, capacity=cfg["buffer"])
    buf2 = simpy.Store(env, capacity=cfg["buffer"])

    stats = {"busy": {s: 0.0 for s in STATIONS},
             "down": {s: 0.0 for s in STATIONS},
             "blocked": {s: 0.0 for s in STATIONS},
             "starved": {s: 0.0 for s in STATIONS},
             "finished": 0}
    levels = {"buf1": [], "buf2": []}

    # input/output buffers for each station
    io = {"M1": (None, buf1), "M2": (buf1, buf2), "M3": (buf2, None)}
    for s in STATIONS:
        for i in range(cfg["counts"][s]):
            env.process(machine(env, f"{s}-{i}", s, cfg, io[s][0], io[s][1], stats, rng))
    env.process(monitor(env, {"buf1": buf1, "buf2": buf2}, levels))

    env.run(until=cfg["sim_time"])

    T = cfg["sim_time"]
    result = {"throughput": stats["finished"] / (T / 60)}
    for s in STATIONS:
        n = cfg["counts"][s]
        result[f"util_{s}"] = stats["busy"][s] / (T * n) * 100
        result[f"down_{s}"] = stats["down"][s] / (T * n) * 100
        result[f"blocked_{s}"] = stats["blocked"][s] / (T * n) * 100
        result[f"starved_{s}"] = stats["starved"][s] / (T * n) * 100
    result["buf1_avg"] = float(np.mean(levels["buf1"]))
    result["buf2_avg"] = float(np.mean(levels["buf2"]))
    return result


def run_replications(cfg, n=30):
    #run it in different seeds
    return pd.DataFrame([run_once(cfg, seed) for seed in range(n)])


def summarize(df):
    mean = df["throughput"].mean()
    half = 1.96 * df["throughput"].std() / math.sqrt(len(df))
    return mean, half


def make_scenario(**changes):
    
    cfg = {k: (v.copy() if isinstance(v, dict) else v) for k, v in BASE.items()}
    for key, val in changes.items():
        cfg[key] = val
    return cfg


def compare_scenarios(scenarios, n=30):
    
    rows = []
    details = {}                       # full results of every scenario, for the bottleneck chart
    for name, cfg in scenarios.items():
        df = run_replications(cfg, n)
        details[name] = df
        mean, half = summarize(df)
        rows.append({
            "scenario": name,
            "throughput": mean,
            "ci_95_+/-": half,
            "util_M1": df["util_M1"].mean(),
            "util_M2": df["util_M2"].mean(),
            "util_M3": df["util_M3"].mean(),
            "buf1_avg": df["buf1_avg"].mean(),
            "buf2_avg": df["buf2_avg"].mean(),
        })
    return pd.DataFrame(rows).set_index("scenario"), details


def find_bottleneck(df):
    utils = {s: df[f"util_{s}"].mean() for s in STATIONS}
    return max(utils, key=utils.get), utils


def state_table(df):
    #what machine spends its tim in what state
    table = pd.DataFrame({
        "Working":     [df[f"util_{s}"].mean() for s in STATIONS],
        "Broken down": [df[f"down_{s}"].mean() for s in STATIONS],
        "Blocked":     [df[f"blocked_{s}"].mean() for s in STATIONS],
        "Starved":     [df[f"starved_{s}"].mean() for s in STATIONS],
    }, index=STATIONS)
    table["Total"] = table.sum(axis=1)
    return table


def plot_bottleneck(details, filename):
    states = [("util", "Working", "seagreen"),
              ("down", "Broken down", "firebrick"),
              ("blocked", "Blocked (next buffer full)", "orange"),
              ("starved", "Starved (no parts to work on)", "lightgray")]

    fig, axes = plt.subplots(1, len(details), figsize=(17, 5.5), sharey=True)
    for ax, (name, df) in zip(axes, details.items()):
        bottom = np.zeros(len(STATIONS))
        for key, label, color in states:
            vals = np.array([df[f"{key}_{s}"].mean() for s in STATIONS])
            ax.bar(STATIONS, vals, bottom=bottom, color=color, label=label)
            bottom += vals
        b, _ = find_bottleneck(df)
        ax.text(STATIONS.index(b), 103, "BOTTLENECK", ha="center",
                fontsize=8, fontweight="bold", color="crimson")
        ax.set_title(name, fontsize=9)
        ax.set_ylim(0, 112)
    axes[0].set_ylabel("% of simulated time")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4)
    fig.suptitle("Where is the bottleneck? How each machine spends its time", fontsize=12)
    plt.tight_layout(rect=(0, 0.07, 1, 0.95))
    plt.savefig(filename, dpi=150)
    plt.close()


#analysis

if __name__ == "__main__":
    scenarios = {
        "1. Baseline":            make_scenario(),
        "2. Bigger buffers (15)": make_scenario(buffer=15),
        "3. Faster M2 (5.0 min)": make_scenario(means={"M1": 4.0, "M2": 5.0, "M3": 5.0}),
        "4. Second M2 machine":   make_scenario(counts={"M1": 1, "M2": 2, "M3": 1}),
        "5. No breakdowns (ideal)": make_scenario(breakdowns=False),
    }

    results, details = compare_scenarios(scenarios, n=30)
    print(results.round(2).to_string())

    #bottleneck analysis
    print("\nBOTTLENECK PER SCENARIO (station with the highest utilization):")
    for name, df in details.items():
        b, utils = find_bottleneck(df)
        print(f"  {name:28s} -> {b}  ({utils[b]:.1f}% working)")

    print("\nBASELINE: how each machine spends its time (%):")
    print(state_table(details["1. Baseline"]).round(1).to_string())

    plot_bottleneck(details, "bottleneck_analysis.png")
    print("\nChart saved: bottleneck_analysis.png")

    # Chart
    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.bar(results.index, results["throughput"], yerr=results["ci_95_+/-"],
           capsize=5, color="steelblue")
    ax.set_ylabel("Throughput (parts/hour)")
    ax.set_title("Throughput by scenario (mean of 30 runs, 95% CI)")
    plt.xticks(rotation=20, ha="right")
    plt.tight_layout()
    plt.savefig("scenario_throughput.png", dpi=150)
    print("\nChart saved: scenario_throughput.png")