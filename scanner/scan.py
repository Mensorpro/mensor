"""Read-only Polymarket arbitrage scanner.

Checks, using real order-book asks/bids (not midpoints):
  1. Binary markets: buy YES + buy NO for < $1  (or sell both for > $1, via split)
  2. Multi-outcome (neg-risk / exhaustive) events: buy YES of every outcome for < $1
Reports size-aware profit: walks the book so profit is for fillable size.
"""
import json, sys, time, requests
from concurrent.futures import ThreadPoolExecutor

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
S = requests.Session()

def get_events():
    out, off = [], 0
    while True:
        r = S.get(f"{GAMMA}/events", params=dict(active="true", closed="false", limit=100, offset=off), timeout=30)
        if r.status_code == 422: break
        r.raise_for_status()
        b = r.json()
        if not b: break
        out += b; off += 100
        if off > 20000: break
    return out

def get_books(token_ids):
    books = {}
    ids = list(token_ids)
    def chunk(c):
        r = S.post(f"{CLOB}/books", json=[{"token_id": t} for t in c], timeout=30)
        r.raise_for_status()
        return r.json()
    chunks = [ids[i:i+50] for i in range(0, len(ids), 50)]
    with ThreadPoolExecutor(8) as ex:
        for res in ex.map(chunk, chunks):
            for b in res: books[b["asset_id"]] = b
    return books

def levels(book, side):
    ls = [(float(x["price"]), float(x["size"])) for x in book.get(side, [])]
    return sorted(ls) if side == "asks" else sorted(ls, reverse=True)

def basket_cost(books_levels, max_size=1e9):
    """Walk asks of several legs in lockstep; return (shares, total_cost, avg_cost_per_set) for sets with marginal cost<1."""
    idx = [0]*len(books_levels); rem = [l[0][1] if l else 0 for l in books_levels]
    shares = cost = 0.0
    if any(not l for l in books_levels): return 0, 0
    while shares < max_size:
        marg = sum(l[i][0] for l, i in zip(books_levels, idx))
        if marg >= 1: break
        q = min(rem)
        shares += q; cost += q*marg
        for k in range(len(idx)):
            rem[k] -= q
            if rem[k] <= 1e-9:
                idx[k] += 1
                if idx[k] >= len(books_levels[k]): return shares, cost
                rem[k] = books_levels[k][idx[k]][1]
    return shares, cost

def main():
    ev = get_events()
    print(f"events: {len(ev)}", file=sys.stderr)
    markets = []  # (event, market, yes_tok, no_tok)
    for e in ev:
        for m in e.get("markets", []):
            if not m.get("active") or m.get("closed") or not m.get("enableOrderBook"): continue
            try: toks = json.loads(m["clobTokenIds"])
            except Exception: continue
            if len(toks) != 2: continue
            markets.append((e, m, toks[0], toks[1]))
    print(f"orderbook markets: {len(markets)}", file=sys.stderr)
    # prefilter on gamma best prices to limit book calls: fetch all anyway (cheap batched)
    books = get_books([t for _, _, y, n in markets for t in (y, n)])
    print(f"books: {len(books)}", file=sys.stderr)
    res = []
    # 1. binary
    for e, m, y, n in markets:
        by, bn = books.get(y), books.get(n)
        if not by or not bn: continue
        ay, an = levels(by, "asks"), levels(bn, "asks")
        sh, c = basket_cost([ay, an])
        if sh > 0:
            res.append(dict(kind="BUY-BOTH", q=m["question"], end=m.get("endDate"), shares=sh, cost=c, profit=sh-c, roi=(sh-c)/c, slug=e.get("slug")))
        by_, bn_ = levels(by, "bids"), levels(bn, "bids")
        # sell both: proceeds = bid_y + bid_n > 1 per set (mint set for $1)
        i = j = 0; sh = pr = 0.0
        if by_ and bn_:
            ry, rn = by_[0][1], bn_[0][1]
            while i < len(by_) and j < len(bn_) and by_[i][0] + bn_[j][0] > 1:
                q = min(ry, rn); sh += q; pr += q*(by_[i][0]+bn_[j][0]-1)
                ry -= q; rn -= q
                if ry <= 1e-9:
                    i += 1
                    if i < len(by_): ry = by_[i][1]
                if rn <= 1e-9:
                    j += 1
                    if j < len(bn_): rn = bn_[j][1]
            if sh > 0:
                res.append(dict(kind="SELL-BOTH", q=m["question"], end=m.get("endDate"), shares=sh, cost=sh, profit=pr, roi=pr/sh, slug=e.get("slug")))
    # 2. multi-outcome neg-risk events: buy all YES
    for e in ev:
        if not e.get("negRisk") or e.get("negRiskAugmented"): continue  # augmented = open-ended w/ placeholders/Other: not exhaustive
        if e.get("endDate") and e["endDate"] < time.strftime("%Y-%m-%dT%H:%M:%SZ"): continue  # stale
        ms = [x for x in markets if x[0]["id"] == e["id"]]
        total = [x for x in e.get("markets", []) if x.get("active") and not x.get("closed")]
        if len(ms) < 3 or len(ms) != len(total): continue
        ls = []
        for _, m, y, n in ms:
            b = books.get(y)
            if not b: break
            ls.append(levels(b, "asks"))
        else:
            sh, c = basket_cost(ls)
            if sh > 0:
                res.append(dict(kind=f"NEGRISK-BUY-ALL-YES({len(ms)})", q=e["title"], end=e.get("endDate"), shares=sh, cost=c, profit=sh-c, roi=(sh-c)/c, slug=e.get("slug")))
    res.sort(key=lambda r: -r["profit"])
    json.dump(res, open("results.json", "w"), indent=1)
    print(f"{'kind':28}{'profit$':>9}{'roi%':>7}{'cost$':>9}  end         question")
    for r in res[:40]:
        print(f"{r['kind']:28}{r['profit']:9.2f}{100*r['roi']:7.2f}{r['cost']:9.0f}  {str(r['end'])[:10]}  {r['q'][:70]}")
    print(f"\ntotal opportunities: {len(res)}  sum profit: ${sum(r['profit'] for r in res):.2f}")

if __name__ == "__main__":
    main()
