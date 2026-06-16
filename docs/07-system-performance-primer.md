# 07 - System performance primer

This is the one doc in the series that is **not about Cardano**. It is a from-scratch primer on
*performance engineering*: what your CPU, RAM, and disks actually do when you run a program, what
it means to be "I/O-bound" or "CPU-bound," what swap and the page cache are for, what `io_uring`
and `LUKS` are, why two SSDs behave differently from one, what PSI is and how to measure things
honestly, and which errors (OOM, segfault, EIO…) mean what.

Everything up to the last section is generic - it applies to any heavy program (a database, a
video encoder, a compiler, a game). **Only the final section** connects it back to `cardano-node`
and `cardano-db-sync`, contrasting the LSM (on-disk) and in-memory ledger backends.

Aimed at someone comfortable on the command line but new to measuring performance as a discipline.
Plain language, analogies where they help.

---

## 1. The cast, and the memory hierarchy

A computer doing work is really four things passing data around:

- **CPU** - the worker that actually computes. Think of a **chef**.
- **RAM (main memory)** - fast, small, *temporary* working space. The chef's **countertop**: whatever
  you're cooking *right now* lives here. Empty it when the power goes off.
- **Disk (SSD/HDD)** - slow, large, *permanent* storage. The **pantry and the warehouse out back**.
  Survives a power cut.
- **The bus / interconnect** - the **hallways** the food travels through between them.

The single most important fact in performance is that these run at *wildly* different speeds. The
numbers below are approximate, but the **ratios** are what matter. To make them graspable, imagine
one CPU cycle (~well under a nanosecond) were stretched to **1 second**:

| Operation | Real latency (order of) | If 1 CPU cycle = 1 second |
|:---|:---|:---|
| Read from CPU register / L1 cache | ~1 ns | ~1 second |
| Read from RAM | ~100 ns | ~2 minutes |
| Read from a fast NVMe SSD | ~10-100 µs | ~1 day to a week |
| Read from a spinning hard disk (HDD) | ~5-10 ms | ~6 months to a year |
| Round trip over the network | ~milliseconds to seconds | months to years |

The takeaway: **going to disk is enormously slower than staying in RAM**, and RAM is enormously
slower than the CPU's own caches. Almost all of performance engineering is a fight to *avoid the
slow tier* - keep the chef working off the countertop, not running to the warehouse for every
ingredient. When a program is fast, it's usually because its working data fits in a fast tier. When
it's slow, it's usually because it keeps falling down to a slower one.

---

## 2. What happens when you start a process

When you launch a program, the operating system (the "kitchen manager") does roughly this:

1. **Creates a process** - a fresh container with its own private view of memory. (On Linux this is
   classically `fork` + `exec`: clone the launcher, then replace it with the new program.)
2. **Gives it *virtual* memory, not real memory.** The program sees one big, clean, private address
   space - as if it owned the whole machine. This is an illusion the OS maintains. The program says
   "give me memory" and gets *addresses*, not necessarily physical RAM yet.
3. **Allocates lazily.** Asking for memory is a promise, not a delivery. Real RAM is handed over only
   when the program actually *touches* a page - a moment called a **page fault** (not an error; just
   "this page isn't backed by RAM yet, go fetch/zero it"). This is why a program can claim to "use"
   huge amounts of memory while really occupying little.
4. **Maps the program's code and libraries** from disk. Code is paged in on demand as execution
   reaches it - part of why a big program feels sluggish for the first few seconds (it's "warming
   up": faulting code and data in from disk into RAM).

**Pages.** Memory is managed in fixed-size chunks called **pages** (typically 4 KB). The OS tracks,
maps, and moves memory a page at a time. Keep this word - pages are the unit of almost everything:
allocation, faulting, caching, swapping.

Three numbers you'll see for a process, and what they really mean:

- **VIRT (virtual size)** - everything the process has *mapped*, including memory it never touched,
  shared libraries, memory-mapped files. Often huge and **mostly meaningless** as a "how much RAM is
  this using" figure.
- **RSS (Resident Set Size)** - the pages actually **resident in physical RAM** right now. This is
  the honest "how much RAM" number. (Caveat: it counts shared pages in every sharer, so summing RSS
  across processes over-counts.)
- **Shared** - pages shared with other processes (e.g. a common library loaded once, mapped many
  times).

> Rule of thumb: judge memory use by **RSS**, not VIRT. A process showing 100 GB VIRT and 800 MB RSS
> is using 800 MB.

---

## 3. RAM, the page cache, and swap

### What RAM holds

RAM holds more than your programs' working data. The OS uses **all otherwise-free RAM as a disk
cache** - the **page cache**. When you read a file, its blocks are kept in RAM afterwards; the next
read is served from RAM at memory speed instead of going back to the slow disk. Writes are buffered
in RAM and flushed to disk in the background.

This is why on Linux **"free memory" is almost always low and that's healthy.** Tools split memory
into:

- **used** - by running programs (their RSS),
- **buff/cache** - the page cache (and buffers): *reclaimable*. The OS will instantly hand it back to
  a program that needs it.
- **free** - genuinely unused. Often tiny.
- **available** - the number that actually matters: an estimate of how much a new program could get
  **without swapping**, i.e. free + most of the reclaimable cache.

> "Free RAM is wasted RAM." Empty RAM does no work. A healthy busy machine fills RAM with cache and
> shows little "free." Look at **available**, not **free**.

### Swap: the role and the myths

**Swap** is disk space the OS can use as an *overflow* for RAM. When memory gets tight, the kernel
takes pages that haven't been touched in a while ("cold" pages), writes them to the swap area, and
frees that RAM for active work. If a swapped-out page is needed again, it's read back ("swapped in").

Analogy: RAM is your **desk**; swap is a **filing cabinet** across the room. Moving rarely-used
papers to the cabinet frees desk space. That's good - *until* you have to keep running to the cabinet
for papers you need every minute. Then you spend all your time walking and none working. That
pathological state is called **thrashing**, and it can make a machine feel completely frozen even
though the CPU is "idle" (it's idle because it's *waiting on disk*).

Two myths to drop:

- **"Swap is used, therefore I'm out of memory."** No. The kernel proactively swaps out cold pages
  even when RAM is plentiful, to make room for cache. A few GB of *idle* swap with near-zero swap
  activity is normal and fine. What matters is the **rate** of swapping (in/out), not the static
  amount sitting there.
- **"Swap makes things slow."** Swap *capacity* doesn't. Swap *traffic under pressure* does. Watch
  the `si`/`so` (swap-in/swap-out) columns in `vmstat`: near zero = fine; sustained high = you're
  memory-bound and paying disk latency for memory accesses.

**swappiness** is a kernel knob (0-100, default ~60) for how eagerly it trades program pages for
cache. Lower = "prefer keeping program pages in RAM, shrink cache first." It tunes the *tendency*,
not a hard limit.

### The OOM killer

If memory demand exceeds RAM **and** swap and nothing can be reclaimed, the kernel can't conjure
memory from nothing. Rather than freeze forever, the **OOM (Out-Of-Memory) killer** picks a process
and kills it to recover RAM. It scores processes (roughly "who'll free the most, who's least
important" - the `oom_score`) and terminates the loser. You'll find the evidence in the kernel log
(`dmesg` / journal): *"Out of memory: Killed process …"*. From the program's point of view it just
*vanishes* - no clean shutdown, no error it could catch. (More in §12.)

### Linux vs Windows memory reporting

The same physical reality, different labels:

- **Linux** distinguishes *free* from *available* and shows the page cache as `buff/cache`. Newcomers
  panic at "free: 200 MB" - but `available` might be 40 GB.
- **Windows Task Manager** shows "In use," "Available," and "Cached." Its "Available" ≈ Linux's
  available. Windows' **page file** is the equivalent of Linux **swap** (Windows also uses RAM as a
  file cache, just labeled differently).
- Both OSes aggressively cache files in RAM and both overflow to disk-backed virtual memory. The
  *concepts* are identical; only the words on the dashboard differ.

---

## 4. CPU: cores, threads, and percentages over 100%

A modern CPU has multiple **cores** - independent workers. Many also do **simultaneous
multithreading** (Intel calls it Hyper-Threading), exposing each physical core as two **logical**
CPUs that share execution hardware.

### Why you see 200%, 800%, "1600%"

This trips up almost everyone. On Linux, the classic `top`/`ps` convention is that **100% means one
core fully busy.** So:

- **100%** = one core saturated (or the work spread so it sums to one core's worth).
- **200%** = the equivalent of **two cores** fully busy - e.g. two threads each pinning a core.
- On an 8-core / 16-thread machine the theoretical max is **1600%**.

So "200%" does **not** mean "impossible, over capacity." It means *two cores' worth of work*. It is
**not** "100% across all cores" and it is **not** "20% on each of 5 cores" - it's an additive total
where each fully-used core contributes 100.

Tools differ in how they present this - know which mode you're reading:

- **`top` (default) and `htop` in "Irix mode"**: per-process number is the **sum across cores** (can
  exceed 100%).
- **`htop` in "Solaris mode"** (toggle with `Shift`+`I`): divides by core count, so a process maxing
  two cores on a 16-thread box shows ~12.5% (200/16). Same reality, normalized to "% of the whole
  machine."

So the *same* process can read "200%" or "12.5%" depending only on the tool's mode. Always know which
you're looking at.

### Load average

The three numbers in `uptime`/`top` (e.g. `2.0 1.5 1.1`) are the **load average** over ~1/5/15
minutes: roughly the average number of threads either running or *waiting to run* (and, on Linux,
also those blocked in uninterruptible I/O wait). Read it against your core count:

- load ≈ cores → fully utilized, no queue.
- load < cores → spare capacity.
- load ≫ cores → a backlog; tasks are queueing for CPU (or stuck in I/O wait).

A load of 8 is healthy on a 16-core box and overloaded on a 4-core box. **Always interpret load
relative to core count.**

### Where the CPU's time goes

CPU time splits into categories (the `us/sy/id/wa/st` fields in `top`/`vmstat`):

- **user (us)** - running your program's own code.
- **system (sy)** - running kernel code on the program's behalf (syscalls: I/O, memory, networking).
- **idle (id)** - doing nothing.
- **iowait (wa)** - *idle specifically because it's waiting for disk/I/O to come back.* High iowait
  is the classic fingerprint of an **I/O-bound** workload: the CPU has nothing to do but wait on the
  disk. (Caveat: iowait is a slippery metric on multicore - PSI in §11 is better.)
- **steal (st)** - time a VM wanted the CPU but the hypervisor gave it to someone else (only relevant
  on virtual machines / cloud).

### Linux vs Windows CPU reporting

- **Linux** sums per-core (so totals run past 100% as above), and exposes the user/sys/iowait/steal
  breakdown.
- **Windows Task Manager** normalizes CPU to **0-100% of the whole machine** by default (so two of
  eight cores busy shows ~25%, not 200%). Its Performance tab can show per-core graphs. There's no
  separate "iowait" headline; Windows surfaces I/O waiting differently (Resource Monitor, disk queue
  length).

Neither is "right" - they're different denominators. Confusion almost always comes from comparing a
**summed** number to a **normalized** one.

---

## 5. How a disk is actually used

### The path a read takes

When your program reads a file, it doesn't talk to the disk directly. It travels a stack:

```
your program
   → read() syscall (into the kernel)
      → page cache  (is this block already in RAM? if yes, return it - done, fast)
         → filesystem  (translate "file X, offset Y" → block numbers)
            → block layer  (queue/schedule/merge I/O requests)
               → device driver
                  → the physical device (SSD/HDD)
```

Two consequences:

1. **The page cache (§3) can short-circuit the whole trip.** A "read" that hits cache never touches
   the disk. This is why a second run of the same workload is often dramatically faster - the data is
   "warm" in RAM.
2. Every layer adds a little latency and, for encrypted/abstracted setups, real work (see §8).

### The vocabulary that actually matters

- **Block / sector** - the smallest chunk the disk deals in (commonly 4 KB). You never read "1 byte
  from disk"; you read at least a block.
- **Sequential vs random access.** *Sequential* = reading blocks that sit next to each other (e.g.
  streaming one big file). *Random* = jumping all over the device for small scattered reads (e.g.
  database point-lookups). This distinction is the single biggest driver of disk performance (§6).
- **IOPS vs throughput vs latency** - three different questions:
  - **IOPS** = *operations* per second (how many separate reads/writes). Random small-I/O workloads
    live or die on IOPS.
  - **Throughput / bandwidth** = *bytes* per second (MB/s, GB/s). Big sequential transfers live on
    throughput.
  - **Latency** = how long *one* operation takes. What a single waiting request feels.
  A device can have great throughput and poor latency, or vice-versa. Know which your workload needs.
- **Queue depth** - how many I/O requests are *in flight* at once. Modern SSDs are fast precisely
  because they service **many** requests in parallel - but only if the software keeps the queue full.
  A program that issues one request, waits, then issues the next, leaves most of an SSD's speed on the
  table. (This is exactly the gap `io_uring` in §7 was built to close.)

### Writes and durability

Writes are usually **buffered**: `write()` returns once the data is in the page cache, *not* once it's
safely on the platter. The kernel flushes "dirty" pages to disk later. That's fast but risky on power
loss, so programs that need durability call **`fsync`** to force the data to stable storage and wait
for confirmation. `fsync` is *expensive* - it's a real round-trip to the device - which is why
databases batch writes and think hard about how often they fsync. Many devices also have their own
**write-back cache**, adding another layer between "the OS thinks it's written" and "the electrons are
actually stored."

---

## 6. SSD vs mechanical hard disk (HDD)

### Mechanical hard disk (HDD)

An HDD is a **record player**: spinning magnetic platters and a head on an arm that physically moves
to the right track, then waits for the right spot to rotate under it. That physical movement is
**seek time** + **rotational latency** - milliseconds per random access.

- **Sequential** reads/writes are decent: the head stays put and data streams under it.
- **Random** access is brutal: every jump means physically moving the arm and waiting for rotation.
  An HDD might do a few **hundred** random IOPS - and that's a hard ceiling set by physics.

### Solid-state disk (SSD)

An SSD has **no moving parts** - it's flash (NAND) memory with a controller. There's no head to move,
so the penalty for "random" access is small. And the controller can read/write **many** flash chips in
**parallel**.

- Random access is *enormously* better than HDD - tens of thousands to millions of IOPS.
- It rewards **parallelism**: feed it a deep queue (§5) and it shines; feed it one-at-a-time and you
  waste it.
- **NVMe vs SATA**: both are SSDs, but the *interface* differs. SATA SSDs ride an older bus designed
  for spinning disks (one shallow command queue). **NVMe** SSDs sit on PCIe with many deep queues -
  much higher throughput and IOPS. A SATA SSD is far faster than an HDD; an NVMe SSD is far faster
  again.
- **Wear & TRIM**: flash cells wear out after many writes; controllers spread writes around (wear
  leveling), and the OS tells the drive which blocks are free (**TRIM**) so it can stay fast. Usually
  invisible to you, but it's why SSDs have finite write endurance.

### Why this matters for "syncing" / bulk catch-up work

Any "download and process a large history" job has two very different phases:

- **Bulk sequential ingestion** (streaming a lot of data in order): bottlenecked on *throughput*. An
  HDD can sometimes keep up here.
- **Random point-lookups into a large dataset** (e.g. "does key X exist? what's its current value?"
  scattered across a huge structure): bottlenecked on *random IOPS and latency*. This is where HDDs
  fall off a cliff and **an SSD (ideally NVMe) is effectively mandatory.**

So a workload that is mostly random reads against a big on-disk dataset *needs* an SSD, while a purely
sequential bulk job is more forgiving. (This is the seed of the LSM-vs-in-memory discussion in §13.)

---

## 7. io_uring and the evolution of asynchronous I/O

To understand `io_uring` you need the problem it solves.

**The naïve way - blocking I/O.** Your thread calls `read()`, and *stops* until the data arrives. If
disk latency is, say, 100 µs, that thread does nothing for 100 µs. For a single-stream job that's
fine. For something needing thousands of I/Os, it's a disaster: the device could be doing many in
parallel, but your one waiting thread issues them one at a time (queue depth 1, see §5).

**Workaround 1 - many threads.** Spin up hundreds of threads so that while some block, others issue
I/O. This works but is costly: each thread uses memory and the CPU burns time **context-switching**
between them (saving/restoring state - overhead that buys no real work).

**Workaround 2 - `epoll`, `libaio`.** Older async mechanisms. `epoll` is great for *network sockets*
but doesn't really do async *file* I/O. `libaio` does async disk I/O but is limited and full of sharp
edges (often only truly async with `O_DIRECT`, etc.).

**`io_uring`** (a modern Linux kernel interface) fixes this with **two shared ring buffers** between
your program and the kernel:

- a **Submission Queue (SQ)** - you drop I/O requests in,
- a **Completion Queue (CQ)** - the kernel drops finished results out.

Analogy: a **deli counter with a ticket system**. Instead of asking for one item and standing there
until it's bagged (blocking), you drop a *stack* of order tickets in the in-tray (SQ) and periodically
check the out-tray (CQ) for finished orders - picking them up in whatever order they're ready. You can
have **dozens or hundreds of orders in flight at once**, and you barely talk to the clerk (few
syscalls), so the CPU overhead per I/O is tiny.

Why programs adopt it:

- **High IOPS with low CPU cost** - exactly what a random-read-heavy database wants from an SSD.
- **Deep queues kept full** - it naturally exploits SSD parallelism (§6).
- **Fewer syscalls / context switches** than thread-per-I/O or one-syscall-per-operation.

Where it shows up when things go wrong: because submissions and completions pass through this ring,
errors surface at **submission/completion time** - you'll see them attributed to a "submit I/O"
operation rather than to a plain `read()`. (Keep this in mind for §12's I/O errors.)

---

## 8. The storage stack and encryption (LUKS / dm-crypt, LVM)

A physical disk rarely sits *directly* under your filesystem. On a typical encrypted Linux laptop the
real stack, top to bottom, is a layered cake:

```
your files
  → filesystem        (ext4 / xfs / btrfs: "file + offset" → block numbers)
    → LVM              (logical volumes: flexible partitioning on top of physical storage)
      → dm-crypt/LUKS  (full-disk encryption: encrypt on the way down, decrypt on the way up)
        → partition    (e.g. nvme0n1p3)
          → the physical device (the SSD/HDD itself)
```

Every read climbs *up* this cake and every write travels *down* it.

### The thing that surprises people: unlocking ≠ "decryption is over"

When you type your disk password at boot (or unlock the machine), you are **not** decrypting all your
data once and leaving it in the clear. You are unlocking the **key**. From then on the data **stays
encrypted on the platter forever**, and the `dm-crypt` layer **decrypts every block on the way in and
encrypts every block on the way out**, transparently, for as long as the machine is running.

Analogy: imagine every book in a library is written in cipher. Unlocking the building in the morning
doesn't translate the books - it just lets the **translator** come to work. After that, *every* time
you read a page the translator decodes it for you, and *every* time you write a note they re-encode it
before it's shelved. The translator never leaves while the library is open. Unlocking opened the door;
it didn't remove the translator.

### What that costs

- **CPU per I/O.** Every block read or written runs through a cipher. Modern CPUs have hardware
  acceleration (AES-NI) so it's cheap *per block*, but it's not free, and it scales with how many
  blocks you move. A random-read-heavy job pushes a *lot* of blocks through the translator.
- **Extra memory copies / buffer handling.** The encryption layer needs buffers to transform data
  between ciphertext and plaintext, adding work and indirection on the hot path.
- **Latency.** One more layer between the request and the device - small, but it's there on *every*
  operation.
- **Interaction with deep async I/O under saturation.** When a high-queue-depth async engine (§7) is
  hammering an encrypted device flat-out, the combined system (async submission → crypt transform →
  device) has more moving parts and more ways to back up or surface an error than a plain read off a
  bare disk. Under heavy saturation this stack is simply *busier and more complex* than an
  unencrypted one.

None of this means "encryption is bad" - full-disk encryption is the right default for most people.
It means: **encryption is a real, permanent layer in the I/O path that consumes CPU and adds latency
on every block, and it's part of the picture whenever you reason about a disk-bound workload.**

### Why LVM is there (and why its overhead is small)

**LVM (Logical Volume Manager)** sits between the encryption layer and the filesystem to give you
*flexible* storage: you can resize volumes, span multiple physical disks, snapshot, etc., without
repartitioning. Unlike encryption it does almost no per-block computation - it's mostly an address
remapping - so its performance overhead is negligible. It's in the stack for flexibility, not speed.

---

## 9. Why separate disks matter

Two heavy workloads on **one** device versus **two** devices behave very differently.

### Contention: one device = one queue

A storage device has a finite amount of work it can do per second (its IOPS/throughput ceiling) and
fundamentally **one** request stream into the hardware. If two demanding jobs share it, they **take
turns** - their requests interleave in the same queue and each waits behind the other's.

Analogy: a supermarket with **one checkout lane**. Two full carts don't get served simultaneously; they
queue, and *both* shoppers wait longer. Add a **second lane** (a second disk) and each cart gets its
own - both finish sooner and neither blocks the other.

### Isolation and fault containment

Separation buys more than throughput:

- **No noisy neighbor.** If job A suddenly saturates its disk, job B on a *different* disk is
  unaffected. On a *shared* disk, A's I/O storm starves B - even though B's own demand didn't change.
  This is the "noisy neighbor" problem.
- **Failure containment.** A disk that develops errors or fills up takes down only the work that lives
  on it. Spreading independent workloads across devices limits the blast radius.
- **Read/write separation.** Putting a read-heavy workload on one disk and a write-heavy one on another
  stops them fighting over the same queue, and lets each device's behavior (and your monitoring of it)
  stay legible.

> Practical heuristic: when two processes are both demanding on storage, **give them separate physical
> devices** if you can. You gain throughput *and* resilience *and* clearer diagnostics. The benefit is
> real precisely because of everything in §5-§8: the device, its queue, and (if encrypted) its crypt
> layer are all shared resources.

A subtlety worth stating plainly: "they're both SSDs" does **not** make them interchangeable for this.
Two separate SSDs give you two independent queues; one SSD holding both datasets gives you one queue no
matter how fast it is. Capacity and speed are about the *device*; contention is about *how many
independent devices* the work is spread across.

---

## 10. Bottlenecks: CPU-bound vs I/O-bound vs memory-bound

A workload is a pipeline of stages (CPU compute, memory access, disk I/O, sometimes network). The
**slowest stage sets the overall pace** - like an assembly line where one slow station throttles the
whole belt no matter how fast the others run. That slowest stage is the **bottleneck**, and naming it
correctly is the whole game: optimizing any *other* stage changes nothing.

The three common bottlenecks:

- **CPU-bound** - the cores are pinned near 100% doing actual computation; the disk is mostly idle.
  Adding faster/more cores, or doing less work, helps. Faster disks do nothing.
  *Fingerprint:* high `user`+`system` CPU, low iowait, low/zero swap traffic, CPU PSI elevated.

- **I/O-bound** - the CPU spends much of its time **waiting** for disk; cores look "idle" but the work
  isn't progressing because it's blocked on storage. A faster disk (or fewer/larger I/Os, more cache,
  or splitting across disks per §9) helps. A faster CPU does nothing.
  *Fingerprint:* high **iowait**, high **I/O PSI**, modest user-CPU, disk near its IOPS/throughput
  ceiling.

- **Memory-bound** - demand for RAM exceeds what's available, so the system **swaps** (or thrashes the
  CPU caches). Time goes into moving pages, not doing work. More RAM (or a smaller working set) helps.
  *Fingerprint:* sustained **swap-in/out** traffic (`si`/`so`), high **memory PSI**, possibly the OOM
  killer firing.

The diagnostic mindset: **don't guess - measure which stage is the bottleneck, then optimize *that*
one.** Speeding up a non-bottleneck stage is wasted effort (and a classic beginner mistake: buying a
faster CPU for an I/O-bound job, or a faster disk for a CPU-bound one).

---

## 11. PSI, and how to measure honestly

### Utilization lies; pressure doesn't

The instinctive metric is **utilization** - "the disk is 100% busy," "the CPU is at 100%." But
utilization conflates *healthy saturation* with *harmful overload*. A disk at 100% utilization might be
perfectly happy (it has exactly enough work and no one's waiting), or it might have a huge backlog of
requests stalled behind it. Utilization can't tell those apart.

**PSI (Pressure Stall Information)** can. PSI, exposed by the Linux kernel at
`/proc/pressure/{cpu,memory,io}`, measures **how much time tasks spent *stalled* waiting for a
resource they couldn't get** - i.e. *unmet demand*, not mere busyness. Each file looks like:

```
some avg10=24.17 avg60=24.03 avg300=28.51 total=57921435764
full avg10=20.84 avg60=21.39 avg300=26.44 total=55508039607
```

Reading it:

- **`some`** - the fraction of time **at least one** task was stalled waiting for this resource.
- **`full`** - the fraction of time **every** runnable task was stalled (nothing could make progress).
  `full` is the more serious signal: time the system was effectively *blocked* on that resource.
- **`avg10 / avg60 / avg300`** - percentages averaged over the last 10 / 60 / 300 seconds. Watch the
  *trend* across the three (rising = pressure building).
- **`total`** - a cumulative microsecond counter since boot; take differences between two readings to
  compute a rate over an interval.

Why it's the better headline number: a CPU/disk can be at 100% utilization with **zero** pressure
(saturated but no queue) - fine. The moment demand exceeds capacity, **PSI rises** because tasks start
*waiting*. So `io full avg10 = 20%` means "for ~20% of the last 10 seconds, *everything* was stuck
waiting on storage" - a far more actionable statement than "disk is busy."

### The measurement toolbox

Match the tool to the question:

| Question | Tool(s) |
|:---|:---|
| Overall RAM / cache / swap *amounts* | `free -h` |
| Live rates: swap in/out, blocks in/out, CPU split, run/blocked queue | `vmstat 1` |
| Per-process CPU/RAM, interactive overview | `top`, `htop` |
| Per-**device** disk utilization, IOPS, throughput, await (latency) | `iostat -xz 1` |
| Which **process** is doing the disk I/O | `iotop`, `pidstat -d 1` |
| Per-process CPU / context-switch / memory rates over time | `pidstat 1`, `pidstat -w 1` |
| **Stall / pressure** (the honest saturation signal) | `cat /proc/pressure/{cpu,memory,io}` |
| Drive health, reallocated sectors, error counters | `smartctl -a /dev/…` |
| Recorded multi-resource history / one-screen dashboard | `sar` (sysstat), `dstat` |
| Deep CPU profiling (where cycles actually go) | `perf` |
| Kernel events: OOM kills, I/O errors, resets | `dmesg`, the journal |

### How to measure *properly* (and how people get it wrong)

- **Sample over time; never trust one snapshot.** A single instant catches noise. Watch `vmstat 1`,
  `iostat 1`, or PSI trends for a *period* under representative load. (This is, not coincidentally, the
  entire philosophy of this project's monitors - see [01 - Time-series fundamentals](01-time-series-fundamentals.md).)
- **Watch rates, not totals.** "100 GB written" is meaningless without "over how long." Differentiate
  counters into per-second rates (the `total=` PSI field, bytes written, etc.).
- **Correlate signals before concluding.** "Slow" + high iowait + high I/O PSI + disk at its IOPS
  ceiling ⇒ I/O-bound. "Slow" + pinned cores + ~zero iowait ⇒ CPU-bound. "Slow" + heavy `si`/`so` +
  high memory PSI ⇒ memory-bound. One metric alone can mislead; the *pattern* is the diagnosis.
- **Common mistakes:** reading VIRT as memory usage (use RSS, §2); panicking at low "free" (read
  *available*, §3); equating "swap used" with "out of memory" (watch the *rate*, §3); equating "disk
  100% busy" with "disk overloaded" (check **PSI** and **await/latency**); comparing a *summed* CPU%
  to a *normalized* one (§4).

---

## 12. Common failure modes

When a heavy job goes wrong, the failure usually has a recognizable name. The big distinction to
internalize first: **OOM-kill ≠ segfault.** Both make a program disappear, but for opposite reasons -
one is "the system ran out of memory and killed you," the other is "your program touched memory it
shouldn't have."

| Failure | What it is | Typical cause | Where you see it |
|:---|:---|:---|:---|
| **OOM kill** | The kernel's OOM killer terminates a process to reclaim RAM when memory + swap are exhausted. The process gets *no chance to handle it* - it's killed outright. | Working set exceeds RAM+swap; a memory leak; too many memory-hungry processes at once. | `dmesg`/journal: *"Out of memory: Killed process …"*; the process just vanishes. |
| **Segfault (SIGSEGV)** | The program accessed a memory address it isn't allowed to (a *programming* fault, not a resource shortage). | Null/dangling pointer, buffer overrun, bug in the program or a library. **Unrelated to how much RAM is free.** | `Segmentation fault (core dumped)`; a crash report / core dump. |
| **Thrashing / swap-death** | The machine spends ~all its time swapping pages in and out instead of working; it appears frozen though "CPU is idle." | Memory pressure just under the OOM threshold - constant paging. | Sustained high `si`/`so` in `vmstat`; high memory PSI; UI unresponsive. |
| **`EIO` (I/O error)** | A read/write failed at the **hardware/device** level. | Failing disk, bad sectors, cable/controller fault, device reset. | Kernel log I/O errors; `smartctl` shows reallocated/pending sectors. |
| **`EFAULT` (bad address)** | An I/O or syscall referenced a memory buffer/address the kernel deemed invalid. Often a *software/interaction* issue rather than a dying disk. | Bad/aligned-wrong buffer in an async-I/O path; bugs or edge cases in a complex I/O stack under load. | Surfaces from the I/O submission path (e.g. an async "submit I/O" error); kernel/program logs. |
| **`ENOSPC` (no space left)** | A write failed because the filesystem is **full** (or out of inodes). | Disk filled up; runaway logs; an unbounded dataset. | `df -h` shows 100%; write errors in logs. |
| **`EMFILE` / "too many open files"** | The process hit its **file-descriptor limit**. | A descriptor leak, or a legitimately high-concurrency program with a too-low `ulimit -n`. | Errors opening files/sockets; check `ulimit -n` and `/proc/<pid>/limits`. |

A useful way to read an `errno`: **EIO points at the *hardware*; ENOSPC points at *capacity*; EMFILE
points at a *limit/leak*; EFAULT points at a *buffer/address* problem in the software path** (more
likely under a complex, saturated I/O stack than from a healthy disk). When you see one, the name
already narrows where to look.

---

## 13. Cardano-specific: the LSM (on-disk) vs in-memory ledger backend

Everything above is generic. Here's where it lands for `cardano-node` and `cardano-db-sync`.

### The problem: the ledger state is large

A `cardano-node` must keep the **ledger state** - most importantly the **UTxO set** (every unspent
output, the chain's "account balances") - and consult it constantly to validate blocks. This state is
large and grows over time. **UTxO-HD** ("UTxO on a Hard Disk") is the feature that lets the node choose
*where* that state lives, via the `LedgerDB.Backend` config setting. Two backends matter here:

**`V2InMemory` - ledger state in RAM.**
- The whole UTxO set is held in memory.
- Lookups are **memory-speed** - no disk trip to check a UTxO. Validation is fast.
- The cost is **RAM**: the working set must fit, so this backend is **memory-bound** and its RAM
  footprint scales with the ledger.
- Its disk I/O is comparatively modest - mainly reading/writing the **chain database** (the blocks
  themselves) and periodically writing ledger **snapshots**. It does **not** do a disk read per UTxO
  lookup.

**`V2LSM` - ledger state in an on-disk LSM-tree.**
- The UTxO set lives **on disk**, in an **LSM-tree** (see below), not in RAM.
- This **slashes RAM usage** - the headline benefit - because the state no longer has to fit in
  memory.
- The trade-off is that **validation lookups become disk reads**: checking "does this UTxO exist /
  what is it?" now hits storage. So this backend is **I/O-bound** and, critically, **sensitive to the
  entire disk stack from §5-§8** - device speed, queue depth, *and* the encryption layer.
- To make those lookups bearable it issues **many small random reads with high parallelism via
  `io_uring`** (§7) - exactly the access pattern that needs a fast **SSD/NVMe** (§6) and benefits from
  deep queues.

### What an LSM-tree is (in one breath)

An **LSM-tree (Log-Structured Merge-tree)** is a write-optimized on-disk structure. New data is
buffered and written out as sorted, immutable files; in the background those files are merged and
re-sorted into larger levels (**compaction**). A **point lookup** ("find key X") may have to check
**several levels** to find the current value - i.e. **multiple small, scattered reads per lookup**.
That's precisely the *random-read-heavy* pattern from §6 and §10, and precisely why `io_uring`'s
ability to fire many reads in parallel (§7) is the right engine for it.

### Putting it together (the §1-§12 lens)

- **In-memory** makes a node **RAM-bound**: watch *memory* and *swap* (§3, §10). Its risk is running
  out of RAM; its disk demand is moderate.
- **LSM** makes a node **I/O-bound**: watch *I/O PSI*, *iowait*, *device IOPS/latency* (§10, §11). Its
  lookups are random reads through `io_uring` (§7) → through any **LUKS/dm-crypt** layer (§8) → to the
  SSD. Under heavy saturation that whole async-encrypted-storage path is the busy, complex part - and
  the place I/O errors (§12) would surface (an `EFAULT`-class failure from the I/O submission path is
  far more at home here than on a quiet, unencrypted disk).

### The db-sync angle

`cardano-db-sync` reads blocks from the node (over a local socket) and **writes** them, heavily, into
**PostgreSQL**. That's a **write-heavy** workload on the postgres data directory. Meanwhile an
LSM-backed node is doing **random reads** on its own ledger database. Per §9, these are two demanding,
*different-shaped* storage workloads - so keeping the **node's ledger DB and postgres's data directory
on separate physical disks** lets each have its own queue, avoids the read-vs-write noisy-neighbor
fight, and keeps your monitoring legible (node-disk pressure vs postgres-disk pressure are then
distinct signals).

> Bottom line: **`V2InMemory` trades RAM for speed (memory-bound); `V2LSM` trades speed for RAM
> (I/O-bound, and sensitive to the SSD + encryption stack).** Which one is "better" depends entirely
> on whether your machine is short on RAM or short on fast I/O - and *measuring* which (§10-§11) is the
> only honest way to decide.

---

## Recap / cheat-sheet

- **Memory hierarchy:** CPU ≫ RAM ≫ SSD ≫ HDD ≫ network, by orders of magnitude. Performance is the
  art of staying in the fast tiers.
- **RSS, not VIRT,** is real memory use. **Available, not free,** is your real memory headroom.
- **Free RAM is wasted RAM** - the OS fills it with reclaimable page cache.
- **Swap used ≠ problem;** swap *traffic* (`si`/`so`) under pressure = problem (thrashing).
- **CPU 200% = two cores' worth** (Linux sums per core; Windows normalizes to 100% total). Read
  **load average against core count.**
- **Sequential vs random** is the master variable for disks; **SSDs (esp. NVMe) crush HDDs at random
  I/O**, and reward **deep queues** - which is what **`io_uring`** delivers.
- **Unlocking an encrypted disk reveals the key, not the data:** `dm-crypt` decrypts every read and
  encrypts every write *forever*, costing CPU and latency on every block.
- **Separate disks = separate queues:** more throughput, fault isolation, no noisy neighbor - even
  when both are SSDs.
- **Name the bottleneck (CPU- / I/O- / memory-bound) by *measuring*, then fix *that* stage.**
- **PSI (`/proc/pressure/*`) beats utilization** - it measures *unmet demand* (stall), not mere
  busyness; watch `full`, watch the trend.
- **Errors:** OOM-kill (out of RAM, killed) ≠ segfault (bad pointer); `EIO` = hardware, `ENOSPC` =
  full, `EMFILE` = fd limit, `EFAULT` = bad buffer/address in the I/O path.

**Which tool answers which question:**

| You want to know… | Reach for… |
|:---|:---|
| Am I out of memory / how much headroom? | `free -h` (read *available*), memory PSI |
| Am I swapping right now? | `vmstat 1` (`si`/`so`), memory PSI |
| Which process eats CPU/RAM? | `htop` / `top`, `pidstat 1` |
| Is a disk the bottleneck, and how hard? | `iostat -xz 1` (util, await), **I/O PSI** |
| *Which* process is hitting the disk? | `iotop`, `pidstat -d 1` |
| Is the *machine* under genuine pressure? | `cat /proc/pressure/{cpu,memory,io}` |
| Is the drive itself healthy? | `smartctl -a /dev/…` |
| Did the kernel kill something / log I/O errors? | `dmesg`, the journal |

For *how this project turns these ideas into sampled time-series and graphs*, see
[01 - Time-series fundamentals](01-time-series-fundamentals.md) and the
[03 - Graph catalog](03-graph-catalog.md). For term lookups, see the [06 - Glossary](06-glossary.md).
