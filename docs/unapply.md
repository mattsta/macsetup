# Unapply

`macsetup unapply` reverses setup intent in reverse graph order. It has two
explicit modes:

```bash
uv run macsetup unapply --from-run latest --dry-run
uv run macsetup unapply --from-run ~/.macsetup/runs/20260531T120000Z.json --yes
uv run macsetup unapply --force --tags files --dry-run
```

`--from-run` is the safer mode. It only considers steps that a selected apply
journal recorded as `applied`. Apply journals include `changes` metadata for
file content, package installs, Git values, chmod state, pyenv state, and
package manager installs where macsetup can capture that data. Older journals
are still usable when command evidence is enough, such as recorded
`brew install ...` commands.

`--force` is the reset/test mode. It reverses the currently selected profile
intent even when there is no journal proving macsetup created the state. Use
tags to keep the blast radius explicit:

```bash
uv run macsetup unapply --force --tags files --dry-run
uv run macsetup unapply --force --tags packages --dry-run
```

Non-dry-run unapply requires `--yes`, and privileged inverse operations require
`--allow-privileged`.

Homebrew cask uninstalls are treated as privileged operations because cask
metadata may stop launchd jobs, quit GUI apps, and remove helper files under
system-owned paths. Before running real cask uninstalls, macsetup validates
`sudo -v` with terminal IO so password prompts are visible while the main
process stays under the normal user. If a command still prints a
password/passphrase prompt without a trailing newline, the runner emits it as
`PROMPT` and pauses heartbeat status lines until the prompt clears. Cask
uninstall commands also have a hard timeout so a stuck app/helper cannot run
forever.

## Current Inverses

- Managed marker blocks remove only their own `macsetup:<marker>` block.
- Managed files and generated launchers are removed with backups.
- Homebrew formula/cask groups uninstall journal-recorded packages, or all
  current-profile packages in `--force` mode.
- Homebrew formula uninstalls are consolidated across selected package groups
  and ordered from dependents to dependencies using local Homebrew install
  receipts under `$(brew --prefix)/Cellar` plus local Ruby formula files under
  installed Homebrew taps, such as
  `$(brew --prefix)/Library/Taps/homebrew/homebrew-core/Formula`. If a selected
  formula is required by an installed formula outside the unapply set, macsetup
  blocks before running `brew uninstall` and reports the blocker.
- Git settings restore recorded previous values, or unset keys in `--force`
  mode.
- Touch ID sudo setup comments the active `pam_tid.so` line in
  `/etc/pam.d/sudo_local`.
- pyenv Python setup restores recorded global state and uninstalls versions that
  were installed by the run; `--force` uninstalls the configured version.
- Python and npm global packages uninstall recorded missing packages, or all
  current-profile packages in `--force` mode.

## Non-Reversible Or Manual

Some operations are intentionally not blindly inverted:

- Homebrew bootstrap is not automatically uninstalled.
- Xcode license acceptance is not reversible by macsetup.
- One-shot service/cache commands, such as DNS restarts and cache flushes, are
  reported as manual/no-inverse.
- Cleanup steps such as stale PhantomJS metadata removal are not restorable
  unless the removed data has been archived outside macsetup.
- Permission sweeps that do not record complete prior recursive modes are not
  blindly reversed.

## Journal Scope

Apply journals write `operation: "apply"` and unapply journals write
`operation: "unapply"`. Completed runs write `status: "completed"`; interrupted
non-dry-run apply/unapply runs write `status: "interrupted"` with completed
step results plus a failed marker for the in-flight step. `--from-run` still only
uses journal results recorded as `applied`, so an interrupted in-flight step is
not blindly inverted. macsetup refuses to unapply an unapply journal. When an
older apply journal lacks previous-value metadata, the inverse step fails closed
and tells you whether `--force` is the appropriate reset path.
