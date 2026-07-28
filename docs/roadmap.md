# Roadmap — Deferred Items

Features intentionally out of scope for the initial in-tree upstreaming
(OpenStack Gerrit change [989998](https://review.opendev.org/c/openstack/manila/+/989998),
"Add Weka share driver"). Each item was raised in review, answered with
"planned as a follow-up," and the corresponding Gerrit thread was marked
resolved so the change could land. This file is the durable tracker: when we
pick an item up, reopen/reply to the referenced Gerrit comment(s).

## 1. Multi-tenancy: per-project Weka-org isolation (DHSS=true)

The initial driver runs single-org, `driver_handles_share_servers=false`.
Reviewer (Stig Telfer) asked about isolating tenants into separate Weka orgs
via Manila share-server support.

- Direction: add share-server / DHSS support so each Manila project maps to a
  dedicated Weka organization.
- Gerrit threads (change 989998):
  - `/PATCHSET_LEVEL`, our reply `e12e2bc1` (in reply to `a053d8dc`)
  - `/PATCHSET_LEVEL`, our reply `41e62df8` (in reply to `a8ea454d`)

## 2. Multi-tenancy: `weka_api_server` as a share-type extra_specs override

Related to #1. Stig suggested treating `weka_api_server` as a share-type
`extra_specs` override mapping a share type to a Weka network space / tenancy —
the same pattern as `vast_vippool_name` in
[change 963494](https://review.opendev.org/c/openstack/manila/+/963494).

- Direction: support per-share-type Weka network-space / tenancy selection via
  extra_specs.
- Gerrit thread (change 989998):
  - `doc/source/admin/weka_share_driver.rst:125`, our reply `fcb996e2`
    (in reply to `a59f220a`)

## 3. WEKAFS access enforcement via IP-based security policies — IMPLEMENTED

**Status: done.** WEKAFS `ip` access rules are now enforced with Weka
per-filesystem security policies (IPv4). The driver maps each `ip` rule to a
per-share `Allow` policy and honors `--access-level` (`ro` installs a
read-only policy). Two models are supported:

- **Model A (per share, default):** `ip` rules drive two per-share policies
  (`manila-<share8>-rw` / `-ro`). Once a policy is attached, a client whose
  source IP matches no policy is denied the mount.
- **Model B (named group):** a share type with the
  `weka:security_policy_group` extra spec attaches shared group policies
  (`manila-grp-<group>-*`) defined by the `weka_security_policy_group`
  config, reused across every share of the type (see
  [configuration.md](configuration.md)).

`user`/`cert` rules still grant the org-boundary mount credential (they
cannot map to an IP policy). See
[known-issues.md §6](known-issues.md#6-wekafs-access-control-scope-and-limits)
for the remaining scope/limits (client-IP granularity, the interface-group
caveat, and the per-org policy budget).

- Original Gerrit threads (change 989998):
  - `doc/source/admin/weka_share_driver.rst:237`, our reply `66d1d827`
    (in reply to `61f54ac6`)
  - `manila/share/drivers/weka/driver.py:660`, our reply `4701a7f2`
    (in reply to `d423f5e1`)

### Remaining follow-ups
- Per-*user* (not per-IP) WEKAFS access — would need POSIX ACLs (uid-based,
  client-trust) or NFS Kerberos; not covered by IP policies.
- Scaling past the per-org policy budget for very large share counts with
  unique per-share rules (use Model B group reuse to stay within budget).

## 4. QoS / thin provisioning

Weka QoS is CLI-only today (not exposed in the REST API v2 the driver uses), so
per-share QoS cannot be set through the driver. Thin provisioning is the
recommended fast-follow. See `docs/known-issues.md`.
