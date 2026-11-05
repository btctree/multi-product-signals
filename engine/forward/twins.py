"""The random twins: the same days and the same number of picks as LIVE, random uptrend names from the frozen list. Frozen.

Follows the review's placebo tables (s35_tables.py, type P2) with two changes, both so that a reading six months on cannot move
a twin's past:

  * Each (seed, group, day) has its own numpy stream, from SeedSequence(entropy, spawn_key=...), never crc32. One long stream per
    seed would shift every later pick each time a new day was added.
  * A pick is the n smallest of one uniform per frozen name, so a name dropping out of a day changes only that day's picks that
    involved it, not everyone's position in a list.

A twin runs as variant "D3": E cards with no E1 filter, D3 cards ranked after them. Its picks are already counted after E1, and
filtering a random name again would break "the same number of picks". Ties are broken by its own random table (rank "feat").
"""
import numpy as np

from .rules import group_of


def window_keys(gdates, start_day):
    """{group: first bar day whose cards a run from start_day on can read}: the last bar before the start."""
    out = {}
    for g, gd in gdates.items():
        j = int(np.searchsorted(gd, start_day - 1, "right")) - 1
        out[g] = int(gd[max(j, 0)])
    return out


def live_counts(cards, e1, tickers, first):
    """{(group, day): (E picks, D3 picks)} as LIVE's card list would read them: E1 applied, a D3 card only if the name has no E card."""
    out = {}
    for key, lst in cards.items():
        g, d = key
        if d < first.get(g, 10 ** 9):
            continue
        seen, n_e, n_d = set(), 0, 0
        for kind, t, i in lst:
            if kind == "E":
                if tickers[t]["m"] == "US":
                    v = e1.get(t, {}).get(i)
                    if v is not None and not v:
                        continue
                seen.add(t)
                n_e += 1
        for kind, t, i in lst:
            if kind == "D3" and t not in seen:
                n_d += 1
        if n_e + n_d:
            out[key] = (n_e, n_d)
    return out


def eligible(tickers, keys, order):
    """{(group, day): (frozen-list positions, bars)}: names of the group in an uptrend with a bar that day and enough history."""
    by_g = {}
    for g, d in keys:
        by_g.setdefault(g, []).append(d)
    acc = {k: [] for k in keys}
    for t, r in tickers.items():
        g = r["group"]
        if g not in by_g or r["m"] == "CRYPTO":
            continue
        kd = np.array(sorted(by_g[g]), dtype=np.int64)
        pos = np.searchsorted(r["dn"], kd)
        pos[pos >= len(r["dn"])] = len(r["dn"]) - 1
        hit = r["dn"][pos] == kd
        for d, i in zip(kd[hit], pos[hit]):
            if r["UP"][i]:
                acc[(g, int(d))].append((order[t], int(i)))
    return {k: (np.array([x[0] for x in v], dtype=np.int64), np.array([x[1] for x in v], dtype=np.int64)) for k, v in acc.items()}


def twin_table(seed, counts, elig, frozen_names, entropy):
    """One twin: (cards, fpct, short draws). Crypto cards are added by the caller: never bought, they only take list room."""
    groups = sorted({group_of(t) for t in frozen_names})
    gid = {g: k for k, g in enumerate(groups)}
    n_names = len(frozen_names)
    cards, fpct, short = {}, {}, 0
    for key in sorted(counts):
        g, d = key
        n_e, n_d = counts[key]
        ids, bars = elig[key]
        n = n_e + n_d
        if len(ids) < n:
            short += n - len(ids)
            n = len(ids)
        if n == 0:
            continue
        rng = np.random.default_rng(np.random.SeedSequence(entropy=entropy, spawn_key=(seed, gid[g], d)))
        u, p = rng.random(n_names), rng.random(n_names)
        pick = np.argsort(u[ids], kind="stable")[:n]
        lst = []
        for j, q in enumerate(pick):
            t, i = frozen_names[ids[q]], int(bars[q])
            lst.append(("E" if j < n_e else "D3", t, i))
            fpct[(t, i)] = float(p[ids[q]])
        cards[key] = lst
    return cards, fpct, short
