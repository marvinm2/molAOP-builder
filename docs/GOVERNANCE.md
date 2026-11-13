# Governance — Molecular AOP Builder

**Version:** 1.0
**Date:** 2026-05-14
**Supersedes:** the implicit role definitions held in the `ADMIN_USERS` environment variable and in code-level access controls.

This document specifies who is **Responsible / Accountable / Consulted / Informed** (RACI) for each material activity in the lifecycle of the Molecular AOP Builder. It closes the *"Role and responsibility matrix"* item in [`docs/DMP.md`](DMP.md) §7 and supports DMP §4 (Allocation of resources).

---

## 1. Roles

| Role | Holder(s) today | Source of truth |
|------|-----------------|-----------------|
| **Data Steward** | Marvin Martens — Department of Translational Genomics, Maastricht University · [ORCID 0000-0003-2230-0840](https://orcid.org/0000-0003-2230-0840) | This document |
| **Lead Developer** | Marvin Martens (GitHub [`marvinm2`](https://github.com/marvinm2)) | `git log --pretty=format:'%an'` |
| **Curator / Admin** | Holders of admin privilege on the live instance | `ADMIN_USERS` environment variable on the swarm service (`ssh tgx1 'docker service inspect molaop-builder \| grep ADMIN_USERS'`) |
| **Proposer** | Any user who has authenticated via OAuth (GitHub, ORCID, LS Login, or SURFconext) and submitted a mapping proposal | `proposals`, `ke_go_proposals`, and `ke_reactome_proposals` tables in `ke_wp_mapping.db` |
| **Cluster Operator** | The TGX cluster administrator (Translational Genomics, Maastricht University) — VHP4Safety Strato Swarm | `/mnt/gluster/documentation/operations/user-management.md` on `tgx1` |
| **Data Protection Officer** | Maastricht University institutional DPO | Direct contact channel TBC; subject-rights requests are routed via the Data Steward, who forwards to the DPO for sign-off. |

Multiple roles may be held by the same person; today the Data Steward and Lead Developer are both Marvin Martens. The matrix below operates on *roles* — assignment to people is fluid and recorded in the table above.

---

## 2. RACI matrix

**R** = Responsible (does the work) · **A** = Accountable (one per row; signs off) · **C** = Consulted (two-way input before action) · **I** = Informed (one-way notice after action) · **—** = not involved.

| Activity | Proposer | Curator / Admin | Data Steward | Lead Developer | Cluster Operator | DPO |
|----------|:--------:|:---------------:|:------------:|:--------------:|:----------------:|:---:|
| Submit a new mapping proposal | **R/A** | I | I | — | — | — |
| Review and approve a proposal | — | **R/A** | C | — | — | — |
| Reject or withdraw a proposal | — | **R/A** | I | — | — | — |
| Schema migration / new mapping resource | I | C | **A** | **R** | I | — |
| Update assessment rubric / scoring config | I | C | **A** | **R** | — | — |
| Application code change (non-schema) | — | — | I | **R/A** | — | — |
| Container image build + push to `ghcr.io` | — | — | I | **R/A** | I | — |
| Production deploy on Strato Swarm | — | — | C | **R/A** | C | — |
| Zenodo release of curated dataset | — | I | **A** | **R** | — | — |
| DMP / Governance revision | — | C | **R/A** | C | — | C |
| Scheduled database backup | — | — | C | **R** | **A** | — |
| Database restore from backup | — | — | C | **R** | **A** | — |
| GlusterFS / Traefik / TLS / DNS change | — | — | I | C | **R/A** | — |
| Production incident response | — | C | I | **R** | **A** | — |
| Security advisory triage (Dependabot, CVEs) | — | — | I | **R/A** | C | — |
| GDPR subject-rights request (access, rectification, erasure, restriction, portability, objection) | — | I | C | **R** | — | **A** |
| Privacy notice / retention policy revision | — | — | **R** | C | — | **A** |

Each row has **exactly one Accountable role**; that role signs off and owns the decision record.

---

## 3. Escalation

- **Cluster-wide change or shared-infrastructure operation:** contact the Cluster Operator before action. The change must reference an entry in `/mnt/gluster/documentation/operations/` on `tgx1`.
- **Data-protection request or GDPR subject right:** route to the MU institutional DPO. The Data Steward acts as the technical contact and prepares the response; the DPO signs off before any data is released or erased.
- **Disagreement between curators on a proposal decision:** the Data Steward decides. Rationale is recorded in the `proposals` row's notes field so the audit trail remains intact.
- **Security incident (suspected breach, credential compromise, dependency CVE in active exploit):** Lead Developer triages immediately; if the data layer is affected, the DPO is notified within 24 hours per MU institutional policy.

---

## 4. Review cadence

This document is revised:

- On every minor release of the Builder, matched to the DMP release cadence.
- Whenever a role holder changes — record the change by editing the **Holder(s) today** column in §1 and adding a row to §5 below.
- Whenever a new activity is added that materially shifts responsibility (e.g. a new mapping resource type, a new deployment target).

---

## 5. Version history

| Version | Date | Change |
|---------|------|--------|
| 1.0 | 2026-05-14 | Initial RACI matrix and role registry. Closes DMP §7 "Role and responsibility matrix" item. |
