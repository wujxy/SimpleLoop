---
name: ihep-htcondor
description: >-
  Use when the user asks to query, search, or submit jobs on the IHEP HTCondor
  cluster — finding idle/zen4/zen5 machines, checking job status, decoding why a
  job is idle, picking a machine_constraint, or understanding the pool/schedd
  layout. Encodes the IHEP-specific pool topology (cm01 vs inkcm), schedd names,
  CPU-family decoding, and the exact condor_status/condor_q incantations that
  work from login nodes, so the agent doesn't have to hunt for docs.
---

# IHEP HTCondor cluster — query and submit guide

Print `[skill: ihep-htcondor]` before proceeding.

## Pool topology (the #1 gotcha)

IHEP has **two separate condor pools**. The login node's default collector is
`inkcm.ihep.ac.cn`, a tiny 4-machine pool. **JUNO production jobs run on the
`cm01.ihep.ac.cn` pool**, which has ~879 machines. Querying the wrong pool is
the most common reason an agent "can't find a machine" or misdiagnoses an idle
job.

| Pool | Collector | Machines | Used for |
|------|-----------|----------|----------|
| **JUNO production** | `cm01.ihep.ac.cn` | ~879 | real JUNO jobs, schedd06/07/10/11/12 |
| login-node default | `inkcm.ihep.ac.cn` | 4 | not where JUNO jobs run |

**Rule: always pass `-pool cm01.ihep.ac.cn` for JUNO work.** A bare
`condor_status` / `condor_q` hits `inkcm` and will silently show the wrong
4-machine pool.

## Schedds

JUNO schedds live in the cm01 pool. Names are `scheduler@scheddNN.ihep.ac.cn`
(NN = 06, 07, 10, 11, 12). To query a job on a specific schedd:

```bash
condor_q -pool cm01.ihep.ac.cn -name scheduler@schedd12.ihep.ac.cn <clusterid.0> -af JobStatus Machine
```

List all schedds and their job counts:

```bash
condor_status -pool cm01.ihep.ac.cn -schedd
```

## CPU family / model decoding

Startd ads expose `CpuFamily` and `CpuModelNumber` (not a vendor string).
Decode Zen generation from these:

| CpuFamily | CpuModelNumber | Microarch | Common name |
|-----------|----------------|-----------|-------------|
| 25 | 17 | Zen4 | EPYC Genoa |
| 26 | 2 | Zen5 | EPYC Turin |
| 6 | 106 | Intel Icelake | — |
| 6 | 85 | Intel Skylake/Cascadelake | — |
| 6 | 79 | Intel Skylake-X | — |

`cpu_model` in SimpleLoop `task_hints.yaml` maps to a Requirements clause via
`_CPU_MODEL_REQUIREMENTS` in `simpleloop/config.py` (e.g. `zen4` →
`CpuFamily==25 && CpuModelNumber==17`). **The `cpu_model` and
`machine_constraint` are AND-joined** in `simpleloop/execution/hepjob.py`
`_requirements_expr`. If they contradict (e.g. `cpu_model: icelake` pinned to
an AMD machine), the job can never match and will idle forever.

## Ready-to-use queries

All of these take `POOL=cm01.ihep.ac.cn`. Set it once:

```bash
POOL=cm01.ihep.ac.cn
```

### List all machines with free CPUs, sorted by most idle

```bash
condor_status -pool $POOL -af Machine State Cpus CpuFamily CpuModelNumber \
  -const 'State == "Unclaimed"' \
| awk '{m[$1]+=$3; fam[$1]=$4; mod[$1]=$5} END{for(k in m) printf "%s freeCpus=%d family=%s model=%s\n", k, m[k], fam[k], mod[k]}' \
| sort -t= -k2 -rn | head -20
```

### Find idle Zen4 machines (free CPU sum per machine)

```bash
condor_status -pool $POOL -af Machine State Cpus \
  -const 'CpuFamily==25 && CpuModelNumber==17 && State == "Unclaimed"' \
| awk '{m[$1]+=$3} END{for(k in m) print k, m[k]}' | sort -t' ' -k2 -rn
```

### Find idle Zen5 machines

```bash
condor_status -pool $POOL -af Machine State Cpus \
  -const 'CpuFamily==26 && CpuModelNumber==2 && State == "Unclaimed"' \
| awk '{m[$1]+=$3} END{for(k in m) print k, m[k]}' | sort -t' ' -k2 -rn
```

### Does a specific machine exist in the production pool?

```bash
condor_status -pool cm01.ihep.ac.cn -af Machine State Cpus CpuFamily CpuModelNumber \
  -const 'Machine == "lhws316.ihep.ac.cn"'
```

No output = machine is **not in this pool** (the classic idle-job cause when
`machine_constraint` pins a host from the wrong pool).

### Inspect a job's full Requirements + why it's idle

```bash
SCHEDD="scheduler@schedd12.ihep.ac.cn"
condor_q -pool $POOL -name "$SCHEDD" <clusterid.0> -l \
  | grep -iE '^(Requirements|JobStatus|Machine|AccountingGroup|Owner|IHEP_RealGroup|RequestCpus|RequestMemory|QDate|EnteredCurrentStatus|ServerTime|FileSystemDomain)'
```

JobStatus codes: 1=Idle, 2=Running, 5=Held.

### Diagnose an idle job — checklist

1. Confirm the job's `Machine` constraint target **exists in cm01** (query
   above). If the machine only shows up under bare `condor_status` (inkcm),
   it's in the wrong pool → the constraint can never match.
2. Check `cpu_model` vs the pinned machine's `CpuFamily`/`CpuModelNumber` are
   consistent (both Zen4, or both Zen5). Contradiction → never matches.
3. Check `FileSystemDomain` matches (normally `ihep.ac.cn` on both sides —
   rarely the problem at IHEP, but cheap to rule out).
4. Check the schedd actually has the job: `condor_q -pool cm01 -name <schedd>
   <id> -af JobStatus`.

## Picking a machine_constraint for task_hints.yaml

1. Decide the CPU generation. If the user wants Zen4, query idle Zen4
   machines; if Zen5, idle Zen5. If "any with enough CPUs", use the
   all-machines sort.
2. Pick the machine with the most free CPUs that the user is happy with.
3. Set **both** lines consistently in `task_hints.yaml`:

```yaml
    cpu_model: zen4                       # or zen5
    machine_constraint: 'Machine == "lhws316.ihep.ac.cn"'
```

4. The resulting condor Requirements will be
   `(CpuFamily==.. && CpuModelNumber==..) && (Machine == "..")`.
   Verify the chosen machine satisfies that family/model in cm01 before
   submitting.

## Submitting jobs

Submission is handled by `simpleloop/execution/hepjob.py`, which builds the
submit file and calls `condor_submit`. The agent normally does not submit
by hand — it configures `execution.hepjob` in `task_hints.yaml` and the
harness submits. Key fields:

```yaml
execution:
  backend: hepjob
  hepjob:
    schedd_name: scheduler@schedd12.ihep.ac.cn
    collector: cm01.ihep.ac.cn
    accounting_group: JUNO.juno.default
    cpu_model: zen4
    machine_constraint: 'Machine == "lhws316.ihep.ac.cn"'
    memory_mb: 4096
```

`collector` must be `cm01.ihep.ac.cn` (not the login-node default) or the
schedd won't be reachable.

## Memory notes (persistent facts)

See [[ihep-htcondor-submission]] for: mandatory `accounting_group` +
`IHEP_RealGroup`, and the `condor_q -af` gotcha (always use `-pool` + `-name`
from login nodes).
