# FXRoute Project State

Updated: 2026-07-18

## Roles

- `/home/pbclaw/ai/projects/fxroute`: current internal working tree and source of truth for ongoing development.
- `/home/pbclaw/ai/projects/fxroute-public`: separate public-release working tree; push only after the internal state is accepted for publication.
- `/home/pbclaw/ai/projects/_fxroute-archive/`: retained historical worktrees, experiments, diagnostics, and backups.

## Current baseline

- Release candidate: `0.7.55`
- Pre-release Git HEAD: `a22fa0edafdc880f4a06b18fb1df945b5a2af69b`
- Base history: copied from `fxroute-public`, including its tracked working-tree changes but excluding untracked backup files.
- Public remote: `https://github.com/CobbyCode/fxroute.git`

## Comparison with the running `.104` system

The active `/home/paul/fxroute` tree on `192.168.178.104` was inspected read-only on 2026-07-18. Its HEAD is `70ae165cb875649afa03742f1d38da56de748b82`, version `0.7.54`.

- 105 of 107 files tracked on `.104` are byte-identical to this internal working tree.
- The only differing files among `.104` tracked paths are `README.md` and `MANUAL.md`; these are documentation, not active runtime code.
- This internal tree additionally contains seven focused AutoSub/AutoGain test scripts from the newer local history.
- `.104` runtime state, credentials, media/cache data, loose backups, and untracked files were not copied.
- `.104` was not modified, restarted, deployed, or reconfigured.

The runtime source matches the accepted `.104` system. Release `0.7.55` adds the integrated, measured AutoGain workflow, protected polarity selection, bounded verification and rollback behavior, and continuous AutoSub job polling. The focused AutoSub/AutoGain and runtime suites passed before release; `.104` remained read-only.
