#!/usr/bin/env python3
"""Firebase Cloud Functions cost calculator — TEMPLATE (share freely).

Measures YOUR functions' real execution counts, durations, and memory from
Cloud Monitoring, then applies Firebase Gen 1 pricing per function.

==========================================================================
README — FOLLOW THESE STEPS IN ORDER
==========================================================================
STEP 0 — What you need
  - A Firebase/GCP project with Cloud Functions (Gen 1) deployed.
  - Python 3.10+ and (for measured mode) the Google Cloud SDK (`gcloud`).

STEP 1 — Install uv (recommended) OR use pip
  Option A — uv (fast, manages the environment for you):
      curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS/Linux
      # Windows (PowerShell): irm https://astral.sh/uv/install.ps1 | iex
  Option B — plain pip (no new tools):
      # nothing to install; python3 -m pip ships with Python

STEP 2 — Create a project-local environment (do NOT install into system Python)
  With uv:
      cd YOUR-PROJECT-FOLDER
      uv venv                      # creates ./.venv (editors auto-detect it)
  With pip:
      cd YOUR-PROJECT-FOLDER
      python3 -m venv .venv        # creates ./.venv
      source .venv/bin/activate    # Windows: .venv\\Scripts\\activate
  If pip says "externally-managed-environment", that means you skipped this
  step — go back and create the venv. Never use --break-system-packages.

STEP 3 — Install the Cloud Monitoring library (measured mode only)
  With uv:    uv pip install google-cloud-monitoring
  With pip:   pip install google-cloud-monitoring   (venv activated)
  (Estimate mode below needs NO dependencies at all.)

STEP 4 — Authenticate (measured mode only)
      gcloud auth application-default login
      gcloud auth application-default set-quota-project YOUR-PROJECT-ID
      gcloud services enable monitoring.googleapis.com --project=YOUR-PROJECT-ID
  Your account also needs the Monitoring Viewer role on the project
  (IAM -> monitoring.viewer). Stale credentials? Run
  `gcloud auth application-default revoke` first, then login again.

STEP 5 — Tell the script YOUR function names (pick ONE option)
  Option A — automatic pull (recommended). Point FUNCTION_SOURCE below at
             YOUR repo's functions/index.js. The pull_function_names()
             helper reads every `exports.MyFn = ...` line, which matches
             the deployed function names Cloud Monitoring reports.
  Option B — manual list. Paste your deployed names into FUNCTION_NAMES:
             ["createOrder", "stripeWebhook", ...]. Find them with:
             `firebase functions:list`  (or Cloud Console -> Functions).
  If FUNCTION_NAMES is non-empty it wins; otherwise the script pulls from
  FUNCTION_SOURCE. Run `python3 this-script.py --list` to verify.

STEP 6 — Run it
  Measured (real traffic, last 7 days scaled to a 30-day month):
      uv run python calc-function-costs-template.py --project YOUR-PROJECT-ID
      # or with pip+venv activated:  python calc-function-costs-template.py --project ...
  Offline estimate (no GCP access; assumes same traffic for every function):
      uv run python calc-function-costs-template.py --estimate \\
          --invocations 50000 --avg-ms 400
  Per-function assumptions from JSON:
      uv run python calc-function-costs-template.py --estimate --input traffic.json
      # traffic.json: {"myFn": {"invocations": 50000, "avg_duration_ms": 300,
      #                          "memory_mb": 256, "max_memory_used_mb": 120}}

STEP 7 — Read the output
  Per function: invocations/mo, avg ms, memory, max memory used, GB-s per
  invocation, cost per invocation, 1M-invocation projection, $/month, and a
  "hint" column flagging over/under-provisioned memory. The TOTAL row shows
  gross cost and cost after the Blaze free tier (2M inv, 400K GB-s, 200K
  GHz-s). Egress is NOT included.

ADAPTING THIS TEMPLATE
  - Different default memory? Change DEFAULT_MEMORY_MB (Gen 1 default 256).
  - Per-function memory/timeout overrides (runWith)? Either list them in your
    --input JSON (memory_mb per function) or extend pull_function_names().
  - Gen 2 functions? Different pricing (vCPU-based, min-instance cost).
    The formulas below are Gen 1 ONLY — do not trust them for Gen 2.
  - Rates change. Verify at https://cloud.google.com/functions/pricing.
==========================================================================
"""
import argparse
import json
import re
import sys
from pathlib import Path

# ------------------------------------------------------------------ pricing
# Gen 1 pricing. Verify current rates at cloud.google.com/functions/pricing.
INVOCATION_PRICE = 0.40 / 1_000_000   # $ per invocation
GB_SECOND_PRICE = 0.0000025           # $ per GB-second
GHZ_SECOND_PRICE = 0.0000100          # $ per GHz-second
# Gen 1 CPU (GHz) allotted per memory tier (MB).
CPU_GHZ = {128: 0.2, 256: 0.4, 512: 0.8, 1024: 1.4,
           2048: 2.4, 4096: 4.8, 8192: 4.8}
DEFAULT_MEMORY_MB = 256               # Gen 1 default when no runWith override
FREE_INVOCATIONS = 2_000_000          # Blaze free tier, per month
FREE_GB_SEC = 400_000
FREE_GHZ_SEC = 200_000

# ------------------------------------------------- YOUR FUNCTIONS (STEP 5)
# Option A: path to YOUR functions/index.js (auto-pull). Relative paths
# resolve from THIS script's folder; e.g. if this file lives in scripts/
# next to your functions/ folder, "../functions/index.js" just works.
FUNCTION_SOURCE = "../functions/index.js"
# Option B: paste deployed names here to skip auto-pull (wins if non-empty).
FUNCTION_NAMES = [
    # "createOrder",
    # "stripeWebhook",
]


def pull_function_names(index_js):
    """Read deployed function names from a firebase functions/index.js.

    Every `exports.<Name> = ...` line becomes one deployed function, and
    Cloud Monitoring reports metrics under that same <Name>, so the pull
    doubles as the exact key list for metric queries. Returns [] (with a
    warning) instead of crashing when the file is missing.
    """
    path = Path(index_js)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    if not path.exists():
        print(f"WARNING: {path} not found. Set FUNCTION_SOURCE to your "
              f"functions/index.js, or paste names into FUNCTION_NAMES.",
              file=sys.stderr)
        return []
    names = re.findall(r"^exports\.([A-Za-z0-9_]+)\s*=",
                       path.read_text(), re.M)
    return sorted(set(names))


def resolve_functions():
    if FUNCTION_NAMES:
        return sorted(set(FUNCTION_NAMES))
    return pull_function_names(FUNCTION_SOURCE)


def billed_ms(duration_ms):
    """Gen 1 bills 100ms minimum, rounded up to 100ms increments."""
    import math
    return max(100, math.ceil(duration_ms / 100) * 100)


def cost_per_invocation(avg_ms, memory_mb):
    """The three Gen 1 metrics for ONE invocation. Returns dict of parts."""
    s = billed_ms(avg_ms) / 1000
    gb = memory_mb / 1024
    ghz = CPU_GHZ.get(memory_mb, CPU_GHZ[256])
    gb_s = gb * s
    ghz_s = ghz * s
    return {
        "billed_ms": billed_ms(avg_ms),
        "gb_s": gb_s,
        "ghz_s": ghz_s,
        "invocation_usd": INVOCATION_PRICE,
        "memory_usd": gb_s * GB_SECOND_PRICE,
        "cpu_usd": ghz_s * GHZ_SECOND_PRICE,
        "total_usd": (INVOCATION_PRICE + gb_s * GB_SECOND_PRICE
                      + ghz_s * GHZ_SECOND_PRICE),
    }


def cost_row(name, invocations, avg_ms, memory_mb, max_used_mb=None):
    c = cost_per_invocation(avg_ms, memory_mb)
    return {
        "function": name, "invocations": invocations, "avg_ms": avg_ms,
        "billed_ms": c["billed_ms"], "memory_mb": memory_mb,
        "max_used_mb": max_used_mb, "gb_s_per_inv": c["gb_s"],
        "cost_per_inv": c["total_usd"], "cost_1m": c["total_usd"] * 1_000_000,
        "monthly_gb_s": c["gb_s"] * invocations,
        "monthly_ghz_s": c["ghz_s"] * invocations,
        "monthly_cost": c["total_usd"] * invocations,
    }


def rightsize_hint(row):
    used, alloc = row["max_used_mb"], row["memory_mb"]
    if not used or alloc <= 128:
        return ""
    tiers = sorted(CPU_GHZ)
    if used < alloc * 0.6:
        lower = [t for t in tiers if t < alloc and t >= used * 1.25]
        if lower:
            return f"try {lower[0]}MB"
    if used > alloc * 0.9:
        higher = [t for t in tiers if t > alloc]
        if higher:
            return f"near limit, consider {higher[0]}MB"
    return ""


def print_report(rows, title, apply_free_tier=True):
    print(f"\n{title}")
    print(f"{'function':28s} {'inv/mo':>10s} {'avg ms':>8s} {'mem':>5s} "
          f"{'maxuse':>7s} {'GB-s/inv':>9s} {'$/inv':>12s} {'$1M proj':>9s} "
          f"{'$/mo':>9s}  hint")
    for r in sorted(rows, key=lambda x: -x["monthly_cost"]):
        used = str(int(r["max_used_mb"])) if r["max_used_mb"] else "-"
        print(f"{r['function']:28s} {r['invocations']:10.0f} {r['avg_ms']:8.0f} "
              f"{r['memory_mb']:5d} {used:>7s} {r['gb_s_per_inv']:9.5f} "
              f"${r['cost_per_inv']:11.8f} ${r['cost_1m']:8.2f} "
              f"${r['monthly_cost']:8.2f}  {rightsize_hint(r)}")
    tot_inv = sum(r["invocations"] for r in rows)
    tot_gb = sum(r["monthly_gb_s"] for r in rows)
    tot_ghz = sum(r["monthly_ghz_s"] for r in rows)
    gross = sum(r["monthly_cost"] for r in rows)
    print(f"\n{'TOTAL':28s} {tot_inv:10.0f}")
    print(f"  total GB-s/mo: {tot_gb:,.0f}   total GHz-s/mo: {tot_ghz:,.0f}   "
          f"gross: ${gross:.2f}/mo")
    if apply_free_tier:
        net = (max(0, tot_inv - FREE_INVOCATIONS) * INVOCATION_PRICE
               + max(0, tot_gb - FREE_GB_SEC) * GB_SECOND_PRICE
               + max(0, tot_ghz - FREE_GHZ_SEC) * GHZ_SECOND_PRICE)
        print(f"  after Blaze free tier: ${net:.2f}/mo")
    print("  (excludes networking egress; verify rates at "
          "cloud.google.com/functions/pricing)")


def fetch_monitoring(project, region, days, functions):
    from datetime import datetime, timedelta, timezone
    from google.cloud import monitoring_v3

    client = monitoring_v3.MetricServiceClient()
    proj = f"projects/{project}"
    end = datetime.now(timezone.utc)
    interval = monitoring_v3.TimeInterval(
        {"end_time": {"seconds": int(end.timestamp())},
         "start_time": {"seconds": int((end - timedelta(days=days)).timestamp())}})
    window_s = days * 86400

    def query(metric, aligner, value_fn):
        out = {}
        req = {"name": proj,
               "filter": (f'metric.type="{metric}" '
                          f'AND resource.labels.region="{region}"'),
               "interval": interval,
               "aggregation": {
                   "alignment_period": {"seconds": window_s},
                   "per_series_aligner": aligner,
                   "group_by_fields": ["resource.labels.function_name"]}}
        for ts in client.list_time_series(request=req):
            pts = list(ts.points)
            if pts:
                out[ts.resource.labels.get("function_name", "?")] = value_fn(pts[0].value)
        return out

    A = monitoring_v3.Aggregation.Aligner
    counts = query("cloudfunctions.googleapis.com/function/execution_count",
                   A.ALIGN_SUM, lambda v: v.int64_value)
    times = query("cloudfunctions.googleapis.com/function/execution_times",
                  A.ALIGN_DELTA,
                  lambda v: (v.distribution_value.mean, v.distribution_value.count))
    mem = query("cloudfunctions.googleapis.com/function/user_memory_bytes",
                A.ALIGN_MAX, lambda v: v.double_value / (1024 * 1024))

    scale = 30 / days
    rows = []
    for fn in functions:
        n = counts.get(fn, 0)
        mean_s, dist_n = times.get(fn, (0.0, 0))
        rows.append(cost_row(fn, n * scale, (mean_s * 1000) if dist_n else 1,
                             DEFAULT_MEMORY_MB, max_used_mb=mem.get(fn)))
    return rows


def run_estimate(functions, args):
    per_fn = json.loads(Path(args.input).read_text()) if args.input else {}
    # Union: input-file keys run even if they are not in the pulled list.
    functions = sorted(set(functions) | set(per_fn))
    rows = [cost_row(fn,
                     per_fn.get(fn, {}).get("invocations", args.invocations),
                     per_fn.get(fn, {}).get("avg_duration_ms", args.avg_ms),
                     per_fn.get(fn, {}).get("memory_mb", DEFAULT_MEMORY_MB),
                     per_fn.get(fn, {}).get("max_memory_used_mb"))
            for fn in functions]
    tag = f"from {args.input}" if args.input else "ASSUMED flags for all"
    print_report(rows, f"ESTIMATE (offline, {tag})",
                 apply_free_tier=not args.no_free_tier)


def main():
    ap = argparse.ArgumentParser(description="Firebase Gen 1 cost calculator (template)")
    ap.add_argument("--project", default=None, help="GCP project id (measured mode)")
    ap.add_argument("--region", default="us-central1")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--input", help="JSON with per-function assumptions")
    ap.add_argument("--invocations", type=float, default=100_000)
    ap.add_argument("--avg-ms", type=float, default=500)
    ap.add_argument("--no-free-tier", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    functions = resolve_functions()
    if args.list:
        if not functions:
            sys.exit("No functions found. Do STEP 5: set FUNCTION_SOURCE or "
                     "fill in FUNCTION_NAMES (see README at the top of this file).")
        print(f"{len(functions)} functions:")
        print("\n".join(functions))
        return
    if args.estimate:
        run_estimate(functions, args)
        return
    if not args.project:
        sys.exit("Measured mode needs --project YOUR-PROJECT-ID (or use --estimate).")
    if not functions:
        sys.exit("No functions found. Do STEP 5 first (see README header).")
    try:
        rows = fetch_monitoring(args.project, args.region, args.days, functions)
    except ImportError:
        sys.exit("google-cloud-monitoring not installed (STEP 3), or use --estimate.")
    except Exception as e:  # noqa: BLE001 - surface auth/API errors plainly
        sys.exit(f"Monitoring query failed ({e}).\n"
                 "Check STEP 4: fresh `gcloud auth application-default login` + "
                 "quota project + monitoring.viewer role.")
    print_report(rows, f"MEASURED ({args.project}/{args.region}, last {args.days}d -> 30d)",
                 apply_free_tier=not args.no_free_tier)


if __name__ == "__main__":
    main()
