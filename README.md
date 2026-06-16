# openshift_noobaa

Diagnostic and repair tool for NooBaa CrashLoopBackOff issues on OpenShift Data Foundation (ODF).

This script automates the troubleshooting and repair of two recurring NooBaa failures that require manual `oc` commands to resolve. Rather than memorizing the exact sequence of commands or digging through logs each time, run this tool to diagnose the issue and optionally apply the fix.

## Quick Start

```bash
# Clone the repo:
git clone https://github.com/syangsao/openshift_noobaa.git
cd openshift_noobaa

# Copy and configure .env:
cp .env.example .env
# Edit .env with your SSH host, user, key, and kubeconfig path

# Diagnose everything (no changes):
python3 noobaa_fix.py --check-only

# See what would be done without making changes:
python3 noobaa_fix.py --dry-run

# Diagnose and fix automatically:
python3 noobaa_fix.py
```

## Configuration

The script loads configuration from three sources in priority order (highest first):

1. **CLI flags** — `--ssh`, `--ssh-user`, `--ssh-key`, `--kubeconfig`, `--namespace`
2. **Environment variables** — `SSH_HOST`, `SSH_USER`, `SSH_KEY`, `KUBECONFIG`, `NAMESPACE`
3. **`.env` file** — placed next to the script or in the current working directory

Copy `.env.example` to `.env` and fill in your values:

```ini
# SSH Jump Host (required for SSH mode)
SSH_HOST=your-jump-host.example.com
SSH_USER=your-username
SSH_KEY=~/.ssh/your-key

# OpenShift
KUBECONFIG=~/path/to/kubeconfig
NAMESPACE=openshift-storage
```

### Connection Modes

**SSH through jump host (default):**
```bash
python3 noobaa_fix.py
# Reads SSH_HOST, SSH_USER, SSH_KEY, KUBECONFIG from .env
```

**Direct access (oc already configured locally):**
```bash
python3 noobaa_fix.py --direct
# Uses KUBECONFIG from .env, env var, or system
```

### Command-Line Options

| Flag | Description |
|------|-------------|
| `--direct` | Connect directly using local `oc` + `KUBECONFIG` |
| `--ssh HOST` | SSH jump host (overrides .env / env var) |
| `--ssh-user USER` | SSH username (overrides .env / env var) |
| `--ssh-key PATH` | SSH private key path (overrides .env / env var) |
| `--kubeconfig PATH` | Path to kubeconfig (overrides .env / env var) |
| `--namespace NS` | OpenShift namespace (overrides .env / env var) |
| `--env-file PATH` | Custom .env file path |
| `--dry-run` | Show what would be done without making changes |
| `--check-only` | Run diagnostics only, skip fixes |
| `--fix ISSUE` | Fix specific issue: `version-mismatch`, `pg-replica`, or `all` (default: `all`) |

## Problem

On OpenShift clusters running ODF with a standalone MCG (Multi-Cloud Gateway) configuration, NooBaa can enter degraded states that the ODF operator does not automatically recover from. The following two issues have been observed and resolved:

### Issue 1: Backing Store Agent Version Mismatch

**What happens:** After an ODF operator upgrade, backing store agent pods (`noobaa-default-backing-store-noobaa-pod-*`, `loki-backingstore-*`, `quay-backingstore-*`) enter `CrashLoopBackOff` with restarts accumulating every 30–60 seconds.

**Root cause:** The ODF operator upgrades the backing store agent pods to a newer `mcg-core-rhel9` image, but the `noobaa-core` StatefulSet retains the old image. The agents perform a version check on heartbeat, detect the mismatch, and exit cleanly with code 0 — which Kubernetes interprets as a crash and restarts them immediately.

The NooBaa CR's `spec.image` field may already contain the correct new image, but the ODF operator does not reconcile the StatefulSet image automatically. Simply deleting `noobaa-core-0` is not enough because it restarts with the same old image from the StatefulSet spec.

**Agent log output:**
```
identified version change: res.version 5.21.0-7ca6daa pkg.version 5.21.4-916a765
no messages channel to parent process. exit with code 0
```

**How this script fixes it:** Reads the agent image, then runs `oc set image sts/noobaa-core` to align both the `core` and `noobaa-log-processor` containers to the agent image version. After the patch, `noobaa-core-0` restarts with the new image and agents reconnect within 1–2 minutes.

### Issue 2: CNPG PostgreSQL Replica Stuck in pg_rewind Loop

**What happens:** A NooBaa database replica pod (e.g., `noobaa-db-pg-cluster-2`) is stuck at `0/1 Running` for days. The CNPG cluster status shows `Ready: False` with phase `Waiting for the instances to become active`.

**Root cause:** The replica's `pgdata` diverged from the primary too far back in the WAL timeline. CloudNative PostgreSQL (CNPG) attempts to resync using `pg_rewind`, but `pg_rewind` requires a common WAL ancestor between source and target. When none exists, `pg_rewind` fails repeatedly. In this CNPG version (PostgreSQL 16), neither the in-pod controller nor the cluster-level controller falls back to `pg_basebackup`, so the loop is indefinite.

**Replica log output:**
```
pg_rewind: error: could not find common ancestor of the source and target cluster's timelines
Failed to execute pg_rewind: exit status 1
This is an old primary instance, waiting for the switchover to finish
```

**How this script fixes it:** Performs a 4-step manual resync:
1. **Clear pgdata** on the broken replica using `find -mindepth 1 -delete`
2. **Remove CNPG config files** (`pg_hba.conf`, `pg_ident.conf`) that CNPG places in empty pgdata
3. **Run `pg_basebackup`** from the primary (`noobaa-db-pg-cluster-rw`) using environment variables for SSL (`PGSSLKEY`, `PGSSLCERT`, `PGSSLROOTCERT`, `PGSSLMODE`) — this CNPG PostgreSQL version uses env vars, not `pg_basebackup` flags
4. **Delete the pod** so CNPG restarts it with the fresh pgdata

After resync, CNPG may promote the resynced instance as the new primary and re-bootstrap the old primary as a replica. This is normal — both instances end up `Running 1/1`.

## Why These Failures Aren't Automatic

The ODF operator and CNPG are designed for common failure scenarios (pod evictions, transient network issues). These are edge cases where:

- The ODF operator **does not reconcile StatefulSet images** after an upgrade — it updates `spec.image` on the NooBaa CR but doesn't propagate to the StatefulSet
- CNPG **does not fall back to `pg_basebackup`** when `pg_rewind` fails with "no common ancestor" in this version — it keeps retrying `pg_rewind` indefinitely

Both require manual intervention. This script codifies that intervention so it can be run consistently without referencing notes or documentation.

## How It Works

The script runs in three phases:

### Phase 1: Overall Status

Lists all pods in the namespace with status counts (Running, CrashLoopBackOff, etc.) and checks the NooBaa CR phase. Gives you a quick picture of cluster health before diving into specifics.

### Phase 2: Version Mismatch Diagnosis

1. Reads `noobaa-core` StatefulSet container image
2. Reads backing store agent pod image (tries label selector, then pod name pattern, then full pod list as fallback)
3. Extracts and compares version strings
4. Scans all pods for `CrashLoopBackOff` status
5. If mismatch detected and `--fix` is enabled, runs `oc set image` and verifies the result

### Phase 3: PostgreSQL Replica Diagnosis

1. Queries CNPG cluster status (`instances`, `readyInstances`, `phase`)
2. Lists all `noobaa-db-pg-cluster` pods and checks for `0/1 Running` or `CrashLoopBackOff`
3. If a stuck replica is found, reads the last 100 log lines looking for `pg_rewind` / `common ancestor` / `PG_VERSION` errors
4. If the issue is identified and `--fix` is enabled, performs the 4-step `pg_basebackup` resync

## Requirements

- Python 3.10+ (uses `list[str]` type hints)
- `oc` CLI available on the system (or on the jump host)
- SSH access to jump host (when not using `--direct`)
- `system:admin` or equivalent cluster permissions to read pods, StatefulSets, and execute into pods

## History

| Date | Issue | Resolution |
|------|-------|------------|
| 2026-06-12 | Backing store agents CrashLoopBackOff (core v5.21.0 vs agents v5.21.4) | `oc set image sts/noobaa-core` |
| 2026-06-12 | `noobaa-db-pg-cluster-2` stuck in pg_rewind loop | Manual pg_basebackup resync |

## License

MIT
