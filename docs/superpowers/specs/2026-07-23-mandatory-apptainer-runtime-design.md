# Mandatory Apptainer Runtime Design

**Date:** 2026-07-23  
**Status:** Approved design  
**Scope:** SimpleLoop agent execution, evaluation, runtime validation, and SIF build shortcut

## 1. Problem

SimpleLoop currently launches Claude and evaluation commands with the complete
host environment. Activating a Python virtual environment, sourcing multiple
JUNO releases, or retaining old `PYTHONPATH` and `LD_LIBRARY_PATH` entries can
therefore change what an executor sees. An executor may then spend substantial
time diagnosing a shell-specific ROOT, Python, compiler, or dynamic-library
failure that the harness does not reproduce.

The existing per-run Git clone and candidate worktrees isolate source changes,
but they do not isolate the software runtime. The product needs one reproducible
execution environment shared by:

- the proposer;
- the executor and every Bash command it launches;
- the judger; and
- the harness-owned evaluation.

The MVP should establish that boundary with minimal configuration and without
adding a general container abstraction.

## 2. Decision Summary

Apptainer becomes SimpleLoop's mandatory and only execution runtime.

- All three Claude roles and every harness evaluation run inside one configured
  SIF image.
- Git clone, worktree management, frozen-path gating, commits, history, plotting,
  and scheduling remain host-side.
- The image contains ordinary base tools such as Bash, Git, GCC, G++, Make,
  CMake, Node.js, and Claude Code.
- Large external resources such as `/cvmfs`, input data, maps, and project
  storage are bind-mounted at their existing absolute paths.
- The complete run directory is bind-mounted read/write automatically.
- `--cleanenv` prevents the activated host Python/JUNO environment from leaking
  into agents or evaluation.
- A lightweight tool preflight and a mandatory baseline evaluation reject a bad
  runtime before any optimization agent starts.
- The executor prompt retains its current autonomous style. The design does not
  ban shell changes, debugging tools, or infrastructure investigation.
- SimpleLoop provides a thin command for building a SIF from a user-maintained
  Apptainer definition file.

This is intentionally a breaking change. There is no host-execution fallback.

## 3. User Configuration

Every task config must contain a runtime image. External binds are declared only
when the task needs them:

```yaml
runtime:
  image: /datafs/users/wujxy/images/juno-j26.sif
  binds:
    - /cvmfs
    - /data/juno
    - /datafs/users/wujxy/agent-sci/omilrec_opt
```

### 3.1 Schema

`runtime` is a required top-level object with exactly these fields:

- `image`: required non-empty path to a readable SIF image. A relative value is
  resolved relative to the task config file.
- `binds`: optional list of absolute existing directory paths, defaulting to an
  empty list.

Unknown runtime fields are configuration errors, consistent with SimpleLoop's
existing strict config validation.

### 3.2 Bind semantics

Each configured directory is mounted at the same absolute path inside the
container. The MVP does not support source/destination remapping or a bind-mode
DSL.

The current `run_dir` is always appended to the bind list automatically and
mounted read/write. Mounting the entire run directory is required because a Git
worktree's `.git` file refers to metadata under:

```text
<run_dir>/repo/.git/worktrees/<candidate>
```

Mounting only a candidate worktree would break Git commands inside the
container.

Configured binds are intended for large external directory trees. Small task
files, tests, scripts, fixtures tracked by Git, and ordinary source files remain
inside the cloned repository and candidate worktrees.

## 4. Runtime Command

Add a small `simpleloop/runtime.py` module. It owns Apptainer command assembly,
preflight, and subprocess environment policy.

For a payload command `COMMAND` and a role-specific current directory `CWD`, it
constructs an argv equivalent to:

```bash
apptainer exec \
  --cleanenv \
  --bind /cvmfs,/data/juno,/datafs/users/wujxy/agent-sci/omilrec_opt \
  --bind <run_dir>:<run_dir> \
  --cwd <CWD> \
  <image.sif> \
  COMMAND
```

The implementation uses argv arrays and `shell=False` at the host boundary.
Task evaluation strings are executed by appending:

```text
bash -lc <configured evaluation command>
```

This preserves the existing meaning of `eval.commands` while avoiding a second
host-shell quoting layer.

### 4.1 Hard-coded MVP behavior

The following are product defaults, not configuration options:

- executable: `apptainer`;
- operation: `exec`;
- clean host environment: `--cleanenv`;
- unchanged absolute bind destinations;
- automatic read/write run-directory bind;
- role-specific `--cwd`;
- read-only SIF root filesystem;
- normal host network;
- normal Apptainer home mount.

The normal home mount lets the Claude executable inside the image reuse the
user's existing authentication. `--containall` and `--no-home` are deliberately
not used in the MVP because they require credential seeding and per-run home
state management.

Consequently, the MVP provides software-runtime and environment isolation, not
a complete security sandbox. Files available through the normal home mount may
still be visible to agents.

## 5. Process Boundary

The host and container responsibilities are:

```text
Host SimpleLoop
  ├── config validation
  ├── Apptainer preflight
  ├── local clone and Git worktrees
  ├── frozen/editable path gate
  ├── harness-owned staging and commit
  ├── history, selection, plots, and reports
  │
  └── Apptainer runtime
        ├── proposer Claude
        ├── executor Claude
        │     └── every Claude Bash tool call
        ├── judger Claude
        └── baseline and candidate evaluation commands
```

Claude itself must run inside the container. Prefixing only the build or
evaluation command is insufficient because an executor's arbitrary Bash tool
calls would still run in the host environment.

## 6. Agent Integration

`Agent` receives the runtime object and asks it to wrap the Claude argv.

Container mode does not resolve `claude` with host-side
`shutil.which("claude")`. The literal executable name is passed into the
container, and preflight verifies that the image provides it.

Existing behavior remains unchanged for:

- prompt delivery over stdin;
- stdout and stderr draining;
- heartbeat logging;
- process-group timeout and termination;
- structured-output arguments;
- per-role allowed tools; and
- maximum output-token environment settings.

Only a fixed allowlist of process variables is passed through Apptainer's
supported container-environment mechanism:

- `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, set by SimpleLoop;
- Claude endpoint/auth variables when present:
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_BASE_URL`;
- standard upper- and lower-case HTTP proxy variables; and
- `SSL_CERT_FILE` and `SSL_CERT_DIR` when present.

Values are never printed. The complete host `os.environ` is no longer the
payload environment. File-based Claude authentication remains available through
the normal home mount.

No restrictive executor debugging rules are added. Once the runtime is
validated, the executor remains responsible for choosing an appropriate
implementation and verification strategy.

## 7. Evaluation Integration

`judger.run_eval` uses the same runtime object, image, binds, and cwd handling as
the agents. For an existing command:

```yaml
eval:
  commands:
    - bash scripts/sl_eval_post_v107.sh --evtmax 10
```

the payload inside the container is:

```text
bash -lc "bash scripts/sl_eval_post_v107.sh --evtmax 10"
```

Evaluation timeout, output capture, metric parsing, and result presentation
retain their existing semantics.

The runtime wrapper is shared rather than separately implemented in
`agent.py` and `judger.py`; otherwise the executor and authoritative evaluation
could silently receive different bind or environment behavior.

## 8. Fail-Fast Runtime Validation

Validation has two layers.

### 8.1 Static and tool preflight

Before workspace setup or any Claude call, SimpleLoop verifies:

1. `apptainer` is executable on the host;
2. the configured SIF exists and is readable;
3. every configured bind is an existing directory;
4. the run directory exists;
5. the container starts with the computed binds and cwd;
6. `bash`, `git`, `gcc`, `g++`, `make`, `cmake`, `node`, and `claude` are
   discoverable inside the image; and
7. the run directory is writable inside the container.

Failure identifies the missing executable or path and exits before a proposer
or executor consumes tokens.

### 8.2 Mandatory baseline evaluation

SimpleLoop already evaluates the unoptimized baseline once. That evaluation is
currently best-effort: exceptions are logged and the run continues. Under the
mandatory runtime design, a configured baseline evaluation becomes an
environment acceptance test and must pass before optimization begins.

When `eval.metrics` is configured, baseline acceptance requires:

- the objective key to be present and parseable; and
- every declared gate to parse as passing.

When evaluation commands exist without a metrics schema, every baseline command
must exit successfully.

If no evaluation commands are configured, tool preflight is the complete
runtime acceptance test.

A failing baseline aborts the run with its command, exit status, captured output,
image, binds, and cwd. It is not delegated to an executor for diagnosis.

This change addresses environment rabbit holes structurally while preserving
executor autonomy: an executor only starts after the exact runtime and task
baseline have demonstrated that they work together.

## 9. Base Tools and External Resources

GCC, CMake, and Git must be installed in the SIF rather than bind-mounted as
individual host executables.

These programs depend on directory trees outside `/usr/bin`:

- GCC uses `cc1`, `collect2`, the linker, startup objects, standard headers, and
  compiler runtime libraries.
- CMake uses modules under `/usr/share/cmake-*` and linked libraries.
- Git uses `git-core`, SSL/curl libraries, certificates, and configuration.

Binding enough of host `/usr` to make these tools work would reintroduce host
ABI dependencies and defeat environment isolation. A task-specific toolchain
that is already self-contained may be exposed by binding its complete root
directory. JUNO's external toolchains are naturally available through the
configured `/cvmfs` bind.

The SIF definition should pin the base distribution and relevant tool versions.
A representative definition contains:

```text
Bootstrap: docker
From: almalinux:9

%post
    dnf install -y \
        bash git gcc gcc-c++ make cmake \
        nodejs npm coreutils findutils diffutils patch
    npm install -g @anthropic-ai/claude-code@2.1.216
    dnf clean all

%test
    bash --version
    git --version
    gcc --version
    cmake --version
    node --version
    claude --version
```

Definition files are version-controlled. SimpleLoop does not generate them or
infer project dependencies. User projects maintain their own definition; the
repository also ships maintained definitions for its examples as described
below.

## 10. SIF Build Shortcut

Add:

```bash
simpleloop image build environments/juno.def
```

The default output is the definition path with its suffix replaced by `.sif`:

```text
environments/juno.sif
```

An optional override is supported:

```bash
simpleloop image build environments/juno.def \
  --output /datafs/users/wujxy/images/juno-j26.sif
```

The command is a thin, synchronous wrapper around:

```bash
apptainer build --fakeroot <output.sif> <definition.def>
```

It validates the definition path, output parent, and `apptainer` executable,
then streams Apptainer output and returns its exit status. It does not:

- generate a definition file;
- automatically pull or select a base image;
- manage the Apptainer cache;
- retry network failures;
- overwrite an existing SIF silently; or
- build an image as an implicit part of `simpleloop run`.

## 11. Example Definition Files

The repository ships four independent, regular definition files:

```text
examples/apptainer.def
examples/tiny_algo_opt/apptainer.def
examples/omilrec-opt/apptainer.def
examples/omilrec-post-v107-opt/apptainer.def
```

They are real files rather than symlinks. Each example directory remains
self-contained when copied out of the SimpleLoop repository, and each
definition may evolve with its task without changing another example.

### 11.1 Root template

`examples/apptainer.def` is the generic reference template. It uses an
AlmaLinux 9 base and installs the common SimpleLoop payload:

- Bash, Git, GCC, G++, Make, CMake, and basic command-line utilities;
- Python 3, pip, and pytest for the generic Python evaluation shown in
  `examples/task.yaml`; and
- pinned Node.js and Claude Code.

Its comments explain that large task resources belong in `runtime.binds`, not
in the SIF. Its `%test` checks the installed base tools only.

### 11.2 Tiny algorithm definition

`examples/tiny_algo_opt/apptainer.def` is independently buildable and includes
the common payload plus the Python/pytest dependencies needed by its
correctness, drift, and benchmark commands. It does not copy the toy repository
into the image; the run-directory bind supplies the cloned candidate worktree.

### 11.3 OMILREC definitions

`examples/omilrec-opt/apptainer.def` and
`examples/omilrec-post-v107-opt/apptainer.def` are independent files with the
same initial JUNO-compatible AlmaLinux 9 tool base. They do not copy JUNO,
reconstruction maps, inputs, fixtures, or source packages into the image.
Those remain external and are exposed at their unchanged paths through the
example task config's `/cvmfs`, `/data/juno`, and project-storage binds.

The two files may initially have identical package lists, but remain separate
because the tasks have different source packages, gates, and likely future
runtime needs.

### 11.4 Example commands and configs

Every example README shows its local build command:

```bash
simpleloop image build examples/apptainer.def
simpleloop image build examples/tiny_algo_opt/apptainer.def
simpleloop image build examples/omilrec-opt/apptainer.def
simpleloop image build examples/omilrec-post-v107-opt/apptainer.def
```

The default output is the adjacent `apptainer.sif`. Each runnable example
config uses that relative image path in its required `runtime` block. The root
`examples/task.yaml` reference template uses `apptainer.sif` beside itself.

Generated `*.sif` files are ignored by Git and are never committed. Example
READMEs list required external binds and explain that a user may point
`runtime.image` at one shared prebuilt SIF instead of building every example
image separately.

## 12. Logging and Errors

At run startup, SimpleLoop prints one concise runtime block:

```text
runtime: apptainer
image: /datafs/users/wujxy/images/juno-j26.sif
binds: /cvmfs, /data/juno, /datafs/users/wujxy/agent-sci/omilrec_opt
preflight: PASS
```

Agent heartbeat lines continue to show label, elapsed time, PID, and cwd. The
image path is printed once rather than repeated on every heartbeat.

User-facing failure classes are:

- config error: missing/invalid runtime fields;
- runtime preflight error: missing Apptainer, image, bind, tool, or write access;
- baseline acceptance error: task environment starts but cannot pass baseline
  evaluation; and
- ordinary agent/evaluation timeout or non-zero execution error.

Errors include the failing payload command without printing secrets or the
complete host environment.

## 13. Compatibility and Migration

Host execution is removed. Every example config gains a `runtime` block,
including the generic reference template and tiny algorithm example. Every
current runnable example directory gains its independent `apptainer.def`, while
the `examples/` root gains the generic definition template.

Old configs fail validation with a direct message explaining the required
fields. Continuing an old run is supported only after adding a runtime block to
its task config. Existing history and commit data require no conversion.

This breaking change should be called out in the release notes. There is no
temporary environment-variable escape hatch and no automatic host fallback,
because either would preserve the dual runtime behavior the design is intended
to eliminate.

## 14. Testing

Unit tests mock process execution and cover:

### Configuration

- missing `runtime` is rejected;
- missing or unreadable image is rejected;
- relative images resolve from the config directory;
- binds default to an empty list and, when supplied, contain only absolute
  existing directories;
- unknown runtime fields are rejected.

### Runtime argv

- fixed `apptainer exec --cleanenv` prefix;
- same-path configured binds;
- automatic run-directory bind;
- role-specific cwd;
- paths containing spaces remain single argv elements;
- agent and evaluation paths use the same builder.

### Preflight and baseline

- missing host Apptainer fails before agent creation;
- missing in-image tool names are reported;
- non-writable run directory fails;
- passing baseline permits the first proposer;
- a failed gate, absent objective, non-zero command, or timeout prevents all
  agent calls;
- no-eval tasks proceed after tool preflight.

### Agent and evaluation behavior

- prompt still travels through stdin;
- timeouts terminate the Apptainer process group;
- evaluator invokes `bash -lc` inside the container;
- metrics are parsed from container output unchanged.

### Image command

- default `.sif` output derivation;
- explicit output override;
- existing output requires an explicit user decision rather than silent
  overwrite;
- `apptainer build --fakeroot` argv and exit-code propagation.

### Example assets

- the root and all three current example directories contain regular
  `apptainer.def` files rather than symlinks;
- every definition pins Claude Code and declares the required base tools;
- the tiny definition includes its Python test dependencies;
- both OMILREC definitions omit JUNO data and document external binds;
- each example config references its adjacent default SIF;
- every example README contains the corresponding image-build command; and
- generated SIF files are ignored by Git.

An optional local smoke test may use a small prebuilt image, but normal unit
tests and CI do not build a SIF or require Apptainer.

## 15. Explicitly Out of Scope

The MVP does not include:

- host runtime compatibility;
- Docker, Podman, or another runtime backend;
- `--containall`, isolated home, or credential seeding;
- bind destination remapping or per-bind permission syntax;
- user-configured Apptainer flags;
- arbitrary environment-variable passthrough;
- GPU flags, resource limits, or network namespaces;
- automatic definition generation, registries, signing, cache management, or
  provenance;
- per-role images; or
- security-sandbox claims.

These can be reconsidered only after concrete usage demonstrates a need.

## 16. Success Criteria

The feature is complete when:

1. SimpleLoop rejects configs without an Apptainer image.
2. Every Claude role and every evaluation command demonstrably executes inside
   the same configured SIF.
3. An activated host venv and host `PYTHONPATH`/`LD_LIBRARY_PATH` do not appear
   in payload processes.
4. The run directory and configured external resources are accessible at their
   original absolute paths.
5. A broken toolchain or JUNO baseline fails before the first agent call.
6. Host-side Git gating and commits continue to work.
7. A user can build a version-controlled definition into a SIF with the
   `simpleloop image build` shortcut.
8. The generic template and all three current example directories ship
   independent, buildable definition files and documented build commands.
