# 02 — Cardano domain primer

Just enough Cardano context to read the project's graphs intelligently. Skip if you already know the protocol; come back here when a chart's axis label confuses you.

## The chain's clock: slots and epochs

Cardano's time is measured in **slots**. Each slot is 1 second long. A slot may or may not produce a block — block production is probabilistic, weighted by stake. On mainnet today, roughly 5% of slots produce a block (≈ one block every 20 seconds on average, with occasional gaps).

Slots are grouped into **epochs**. From the Shelley era onward, one epoch = 432,000 slots = exactly 5 days. Epoch numbers (0, 1, 2, …) advance every 5 days. The current epoch on preprod is in the high 200s (preprod started in 2022).

### Byron-era epochs were shorter — and that matters during catch-up

Before the Shelley hard fork, the Byron era used **21,600-slot epochs (~12 hours)** — twenty times smaller than Shelley+ epochs. On mainnet these are ancient history (epochs 0–207). On testnets that were bootstrapped through all eras quickly (preprod was launched in 2022 with all eras enabled in rapid succession), the early Byron-era epochs were even shorter and burn through extremely fast during a fresh catch-up sync.

Concretely: a 21,600-slot epoch on preprod can be ingested by `cardano-node` in 1-2 seconds of wall-clock time during catch-up. The default monitor sample interval is 10 seconds, which is **5–10× slower** than that. The monitor can sample epoch N, then 10 seconds later find the node is already in epoch N+3 — epochs N+1 and N+2 were burned through entirely between samples and never observed.

If you want to capture every early epoch in the SQLite stats DB, drop `--interval` to 1 or 2 seconds for catch-up runs. For A/B comparison purposes the missed epochs usually don't matter — both runs miss them equally, and the contribution to total sync time is tiny (a Byron epoch on preprod takes 1-2 seconds of wall-clock; even a half-dozen missed epochs add up to under a minute).

This is also why the **node-plot.py era bar chart shows a sub-line caveat** about observed-vs-actual epoch counts: short Byron epochs may be undercounted at default sampling rates.

So when you see in the monitor output:

```
Slot 124104574 | Epoch 291 | Era Conway | ...
```

That's "we're at slot 124,104,574 of the chain, which falls in epoch 291" (`124104574 / 432000 ≈ 287` — but epoch numbering isn't quite slot/432000 because of a Byron-era offset; trust the value from cardano-cli rather than computing it).

### Why epochs matter for monitoring

The chain reaches certain "consistency points" at epoch boundaries:

- **Stake snapshots** — at each epoch boundary, the chain records the stake distribution that determines who gets to produce blocks in the next epoch. This is an O(n) operation across all stake addresses, and db-sync has to materialize it into its `epoch_stake` table. On mainnet that's ~1M rows per snapshot, and it's a measurable spike in db-sync's per-epoch work.

- **Reward calculation** — actual ADA rewards for an epoch are calculated during the next-next epoch (Cardano's reward formula uses two-epoch-back stake data). When db-sync processes a reward-calculation epoch, it writes ~M rows to `reward`.

- **Protocol parameter updates** — proposed parameter changes activate at epoch boundaries. The `epoch_param` table gets a new row each epoch.

Catch-up sync rarely shows steady throughput because of these per-epoch costs. The per-epoch sync-duration chart usually has a noticeable per-epoch shape rather than a flat line.

## Eras and hard-fork combinators

A Cardano **era** is a phase of the chain delimited by hard forks. Each era introduces new ledger rules, transaction types, or capabilities. Chronologically:

| Proto major | Era | What it added |
|:---:|:---|:---|
| 1 | Byron | Original chain. UTXO transactions, no smart contracts. |
| 2 | Shelley | Delegation, staking, multi-asset (later). |
| 3 | Allegra | Token-locking via `invalid_before`/`invalid_hereafter`. |
| 4 | Mary | Native multi-asset tokens. |
| 5–6 | Alonzo | Smart contracts (Plutus). |
| 7–8 | Babbage | Reference inputs, inline datums, reference scripts (Vasil), then Valentine intra-era param update. |
| 9–10 | Conway | On-chain governance. |

A **hard-fork combinator** (HFC) is the mechanism that activates a new era. Activation happens at an epoch boundary — never mid-epoch. After the HFC fires, the ledger interprets transactions under the new rules; before it, under the old rules.

This is why our era classification (in `db-sync-report.py` and the node ingest plot) groups epochs by `protocol_major`: it's the chain's authoritative answer to "what era was this epoch in?"

### The proto_major trap we hit

We initially classified eras using `block.proto_major` from db-sync's schema. That's wrong, and the bug took a couple of diagnostic queries to find.

`block.proto_major` records the protocol version **declared in the block header** by the block producer. In Cardano, block producers can signal their support for the *next* protocol version by stamping their blocks with the new major number, **before** the HFC actually fires. So during the lead-up to a hard fork:

- The chain is still in era X.
- The ledger runs under era X rules.
- Most blocks have `proto_major = X`.
- A growing fraction of blocks have `proto_major = X+1` (signaling support).

If you classify by `MAX(block.proto_major)` per epoch, you label any epoch with even one signaling block as era X+1 — wildly overcounting. We saw "300 seconds of Conway sync time" attributed to a chain that was actually still in Babbage, just with some producers voting for Conway.

The correct source is **`epoch_param.protocol_major`** — the active protocol version recorded per epoch. That's what the chain is actually running under. Once the HFC fires at epoch N, `epoch_param.protocol_major` for epoch N has the new value. Before that, it has the old one. The bug fix replaced the SQL `MAX(block.proto_major)` with `MAX(epoch_param.protocol_major)`.

Per-block proto_major still has a use — it's how you'd plot "fraction of blocks signaling the upcoming era over time," which is the chain's vote count for the next HFC. We don't currently graph that, but the data is there.

## Chain time vs wall-clock time

**Chain time** is what the protocol counts in: slots. Each slot is conceptually one second of "chain time."

**Wall-clock time** is what your computer's clock measures.

These differ during catch-up sync:

- The chain produces ~one block per 20 wall-clock seconds **when fresh blocks are being made**.
- During catch-up sync, db-sync is processing blocks that were *already produced months or years ago*. It can ingest them as fast as Postgres can write — usually hundreds of slots per wall-clock second.

So while you sync, **chain time advances much faster than wall-clock time**. A sync that takes 8 wall-clock hours might cover 3 years of chain time. That's the catch-up regime.

Once db-sync reaches the chain tip, the relationship flips: chain time now advances only as new blocks land, at ~one slot per second on average (since slots are 1 second). Db-sync's slot position now grows at most at wall-clock speed.

This explains a few things you see in the project:

- **`tip_lag_sec`** is the wall-clock distance from the chain's actual tip. It starts huge (years of chain history to process) and shrinks during catch-up. Once at tip, it oscillates between 0 and ~20s (one block interval).

- **`sync_percent`** is `100 × (chain time covered) / (total chain time elapsed since genesis)`. Even at full tip with `tip_lag_sec ≈ 0`, sync_percent reads ~99.99% (not 100%) because the denominator includes the most recent few seconds where no new block has landed yet. This is why we don't show sync_percent as the headline "are we caught up" indicator — tip_lag is better.

- **`block_rate`** (blocks ingested per wall-clock second). During catch-up: hundreds. At tip: 0.05 (one block per ~20s). The transition from hundreds to ~0.05 is the most visible "we just hit tip" signal in any of the graphs.

## Why catch-up sync is faster than real time

A natural question: if the chain produces a block every 20 seconds, how can db-sync process them faster than that?

Because catch-up is processing **already-finalized historical blocks**. There's no consensus to wait for, no network propagation delay, no "is this the chain we agree on" question — the answer was decided years ago. db-sync just receives blocks one after another from cardano-node and writes their content to Postgres as fast as the disk and DB engine allow.

The rate-limiting step in catch-up sync is **Postgres write throughput** combined with **db-sync's ledger-state validation work in memory**. CPU-bound and disk-IO-bound, not network-bound.

At tip, the rate-limiting step becomes **chain block production** — db-sync is faster than the chain produces blocks, so it's idle most of the time waiting for the next one.

This is why our CPU% chart usually shows two distinct phases:

1. **Catch-up**: CPU% pegged near 100% (or higher with multi-threading), few-second idle gaps between block batches.
2. **At tip**: CPU% near 0% with occasional brief spikes when a new block arrives.

If you see CPU% drop precipitously and `tip_lag_sec` plateau near zero around the same wall-clock moment, you've hit tip. That's the inflection point worth marking.

## Era transitions during sync

A long-running sync passes through multiple eras. Each era boundary marks a change in:

- **Block format and content** — Mary adds multi-asset payloads, Alonzo adds Plutus redeemers, Babbage adds reference inputs and inline datums, Conway adds governance objects.
- **Ledger state size and shape** — Conway adds a drep set, a voting state, a constitution. The ledger grew step-by-step over eras.
- **Per-block work** — a Plutus-heavy era takes more CPU to validate than a Byron era's simple UTXO transactions.

So a per-epoch sync-duration plot doesn't look uniform across eras. Plutus-heavy epochs are slow; Conway epochs with governance activity are slow; quiet Byron epochs are fast. This is exactly the per-era summary we surface in the report — it lets you see "this version is 20% slower in Conway specifically" rather than just "this version is 5% slower overall."

## The UTXO set: special case

The UTXO (Unspent Transaction Outputs) set is the working state of the chain. Every transaction consumes some UTXOs and produces new ones. The total count grows over time but slowly (most transactions create roughly as many outputs as they consume).

db-sync stores transaction outputs in `tx_out`. To answer "what's the current UTXO set size," you need to count outputs that haven't been consumed yet. Two ways to compute this:

1. **Naive (slow)**: left-join `tx_out` against `tx_in` (the table recording consumptions) and count unmatched rows. On mainnet that's joining 150M against 100M+ rows. Minutes to compute.

2. **With `consumed_by_tx_id`** (db-sync 13.1+ feature, config-gated): db-sync optionally maintains a column on `tx_out` pointing to the tx that consumed it. Then `COUNT(*) FROM tx_out WHERE consumed_by_tx_id IS NULL` gives the UTXO set size in milliseconds (assuming an index).

Operators turn `consumed_by_tx_id` on or off via db-sync's config. The monitor probes for it at startup and only samples `utxo_count` if it's enabled — otherwise the column is all-NULL and the query would degenerate to "total outputs ever" which is meaningless.

This is the kind of domain detail that creeps into project design. The probe + opt-in handling is in `_common.py` (`detect_utxo_tracking`).

## Network identifiers: env and magic

The project uses `--env preprod` etc. to scope which Cardano environment a run targets. `preprod` is a public testnet that mirrors mainnet structurally but with cheap test ADA and faster era cadence. `mainnet` is the production chain. `preview` is another testnet with different parameters.

`cardano-cli` distinguishes them via:

- `--mainnet` for mainnet.
- `--testnet-magic N` for testnets, where N is the network magic:
  - mainnet: 764824073 (but `--mainnet` is the alias)
  - preprod: 1
  - preview: 2

The node monitor uses these magic numbers internally when invoking `cardano-cli query tip`. You don't need to think about them when running the script — `--env preprod` translates to the right flags.

## Recap

- Slots are 1 second; epochs are 432,000 slots (5 days); eras are stretches of epochs between hard forks.
- `epoch_param.protocol_major` is the authoritative active-era source, *not* `block.proto_major` (which is per-block signaling).
- Chain time and wall-clock time diverge during catch-up sync (chain time advances faster than wall-clock); they converge at tip.
- The shape of the per-epoch sync-duration line reflects the chain's content — heavy eras (Plutus, Conway governance) take longer than light eras (early Byron).
- `tip_lag_sec` is the most honest "are we caught up" indicator; `sync_percent` is informative but saturates near 100% well before truly at-tip.
- The UTXO query is only cheap if db-sync was configured to populate `consumed_by_tx_id`; the monitor detects this at startup.

Next: [03 — Graph catalog](03-graph-catalog.md).
