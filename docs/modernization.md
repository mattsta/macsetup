# Modernization Notes

This project packages a modern, repeatable macOS developer-workstation setup.
The defaults are based on long-running command-line setup practices, with
outdated or machine-specific assumptions removed.

## Included Practices

| Practice                                                       | Implementation                                                                                                                                                              |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Install Homebrew and add it to shell startup                   | `HomebrewInstallStep` plus managed `~/.zprofile` shellenv block                                                                                                             |
| Repair Xcode.app developer-directory selection                 | `XcodeDeveloperDirectoryStep` selects `/Applications/Xcode.app/Contents/Developer` when Xcode.app is installed but `xcode-select` still points at Command Line Tools        |
| Avoid Xcode license prompts during tool install                | `XcodeLicenseStep` checks `xcodebuild -license check` and can accept with `--allow-privileged`                                                                              |
| Ensure the Xcode Metal toolchain component is available        | `XcodeMetalToolchainStep` checks `xcrun --find metal` and runs `xcodebuild -downloadComponent MetalToolchain` when missing                                                  |
| Enable Touch ID for sudo in Terminal                           | `SudoTouchIdStep` manages `/etc/pam.d/sudo_local` and refuses direct edits to `/etc/pam.d/sudo`                                                                             |
| Disable Homebrew analytics                                     | `HomebrewAnalyticsStep` plus immediate post-install `brew analytics off`                                                                                                    |
| Install modern CLI tools                                       | Typed Homebrew formula recipe loaded from `macsetup/defaults/profile.toml`                                                                                                  |
| Fix zsh completion permissions                                 | `ZshCompletionPermissionsStep`, scoped to Homebrew prefix/share parents and zsh completion directories; root shells skip the managed Homebrew `compinit` block              |
| Configure pyenv shell helpers                                  | Managed `~/.zprofile` and `~/.zshrc` blocks                                                                                                                                 |
| Install `uglify-js` globally                                   | `NpmGlobalPackagesStep`                                                                                                                                                     |
| Install preferred Python and baseline per-version tooling      | `PyenvPythonStep` installs each managed pyenv version and upgrades `pip`, `wheel`, `setuptools`, `uv`, and `poetry` inside it                                               |
| Configure Git aliases, pager, push tags, and optional identity | `GitConfigStep` entries loaded from profiles; private identity belongs in a local overlay                                                                                   |
| Install global Python utility packages                         | `PythonPackagesStep`                                                                                                                                                        |
| Add IPython startup imports                                    | Managed `~/.ipython/profile_default/startup/00-macsetup.py`                                                                                                                 |
| Configure dnsmasq resolver                                     | Managed dnsmasq config snippets                                                                                                                                             |
| Set non-Desktop screenshot location                            | `MacDefaultsStep` writes `com.apple.screencapture location` after `ManagedDirectoryStep` creates `~/Desktop/Screenshots`                                                    |
| Install/configure Neovim                                       | Managed Lua config with lazy.nvim, Mason/LSP, nvim-cmp/LuaSnip, Telescope, and Trouble                                                                                      |
| Install oh-my-zsh and shell helpers                            | `OhMyZshStep`, a managed patched `kphoen` theme file, and managed `~/.zshrc` blocks; the theme owns prompt/git status formatting                                            |
| Use powerline-style terminal fonts                             | Homebrew font casks for Meslo and Liberation Nerd Fonts                                                                                                                     |
| Install common Mac GUI apps                                    | Homebrew casks for 1Password, Claude Desktop, Codex Desktop, Cursor, Discord, Firefox, TigerVNC Viewer, TG Pro, TradingView, Transmission, VS Code, Windscribe, and WezTerm |
| Build Git-hosted local tools                                   | `SourceBuildStep` clones, updates, builds, and exposes configured cargo/zig/custom source packages                                                                          |
| Track GUI apps without reliable automation                     | Manual app steps for Mac App Store or vendor-only installs such as Trello                                                                                                   |
| Pre-trigger macOS privacy prompts                              | Manual step retained in plan output                                                                                                                                         |
| Review Control Center/System Settings preferences              | Profile-driven manual notes for notifications, AirPlay Receiver, display, mouse, battery, screen lock, Spotlight, and login/background items                                |
| Browser DNS-over-HTTPS review                                  | Manual step retained in plan output                                                                                                                                         |

## Modernized

| Older assumption                             | Current implementation                                                                         |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Hard-coded `/opt/homebrew`                   | Runtime `brew --prefix`; shellenv block handles `/opt/homebrew` and `/usr/local`               |
| `npm` formula                                | `node`, which provides npm                                                                     |
| `openssl` formula                            | `openssl@3`                                                                                    |
| `ctags`                                      | `universal-ctags`                                                                              |
| `sassc`                                      | Removed because Homebrew marks it deprecated and current projects no longer depend on it       |
| `diff-so-fancy` pager                        | Removed; the default recipe uses `git-delta` for diff/show paging and `bat` for git log paging |
| COQ/packer Neovim setup                      | Removed; the default recipe uses lazy.nvim, Mason, nvim-cmp, LuaSnip, Telescope, and Trouble   |
| packer.nvim                                  | lazy.nvim bootstrap                                                                            |
| Editing the Homebrew dnsmasq service formula | `conf-dir` plus managed `dnsmasq.d` snippet                                                    |
| Manual clone of `powerline/fonts`            | Homebrew font casks                                                                            |
| Python `3.12.0`                              | Unpinned pyenv series prefixes `3.11`, `3.12`, `3.13`, and `3.14`; `3.14` selected globally    |
| Minimal pyenv build dependencies             | Adds readline, sqlite, xz, bzip2, libffi, pkg-config, and tcl-tk                               |
| Implicit pyenv build concurrency             | Sets `MAKE_OPTS=-j<N>` for pyenv installs by default, with profile overrides                   |
| Tooling only in one active Python            | Managed pyenv versions each get `pip`, `wheel`, `setuptools`, `uv`, and `poetry`               |
| Local LLM tooling                            | Adds Homebrew `llama.cpp` formula                                                              |
| Rehashing during shell startup               | Every managed `pyenv init` command includes `--no-rehash`                                      |

## Retained With Caution

- The dnsmasq focus blocklist is enabled by default through the managed snippet.
  Use `--disable-dns-blocklist` only for an exceptional run.
- Historical Python/scipy/numba/LLVM build workarounds are obsolete for the
  default recipe and are not automated.
- Classic Vim has no managed plugin stack; setup remains
  Neovim-first and keeps the shell `vim=nvim` alias rather than creating a
  separate Vim plugin stack.
- GUI apps are managed with Homebrew casks rather than direct vendor download
  scripts so installs remain graph-owned and repeatable.
- If a GUI app bundle already exists outside Homebrew, macsetup blocks that cask
  step instead of overwriting the existing app.
- Trello is represented as a manual Mac App Store app because there is no stable
  Homebrew cask in the default recipe. AppleScript-driven App Store installs are
  intentionally not automated because account prompts and confirmations are not
  reliable setup primitives.
- Packaged defaults do not hard-code private Git identity. Gitignored
  repo-local `local/*.toml` overlays are auto-loaded, so `user.name`,
  `user.email`, and other machine-local choices can travel with the local
  checkout without relying on an external account API.

## Omitted

- `kerl`
- `rebar3`
- Manual Erlang build/install commands

## Deployment Practices

- Re-running `apply` should not duplicate shell blocks or managed config.
- Each setup step declares graph resources it requires, provides, and owns.
- Selected steps are topologically sorted before `plan` and `apply`.
- TOML profiles compile into the typed recipe model before graph construction.
- Package and Git setting profiles support extend, deny, allow, and reject
  filters so local policy can customize the base recipe without editing Python.
- Source-built package profiles can infer Homebrew build dependencies for cargo
  and zig projects, while still allowing explicit project-specific formula deps.
- Manual app notes are profile-driven and report `OK` when the expected app
  bundle is already installed.
- Manual notes are profile-driven for System Settings items that should be
  checked during new-machine bring-up but are not reliable enough to automate.
- Homebrew `openssh` is part of the default network/shell package set for
  consistent SSH and target-side rsync pull behavior.
- Generated dotfile bodies live in packaged templates rather than embedded code.
- Duplicate providers and duplicate owners are rejected before execution.
- File writes are atomic.
- Existing files get timestamped backups before real writes.
- Dry-run apply skips writes, backups, and journals.
- Privileged service/cache commands are blocked without `--allow-privileged`.
- Homebrew bootstrap runs automatically when missing unless `--no-bootstrap` is
  set.
- Xcode developer tools license acceptance is ordered before Homebrew and
  blocked without `--allow-privileged`.
- Touch ID for `sudo` is managed through `/etc/pam.d/sudo_local` only; direct
  edits to the main sudo PAM file are intentionally not automated.
- Homebrew analytics are always turned off after bootstrap and audited on
  existing installs.
