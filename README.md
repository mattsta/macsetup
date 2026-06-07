# macsetup

Reusable macOS developer-workstation setup tooling with typed profiles, managed
templates, graph-ordered steps, safe apply/reapply behavior, and explicit undo
support.

The tool is intentionally stdlib-only, supports Python 3.11+ for bootstrap
compatibility, and exposes a `uv run` entrypoint from `pyproject.toml`:

```bash
uv run macsetup audit
uv run macsetup graph
uv run macsetup plan
uv run macsetup preview
uv run macsetup apply --dry-run --diff
uv run macsetup unapply --from-run latest --dry-run
```

Install `uv` before using the managed command wrappers; launchers run commands
through the project with `uv --directory <checkout> run ...`.

`apply` is the only mutating command. It asks before changing anything unless
`--yes` is provided. File changes use managed marker blocks or dedicated managed
files, existing files are backed up under `~/.macsetup/backups`, and successful
non-dry-run applies write a JSON journal under `~/.macsetup/runs`. Apply runs
also print live check, step, command, heartbeat, and package-level state lines so
long package installs do not sit silent. Before confirmation, apply prints an
`Actionable Changes To Apply` section with the exact subset that will be applied.

## Quick Start

Inspect the packaged defaults and modernization notes:

```bash
uv run macsetup audit
```

Plan the full default setup:

```bash
uv run macsetup plan
```

Plan with a private local overlay:

```bash
uv run macsetup plan --profile local/private.toml
```

Show the exact managed file/block diffs that would be written:

```bash
uv run macsetup preview --profile local/private.toml
uv run macsetup plan --diff --profile local/private.toml
```

Do a non-mutating apply simulation:

```bash
uv run macsetup apply --dry-run --diff --profile local/private.toml
```

Preview undoing the most recent recorded apply:

```bash
uv run macsetup unapply --from-run latest --dry-run --profile local/private.toml
```

Apply non-privileged dotfile/editor/Git changes first:

```bash
uv run macsetup apply --tags files --skip-tags dns --profile local/private.toml --yes
```

## Mac-To-Mac Transfer

For new-machine copy workflows over Wi-Fi, see
[`docs/mac-transfer.md`](docs/mac-transfer.md). It includes reusable `ditto`
send/receive commands, filtered tar transfer commands, and a tuned rsync wrapper
for incremental repo syncs, including direct rsync daemon mode when SSH and SMB
are undesirable. These live under the separate multi-system sync branch of the
CLI:

```bash
uv run macsetup sync auto-server --help
uv run macsetup sync auto-sync --help
uv run macsetup sync rsync-daemon-server --help
```

For day-to-day rsync usability, see [`docs/mrsync.md`](docs/mrsync.md). The
managed `mrsync` wrapper injects global/local rsync filter files, keeps project
`.rsync-filter` support enabled, and has `mrsync --audit` for debugging what is
being ignored.

### Data Sync To A New System

For a first data copy to a new Mac, install only the sync support on the new
machine, start the receiver there, then send selected source trees from the
existing machine. The default initial-copy path uses tar because managed
generated-state excludes are active; this skips nested caches/build output such
as `.venv/`, `__pycache__/`, `node_modules/`, `.uv-cache/`, `.ruff_cache/`,
`.hypothesis/`, `.tmp/`, `build/`, `dist/`, and `target/`.

On the new Mac:

```bash
cd /path/to/macsetup
uv run macsetup preview --tags sync
uv run macsetup apply --tags sync --yes
uv run macsetup sync auto-server --bind <new-mac-lan-ip> --port 17000 --dest "$HOME" --no-sudo
```

On the existing Mac:

```bash
cd /path/to/macsetup
uv run macsetup sync auto-sync --host <new-mac-lan-ip> --port 17000 --source "$HOME/path-to-source-tree" --no-sudo
```

`auto-server` listens on both the ditto port and the tar port. With default
excludes active, `auto-sync` selects tar and connects to `17001`. Add private
large-tree excludes to gitignored `local/rsync-excludes.txt`; tar and rsync
pick that file up automatically when it exists.
For source-code trees, add `--no-mac-metadata` on both sides if ACLs, xattrs,
and resource forks are not useful enough to justify the extra tar overhead.
Leave the receiver running while you send multiple selected source trees; stop
it with Ctrl-C when finished. Use `--once` for scripted one-shot receives.

Do not raw-copy a whole home directory into `/Users/<user>` on a new Mac. A
whole home copy includes `~/Library`, and raw `rsync`/`ditto` replacement of
`~/Library` can break machine-local app state, privacy databases, keychains,
containers, sync metadata, and other live macOS state. Use Migration Assistant or
Time Machine for whole-account/system migration.

For a safer manual uplift, copy selected user data folders:

```bash
cd /path/to/macsetup
uv run macsetup preview --tags sync
uv run macsetup apply --tags sync --yes
uv run macsetup sync auto-server --bind <new-mac-lan-ip> --port 17000 --dest "$HOME" --no-sudo
```

Then run one or more selected sends from the old machine:

```bash
cd /path/to/macsetup
uv run macsetup sync auto-sync --host <new-mac-lan-ip> --port 17000 --source "$HOME/Documents" --no-sudo
uv run macsetup sync auto-sync --host <new-mac-lan-ip> --port 17000 --source "$HOME/Desktop" --no-sudo
uv run macsetup sync auto-sync --host <new-mac-lan-ip> --port 17000 --source "$HOME/Downloads" --no-sudo
```

If you intentionally want a broad home-root data pass, exclude `Library/`, app
database packages, and other machine-local state. Dry-run any final rsync and
run from a temporary admin account if the destination account is active:

```bash
uv run macsetup sync rsync-from --host <old-mac-lan-ip> --user <old-user> --source /Users/<old-user>/ --dest /Users/<old-user>/ --exclude Library/ --exclude .Trash/ --exclude .Spotlight-V100/ --exclude .fseventsd/ --exclude 'Pictures/Photos Library.photoslibrary/' --exclude 'Pictures/Photo Booth Library/' --dry-run --itemize
```

For repeated catch-up after the initial stream, use the same defaults through
rsync. With an SMB/local mount:

```bash
uv run macsetup sync auto-sync --rsync-dest /path/to/new-mac-mounted-tree/ --dry-run --itemize "$HOME/path-to-source-tree/"
```

Without SMB, run a temporary LAN-only rsync daemon on the new Mac:

```bash
uv run macsetup sync rsync-daemon-server --bind <new-mac-lan-ip> --port 18730 --dest "$HOME"
```

Then on the existing Mac:

```bash
MACSETUP_RSYNC_PASSWORD='<printed-password>' uv run macsetup sync auto-sync --method rsync-daemon --host <new-mac-lan-ip> --rsync-daemon-port 18730 --rsync-module-path path-to-source-tree --dry-run --itemize --source "$HOME/path-to-source-tree/"
```

For the last catch-up after closing applications on the old machine, run the
pull from the new machine over SSH:

```bash
uv run macsetup sync rsync-from --host <old-mac-lan-ip> --user <old-user> --source /Users/<old-user>/path-to-source-tree/ --dest "$HOME/path-to-source-tree/" --dry-run --itemize
```

Remove `--dry-run --itemize` once the catch-up preview is correct. Use
`--no-default-excludes` only when intentionally copying generated cache/build
trees too. See [`docs/mac-transfer.md`](docs/mac-transfer.md) for the full
runbook, network safety checks, ditto mode, target-side rsync pull, and rsync
daemon details.

## Clean Machine Bring-Up

From a clean macOS install, first make sure `uv` is installed and the checkout
can run through `uv run`. Then use the bootstrap/package path:

```bash
uv run macsetup plan --tags bootstrap,packages --profile local/private.toml
uv run macsetup apply --tags bootstrap,packages --allow-privileged --profile local/private.toml --yes
```

Missing Homebrew is treated as a normal bootstrap action after `apply` is
confirmed. Use `--no-bootstrap` only when you explicitly want to forbid first-time
network bootstrap installers. `--allow-privileged` permits selecting
`/Applications/Xcode.app/Contents/Developer` when Xcode.app is installed but the
system is still anchored on Command Line Tools, accepting an already-installed
Xcode developer tools license if `xcodebuild -license check` reports that it is
still pending, and downloading the Metal toolchain component when it is missing.
Homebrew analytics are disabled immediately after install and checked on later
runs. The same privileged bootstrap path can enable Touch ID authentication for
`sudo` by creating `/etc/pam.d/sudo_local` from Apple's local template.

To manage only the Touch ID sudo setting:

```bash
uv run macsetup plan --tags sudo --allow-privileged
uv run macsetup apply --tags sudo --allow-privileged --yes
```

After packages are present, apply the user-level setup:

```bash
uv run macsetup apply --tags shell,git,python,javascript,editor,files --profile local/private.toml --yes
```

Run DNS service operations only after reviewing the plan:

```bash
uv run macsetup plan --tags dns --profile local/private.toml
uv run macsetup apply --tags dns --allow-privileged --profile local/private.toml --yes
```

The managed focus blocklist is enabled by default. Disable it only for an
exceptional run:

```bash
uv run macsetup apply --tags dns --disable-dns-blocklist --allow-privileged --profile local/private.toml --yes
```

## Ongoing Maintenance

This repo is suitable for ongoing system maintenance and refreshes as long as
the profile sources stay current. The intended loop is:

```bash
uv run macsetup audit --profile local/private.toml
uv run macsetup plan --profile local/private.toml
uv run macsetup preview --profile local/private.toml
uv run macsetup apply --dry-run --diff --profile local/private.toml
uv run macsetup apply --profile local/private.toml --yes
```

For lower-risk refreshes, apply by area:

```bash
uv run macsetup plan --tags packages --profile local/private.toml
uv run macsetup apply --tags packages --profile local/private.toml --yes

uv run macsetup plan --tags apps --profile local/private.toml
uv run macsetup apply --tags apps --profile local/private.toml --yes

uv run macsetup plan --tags files,git,editor --profile local/private.toml
uv run macsetup apply --tags files,git,editor --profile local/private.toml --yes
```

Maintenance caveats:

- Homebrew packages are reconciled by installed/missing state; version pinning is
  intentionally not modeled.
- Homebrew package plans print selected package details, including installed
  names, missing names, cask repair needs, and unmanaged GUI app bundle paths
  that block cask adoption.
- Plan/apply output includes a `Recommended Recovery` section when macsetup can
  identify follow-up commands or manual repair steps.
- Homebrew package groups are applied one missing formula or cask at a time, with
  a visible state line before and after each package install.
- Password/passphrase prompts that arrive without a trailing newline are surfaced
  as `PROMPT` lines and heartbeat status output pauses while input is expected.
- Existing GUI app bundles outside Homebrew are reported as `BLOCKED` instead of
  overwritten. Adopt or remove those manually before letting Homebrew own them.
- Manual steps stay visible as `MANUAL` or `OK` and are not hidden behind brittle
  UI automation.
- Keep `macsetup/defaults/profile.toml` and any `local/*.toml` overlays updated
  when package names, app install sources, or preferences change.
- Run `uv run macsetup graph --tags <area>` after structural edits to verify the
  resource ownership/dependency shape before applying.
- Use [`docs/unapply.md`](docs/unapply.md) for journal-scoped undo and explicit
  `--force` reset workflows. Prefer `--from-run latest --dry-run` before any
  non-dry-run unapply, and pass `--allow-privileged` when reversing cask or
  system-service steps.
- Formula unapply uses local Homebrew install receipts and tap Ruby files to
  sort uninstall targets so selected dependents are removed before selected
  dependencies. It also blocks when a selected formula is still required by an
  installed formula outside the unapply set.

## Profiles And Templates

The packaged defaults live in TOML plus plain template files:

- `macsetup/defaults/profile.toml` defines public package, Git, Python, npm,
  macOS defaults, manual app, manual note, and DNS defaults.
- `macsetup/defaults/templates/` contains generated shell, Git, IPython, and
  Neovim file bodies.
- `profiles/local.example.toml` shows the overlay shape.
- `local/*.toml` is gitignored for private machine/user overlays and loaded
  automatically when present in the repo.

Repo-local `local/*.toml` overlays are applied automatically. Use `--profile`
for additional explicit overlays:

```bash
uv run macsetup plan --profile local/private.toml
uv run macsetup preview --profile local/private.toml
uv run macsetup apply --dry-run --diff --profile local/private.toml
```

Profiles compile into typed Python recipe objects before graph construction. The
engine only works with validated typed data; TOML is the customization boundary,
not an ad hoc script runner.

### Profile Filters

Profiles support replacement plus small filters. For Homebrew formulas and
casks, use:

```toml
[brew]
formula_deny = ["cowsay"]
formula_allow = ["git", "git-delta", "git-lfs"]
formula_reject = ["kerl", "rebar3", "sassc"]

[[brew.formula_extend]]
name = "helix"
tags = ["packages", "editor"]
```

Use the matching `cask_extend`, `cask_deny`, `cask_allow`, and `cask_reject`
keys for GUI casks.

Python, npm, Git settings, macOS defaults, manual app notes, and general manual
notes use the same pattern:

```toml
[python]
version = "3.14"
versions = ["3.11", "3.12", "3.13", "3.14"]
build_jobs = "auto"
build_env = { PYTHON_CONFIGURE_OPTS = "--enable-shared" }
tooling_packages = ["pip", "wheel", "setuptools", "uv", "poetry"]
global_package_deny = ["jupyterlab"]
global_package_extend = ["httpie"]

[javascript]
npm_package_extend = ["eslint"]

[git]
[[git.setting_extend]]
key = "user.name"
value = "Example User"

[[git.setting_extend]]
key = "user.email"
value = "user@example.com"

[macos]
default_deny = ["screenshot-location"]

[[macos.default_extend]]
name = "example-setting"
domain = "com.example.macsetup"
key = "enabled"
value = "true"
value_type = "bool"
tags = ["macos", "defaults"]

[manual]
app_deny = ["Trello"]
note_deny = ["Disable Tips Notifications"]
```

Git identity belongs in a repo-local private overlay such as `local/matt.toml`.
Those files are ignored by Git but loaded by default, so `user.name` and
`user.email` apply on a fresh machine without relying on a hosted account API.

`*_reject` is a guardrail: if a selected final recipe contains a rejected item,
profile loading fails before any plan/apply work starts. Use `*_deny` to remove
something from a particular machine while keeping it allowed in the base profile.

`python.version` remains the selected global pyenv version prefix. Optional
`python.versions` installs additional pyenv version prefixes before the selected
global version. The default manages `3.11`, `3.12`, `3.13`, and `3.14`; pyenv
resolves each prefix to the latest available patch release. `python.tooling_packages`
are installed or upgraded inside every managed pyenv version with that version
selected via `PYENV_VERSION`; the default is `pip`, `wheel`, `setuptools`, `uv`,
and `poetry`.

`python.build_jobs = "auto"` sets `MAKE_OPTS=-j<N>` for `pyenv install`, where
`<N>` is the detected CPU count. Use a positive integer such as `8` to force a
specific job count, or `"default"` to leave python-build's own job handling
untouched. `python.build_env` is merged into the `pyenv install` environment, so
profiles can set `MAKE_OPTS`, `MAKEOPTS`, `PYTHON_CONFIGURE_OPTS`,
`CONFIGURE_OPTS`, `CFLAGS`, `LDFLAGS`, or other python-build overrides.

### macOS Defaults

Use `[[macos.defaults]]` for user-scoped `defaults write` settings that can be
checked, applied, journaled, and restored by `unapply`.

```toml
[[macos.defaults]]
name = "screenshot-location"
domain = "com.apple.screencapture"
key = "location"
value = "~/Desktop/Screenshots"
value_type = "path"
directories = ["~/Desktop/Screenshots"]
tags = ["macos", "defaults", "screenshots"]
```

The packaged defaults create `~/Desktop/Screenshots` and set the global
screenshot location there. On undo, macsetup restores the previous defaults key
value, or deletes the key if it was unset before apply. The managed directory is
removed only when macsetup created it and it is still empty.

### Adding Packages And Apps

Add Homebrew formulae to `[[brew.formulas]]` and casks to `[[brew.casks]]`:

```toml
[[brew.casks]]
name = "firefox"
tags = ["packages", "apps", "browser"]
app_bundles = ["Firefox.app"]
```

`app_bundles` is important for casks. A Homebrew cask is only treated as fully
installed when Homebrew has a cask record and at least one declared app bundle
exists as a valid `.app` bundle in `/Applications` or `~/Applications`. If the
cask record exists but the declared app is missing, macsetup plans a cask
repair with `brew reinstall --cask`. If a matching app path exists without a
Homebrew cask record, macsetup blocks the step so the user can decide whether
to adopt, remove, or leave that app unmanaged.

Some casks stage a separate installer instead of creating the final app bundle.
For those, add `installer_bundles`:

```toml
[[brew.casks]]
name = "windscribe"
tags = ["packages", "apps", "network"]
app_bundles = ["Windscribe.app"]
installer_bundles = ["WindscribeInstaller.app"]
```

For staged installers, macsetup scans `$(brew --prefix)/Caskroom/<cask>/...`
recursively. If `WindscribeInstaller.app` is present there but `Windscribe.app`
is not, macsetup reports the cask as installer-pending and prints a recovery
section with an `open .../WindscribeInstaller.app` command plus the reinstall
fallback. If the cask has not been installed yet, recovery uses `brew install
--cask ...` followed by a Caskroom `find` command to locate and open the staged
installer.

For App Store or vendor-only installs, add a manual app note:

```toml
[[manual.apps]]
name = "Trello"
install_method = "Mac App Store"
url = "https://apps.apple.com/us/app/trello/id1278508951?mt=12"
tags = ["packages", "apps", "manual", "productivity"]
app_bundles = ["Trello.app"]
```

Manual app steps report `OK` when the expected bundle exists and `MANUAL` with
the install URL otherwise.

### Source-Built Packages

Use `[[source_builds.package_extend]]` for tools that should be cloned from Git,
built locally, and exposed on the user's PATH:

```toml
[source_builds]
[[source_builds.package_extend]]
name = "hiproc"
repo = "git@github.com:your-org/hiproc.git"
build_system = "cargo"
ref = "main"
binaries = ["hiproc"]
install_mode = "copy"
install_dir = "~/.local/bin"
```

`build_system = "cargo"` automatically requires Homebrew `git` and `rust`.
`build_system = "zig"` automatically requires `git` and `zig`. Use
`brew_dependencies` for project-specific native deps, `submodules = true` for
recursive checkout, `build_commands` for custom build flags, and `env` for
build-time environment variables.

`install_mode = "copy"` copies declared binaries from `binary_dir` into
`install_dir`; the default `~/.local/bin` PATH block is managed by macsetup.
`install_mode = "path"` leaves binaries in the build output directory and adds a
managed `.zprofile` PATH block for that directory.

### Manual Notes

Use `[[manual.notes]]` for Control Center/System Settings items that are
important on a new machine but not reliable enough to script:

```toml
[[manual.notes]]
name = "Disable AirPlay Receiver"
detail = "System Settings -> search AirPlay Receiver, or General -> AirDrop & Handoff: turn AirPlay Receiver off so Control Center does not listen on TCP port 7000."
tags = ["macos", "manual", "network", "privacy"]
```

Default manual notes currently cover Tips notifications, AirPlay Receiver,
display resolution, mouse speed, battery/performance modes, screen saver and
lock timing, Spotlight result categories, and login/background items. Show them
with:

```bash
uv run macsetup plan --tags macos,manual --profile local/private.toml
```

### Templates And File Ownership

Generated file bodies live under `macsetup/defaults/templates/`.

- Shell profile snippets are inserted as managed blocks.
- Git ignore entries are inserted as a managed block.
- IPython and the Neovim module are managed files with backups on real writes.
- `~/.config/nvim/init.lua` receives only a small managed loader block.

Prefer editing templates for generated file bodies and profiles for package/data
choices. Add Python only when introducing a new kind of resource or deployment
behavior.

## Architecture Guide

The system has four practical layers:

- `macsetup/defaults/profile.toml`: public default recipe data.
- `local/*.toml`: gitignored private overlays.
- `macsetup/defaults/templates/`: generated file bodies.
- `macsetup/*.py`: typed model, profile loader, graph, checks, and deployment
  steps.

Each step declares:

- `requires`: resources that must be present first.
- `provides`: resources made available when the step is present or applied.
- `owns`: resources this step is responsible for managing.

The graph orders selected steps, rejects duplicate providers and owners, and the
apply path skips dependent selected steps when their selected providers are not
available. This makes it reasonable to use the same tool for initial bring-up and
later refreshes.

When adding a new step type:

- Put durable user choices in TOML, not in Python.
- Add a dataclass in `recipes.py` only if the data shape is new.
- Add a `Step` implementation in `steps.py` only if the behavior is new.
- Add tests that cover profile loading, graph shape, and dry-run behavior.
- Prefer `MANUAL` steps for macOS privacy prompts, App Store installs, and GUI
  actions that are not reliably scriptable.

## Safety Model

- `plan` probes current state and does not mutate.
- `preview` probes current state and renders exact unified diffs for managed
  file/block writes without mutating.
- `plan --diff` prints the normal plan plus the same managed file diffs.
- `apply --dry-run --diff` executes probes but skips commands, file writes,
  backups, and journals.
- `apply` captures full command stdout/stderr into the run journal while printing
  summarized live progress lines for important installer output.
- Interrupted non-dry-run apply/unapply runs write a partial journal with
  `status: "interrupted"`, completed step results, and a failed marker for the
  in-flight step. The interrupted step does not claim completed changes.
- Managed file edits are idempotent and update only their own marked blocks
  where possible.
- Homebrew, npm, pip, and pyenv operations are separate steps with package tags.
- Step ordering is graph-driven: each step declares resources it requires,
  provides, and owns; selected steps are topologically sorted before planning or
  applying.
- The graph rejects duplicate step IDs, duplicate providers, and duplicate
  resource ownership before running.
- Sudo-backed service/cache operations are blocked unless `--allow-privileged`
  is set. On `plan` and `preview`, this flag only lets checks classify those
  steps as actionable; it does not mutate the system.
- Do not run the whole tool with `sudo`. macsetup stays under the normal user,
  runs an interactive `sudo -v` preflight before privileged apply work, and
  gives individual sudo commands terminal access so password prompts are visible
  without making Homebrew or generated user files run as root.
- Homebrew bootstrap runs automatically when missing unless `--no-bootstrap` is
  set.
- The installed Xcode developer tools license is checked before Homebrew and
  accepted only when `--allow-privileged` is set.
- Touch ID for `sudo` is managed through `/etc/pam.d/sudo_local`, not by editing
  `/etc/pam.d/sudo` directly. macsetup refuses to automate legacy PAM layouts
  where `sudo` does not include `sudo_local`.
- Homebrew analytics are disabled after bootstrap and checked on every package
  plan/apply run.
- Homebrew share permissions are normalized to mode `755`, and stale PhantomJS
  Homebrew metadata is removed before cask package groups run.
- Zsh completion hygiene removes group/world write bits from the Homebrew
  prefix, `share`, and zsh completion trees. The managed `.zshrc` blocks also
  skip the Homebrew `compinit` block in root shells so `sudo -s` does not run
  root `compaudit` against user-owned Homebrew completion files.
- macsetup deploys the locally patched `kphoen` Oh My Zsh theme into
  `~/.oh-my-zsh/themes/kphoen.zsh-theme`. The managed `.zshrc` loads that theme
  with the `git` plugin, then leaves `PROMPT` and `RPROMPT` owned by the theme
  so right-side git status and exit-code formatting are not overwritten by later
  shell blocks.
- Manual macOS work remains visible as `MANUAL` steps instead of being hidden in
  best-effort automation.

## Modernization Notes

See [docs/modernization.md](docs/modernization.md) for the full mapping.
Important changes:

- `kerl`, `rebar3`, and the Erlang build/install section are omitted.
- Homebrew prefix is detected at runtime for Apple Silicon and Intel.
- Python uses unpinned pyenv series prefixes `3.11`, `3.12`, `3.13`, and `3.14`
  in the recipe; `3.14` is selected globally. Each managed pyenv version gets
  baseline tooling packages: `pip`, `wheel`, `setuptools`, `uv`, and `poetry`.
- `llama.cpp` is included as a modern Homebrew formula for local LLM inference.
- `openssh` is installed through Homebrew so SSH and target-side rsync pull
  workflows use the managed toolchain on new machines.
- `node` replaces the old `npm` formula alias.
- `openssl@3` replaces the old unversioned OpenSSL formula.
- `universal-ctags` replaces legacy `ctags`.
- Git uses `git-delta` for pager/diff rendering, `zdiff3` conflict markers,
  Git LFS filters, and repo-local private identity overlays by default.
- `sassc` is omitted because it is deprecated and no current local components
  depend on it.
- Mac GUI apps are installed and updated through Homebrew casks, including
  1Password, Claude Desktop, Codex Desktop, Cursor, Discord, Firefox, TigerVNC
  Viewer, TG Pro, TradingView, Transmission, VLC, VS Code, Windscribe, and
  WezTerm.
- Trello is represented as a manual Mac App Store install note because a stable
  Homebrew cask was not available.
- Existing app bundles outside Homebrew are reported as blocked rather than
  overwritten by cask installs.
- Neovim uses a `lazy.nvim` bootstrap module with Mason/LSP, nvim-cmp/LuaSnip,
  Telescope, and Trouble instead of the old packer/COQ flow.
- Managed pyenv shell init commands use `--no-rehash`.
- Setup ownership and ordering are modeled with explicit resource graph metadata.
- The DNS focus blocklist is enabled by default through the managed dnsmasq
  snippet. Use `--disable-dns-blocklist` only for an exceptional run.

## References

- Homebrew installation and shellenv guidance: <https://docs.brew.sh/Installation.html>
- pyenv zsh setup and build dependency guidance: <https://github.com/pyenv/pyenv>
- Oh My Zsh unattended/manual install behavior: <https://github.com/ohmyzsh/ohmyzsh>
- lazy.nvim installation: <https://lazy.folke.io/installation>
- Python releases: <https://www.python.org/downloads/>
