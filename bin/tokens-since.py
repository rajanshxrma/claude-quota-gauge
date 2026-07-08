#!/usr/bin/env python3
"""Sum Claude Code usage per model since a given ISO8601 UTC timestamp, weighted
by dollar cost rather than raw token count. Reads raw local session transcripts
directly -- no ccusage dependency, no calendar-week bucketing.

Why cost-weighted: Anthropic's weekly usage pool is metered by compute cost,
not token count. Output tokens run 5x the price of input on every current
model, cache reads are ~10% of input price, cache writes are ~125%, and Fable
5 costs 2x Opus per token. Summing raw tokens 1:1 drifts from the real
percentage whenever the mix of token types or models shifts between
calibrations -- weighting by list price is the closest available proxy for
the underlying compute-cost metric.

Usage: tokens-since.py <iso_start>
"""
import sys, os, glob, json

# $ per 1M tokens: (input, output). Cache write is 1.25x input (5m TTL,
# the default and only TTL these transcripts use); cache read is 0.1x input.
PRICING = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-1": (5.00, 25.00),
    "claude-opus-4-0": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.1
FALLBACK_PRICING = PRICING["claude-sonnet-5"]  # unknown/future model IDs


def cost(model, usage):
    price_in, price_out = PRICING.get(model, FALLBACK_PRICING)
    weighted_input = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0) * CACHE_WRITE_MULT
        + usage.get("cache_read_input_tokens", 0) * CACHE_READ_MULT
    )
    return (weighted_input * price_in + usage.get("output_tokens", 0) * price_out) / 1_000_000


def main():
    start = sys.argv[1]
    root = os.path.expanduser("~/.claude/projects")
    totals = {}
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            with open(path, "r") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ts = d.get("timestamp")
                    if not ts or ts < start:
                        continue
                    msg = d.get("message", {})
                    usage = msg.get("usage")
                    model = msg.get("model")
                    if not usage or not model:
                        continue
                    totals[model] = totals.get(model, 0) + cost(model, usage)
        except Exception:
            continue
    print(json.dumps(totals))

if __name__ == "__main__":
    main()
