# Mac-To-Mac Transfer Helpers

This repo includes small wrappers for fast Mac-to-Mac copying when the only
network path is Wi-Fi.

The primary interface is the `sync` branch of the `macsetup` CLI:

- `macsetup sync auto-server`: receive either `ditto` or tar on separate LAN-only ports.
- `macsetup sync auto-sync`: choose `ditto`, tar, or rsync from the requested inputs.
- `macsetup sync ditto-recv`: receive a `ditto` archive stream on the new Mac.
- `macsetup sync ditto-send`: send a `ditto` archive stream from the old Mac.
- `macsetup sync tar-recv`: receive a metadata-preserving tar stream on the new Mac.
- `macsetup sync tar-send`: send a metadata-preserving tar stream with excludes.
- `macsetup sync rsync-repos`: run a tuned rsync incremental sync for repos.
- `macsetup sync rsync-from`: pull from an old/source machine over SSH.
- `macsetup sync rsync-daemon-server`: run a temporary authenticated rsync daemon.
- `macsetup sync rsync-daemon-sync`: sync to that daemon over direct TCP.

The `scripts/*.sh` files are thin launchers for the same Python implementation.

The default adaptive flow is:

1. `auto-sync` uses tar for initial copies when managed generated-state
   excludes, local excludes, or explicit excludes are active.
2. `auto-sync` uses `ditto` for an initial whole-tree copy only when no excludes
   are active, for example with `--no-default-excludes`.
3. `auto-sync` uses local/SMB rsync for catch-up sync when `--rsync-dest` is present.
4. `rsync-from` runs from the new machine and pulls from the old machine over SSH.
5. `auto-sync --method rsync-daemon` uses direct TCP rsync daemon mode for
   catch-up sync without SSH or SMB.
6. Use Migration Assistant or Time Machine for full whole-account/system
   transfers when system settings and app state matter.

## Raw Copy Safety

Do not use these helpers to raw-replace a full macOS home directory or
`~/Library` on another Mac. `~/Library` contains live and machine-local state:
keychains, app containers, group containers, privacy/TCC databases, sync metadata,
browser and mail databases, caches, sockets, and per-install app state. `sudo`
does not make that copy semantically safe, and macOS privacy controls can still
deny access unless the terminal app has Full Disk Access.

Safe default:

- Use Migration Assistant, Setup Assistant, or Time Machine for whole-account
  migration, Keychain state, app state, and system settings.
- Use macsetup sync for selected user data trees, source repos, media, and
  reproducible config.
- Do not raw-copy `~/Library/Keychains`, `~/Library/Containers`,
  `~/Library/Group Containers`, `~/Library/Mail`, browser profiles, Photos
  libraries, Messages data, or other app databases unless you are following an
  app-specific closed-app restore procedure.
- For broad home-root data syncs, exclude `Library/`, app-owned database
  packages outside Library, and preview with `--dry-run --itemize` before any
  live rsync.

## Network Safety

Do not use TCP port `7000` for this copy stream. Recent macOS versions commonly
use `ControlCenter` on port `7000` for AirPlay Receiver. These scripts default
to port `17000`.

The plaintext stream scripts refuse public and localhost addresses by default:

- Receiver scripts bind only to a detected or explicit RFC1918/link-local IPv4
  address, such as `10.x.x.x`, `172.16.x.x` through `172.31.x.x`,
  `192.168.x.x`, or `169.254.x.x`.
- Receiver scripts do not bind to `0.0.0.0`, `*`, or `127.0.0.1` unless forced.
- Sender scripts require the destination host to resolve to a private/link-local
  IPv4 address.

If the Mac has more than one private address, pass the exact LAN address with
`--bind` on the receiver and use that same address as `--host` on the sender.
There is an explicit `--allow-unsafe-network` escape hatch for unusual lab
setups, but it prints a warning and pauses before opening the stream.

To check a port on the receiving Mac:

```bash
lsof -nP -iTCP:17000 -sTCP:LISTEN
```

## First Bring-Up Runbook

Use this shape when the new Mac has only the minimal checkout and you want an
initial copy of selected user trees without generated caches/build products.
Each archive stream handles one source tree; rerun the receiver for each major
tree you want to copy. `auto-server`, `ditto-recv`, and `tar-recv` stay running
after each completed transfer; stop the receiver with Ctrl-C when you are done.
Use `--once` only for scripted one-shot transfers.

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

The default generated-state excludes are active, so this selects tar and sends
to port `17001` while `auto-server` listens on both ports. Source paths are sent
with their parent directory by default. Sending `$HOME/path-to-source-tree` into
`--dest "$HOME"` creates `$HOME/path-to-source-tree` on the new Mac.
Add machine-local heavy directories to gitignored `local/rsync-excludes.txt`;
the tar and rsync wrappers pick it up automatically when it exists.
For source-code trees where ACLs, xattrs, and resource-fork metadata are not
important, add `--no-mac-metadata` on both archive sides for a leaner tar path.
Leave the receiver running and repeat the sender command for the next selected
source tree.

Tar-mode archive creation is driven by a generated file manifest. The manifest
skips Unix socket files because sockets are live process endpoints and cannot be
meaningfully copied to another machine.

For a manual uplift, copy selected top-level data folders instead of the whole
home. Keep the receiver rooted at the target account's home directory:

On the new Mac:

```bash
cd /path/to/macsetup
uv run macsetup preview --tags sync
uv run macsetup apply --tags sync --yes
uv run macsetup sync auto-server --bind <new-mac-lan-ip> --port 17000 --dest "$HOME" --no-sudo
```

On the old Mac:

```bash
cd /path/to/macsetup
uv run macsetup sync auto-sync --host <new-mac-lan-ip> --port 17000 --source "$HOME/Documents" --no-sudo
uv run macsetup sync auto-sync --host <new-mac-lan-ip> --port 17000 --source "$HOME/Desktop" --no-sudo
uv run macsetup sync auto-sync --host <new-mac-lan-ip> --port 17000 --source "$HOME/Downloads" --no-sudo
```

For a broad home-root data pass, exclude `Library/`, app database packages, and
other machine-local state. Run the new Mac command from a temporary admin account
if the destination account is active:

```bash
uv run macsetup sync rsync-from --host <old-mac-lan-ip> --user <old-user> --source /Users/<old-user>/ --dest /Users/<old-user>/ --exclude Library/ --exclude .Trash/ --exclude .Spotlight-V100/ --exclude .fseventsd/ --exclude 'Pictures/Photos Library.photoslibrary/' --exclude 'Pictures/Photo Booth Library/' --dry-run --itemize
```

Remove `--dry-run --itemize` only after the preview shows exactly the intended
data changes.

For repeated catch-up after the initial archive copy, use either an SMB/local
mount:

```bash
uv run macsetup sync auto-sync --rsync-dest /path/to/new-mac-mounted-tree/ --dry-run --itemize "$HOME/path-to-source-tree/"
```

or a temporary rsync daemon on the new Mac:

```bash
uv run macsetup sync rsync-daemon-server --bind <new-mac-lan-ip> --port 18730 --dest "$HOME"
```

Then on the existing Mac:

```bash
MACSETUP_RSYNC_PASSWORD='<printed-password>' uv run macsetup sync auto-sync --method rsync-daemon --host <new-mac-lan-ip> --rsync-daemon-port 18730 --rsync-module-path path-to-source-tree --dry-run --itemize --source "$HOME/path-to-source-tree/"
```

For the final catch-up, close applications on the old machine and initiate the
copy from the new machine. The old machine must stay awake with Remote Login/SSH
enabled:

```bash
uv run macsetup sync rsync-from --host <old-mac-lan-ip> --user <old-user> --source /Users/<old-user>/path-to-source-tree/ --dest "$HOME/path-to-source-tree/" --dry-run --itemize
```

Remove `--dry-run --itemize` when the catch-up preview looks correct.

## Initial Bulk Copy With Ditto

Install `pv` if you want throughput and ETA output:

```bash
brew install pv
```

The scripts still work without `pv`; they just show less progress.
Archive commands also print explicit state lines for precheck, method
selection, listener wait, accepted connection, pipeline commands, per-transfer
exit status, and readiness for the next transfer. The receiver emits a wait
heartbeat every 30 seconds so an idle terminal still shows that it is listening.

The sender does not pre-scan the source tree for size by default. That avoids a
long silent `du` walk on very large trees. `pv` still shows byte count, elapsed
time, and current/average throughput. If you want an ETA and accept the pre-scan
cost, add `--estimate-size`.

On the new Mac:

```bash
cd /path/to/macsetup
uv run macsetup sync auto-server --bind 192.168.1.20 --port 17000 --dest /path/to/destination-root
```

On the old Mac:

```bash
cd /path/to/macsetup
uv run macsetup sync auto-sync --no-default-excludes --port 17000 --host 192.168.1.20 --source /path/to/source-tree
```

Because excludes are disabled, `auto-sync` chooses `ditto`. By default, the
sender uses `ditto --keepParent`, so sending `/path/to/source-tree` into
destination `/path/to` creates `/path/to/source-tree`.
If you want to copy the contents of a directory instead of the directory itself,
use:

```bash
uv run macsetup sync auto-sync --contents --host 192.168.1.20 --source /path/to/source-tree
```

Both scripts use `sudo` by default so ownership, modes, ACLs, extended
attributes, resource forks, and other macOS metadata have the best chance of
surviving the trip. They refresh sudo credentials before opening the stream so
the password prompt does not collide with archive data.

For user-owned trees where root ownership is not needed:

```bash
uv run macsetup sync auto-server --no-sudo --dest /path/to/destination-root
uv run macsetup sync auto-sync --no-sudo --host 192.168.1.20 --source /path/to/source-tree
```

Security note: this is plaintext TCP with no authentication. Use it only on a
trusted LAN.

## Initial Bulk Copy With Excludes

`ditto` does not have rsync-style exclude patterns. That is intentional for this
workflow: `ditto` is best for a whole source tree. If you need to skip very large
directories during the initial copy, use the tar stream scripts or run `ditto`
on narrower source trees that already omit the unwanted content.

By default, tar-mode initial copies use the managed generated-state excludes
from `macsetup/defaults/templates/rsync_global.filter`. These include recursive
cache/build patterns such as `.venv/`, `__pycache__/`, `node_modules/`,
`.uv-cache/`, `.ruff_cache/`, `.hypothesis/`, `.tmp/`, `build/`, `dist/`,
`target/`, `CMakeFiles/`, `.gradle/`, and frontend cache directories. Pass
`--no-default-excludes` only for a narrow source tree where you intentionally
want generated cache/build output copied. Do not use `--no-default-excludes` as
a whole-home migration mode.

Start the same adaptive receiver on the new Mac:

```bash
uv run macsetup sync auto-server --bind 192.168.1.20 --port 17000 --dest /path/to/destination-root
```

Then pass excludes on the old Mac. Because excludes are present, `auto-sync`
chooses tar and connects to the tar port, which defaults to `--port + 1`:

```bash
uv run macsetup sync auto-sync --port 17000 --host 192.168.1.20 --source /path/to/source-tree --exclude 'large-cache/'
```

For machine-local or project-specific excludes, use `local/rsync-excludes.txt`.
That file is ignored by Git and can be reused by both tar and rsync wrappers:

```bash
uv run macsetup sync auto-sync --host 192.168.1.20 --source /path/to/source-tree --exclude-from local/rsync-excludes.txt
```

You can also add as many one-off excludes as needed:

```bash
uv run macsetup sync auto-sync --host 192.168.1.20 --source /path/to/source-tree --exclude 'large-cache/' --exclude 'scratch-output/' --exclude-from /path/to/extra-excludes.txt
```

Leaf directory excludes are expanded for tar so a rule like `node_modules/` or
`.venv/` applies at the source root and beneath nested project directories.
Path-specific excludes are still allowed when you need narrower behavior.

With no explicit `--exclude`, the managed defaults still count as active
excludes, so `auto-sync` selects tar:

```bash
uv run macsetup sync auto-sync --host 192.168.1.20 --source /path/to/source-tree
```

## Why Ditto Instead Of Tar

`tar` can work, but macOS metadata requires the right flags and extraction mode.
`ditto` is the safer default for Mac-to-Mac copies because it is macOS-native and
preserves resource forks, extended attributes, ACLs, modes, mtimes, owner/group,
and hard links by default where permissions allow.

Use tar when source-side excludes matter. The included tar scripts use a
null-delimited `find` manifest that skips Unix sockets, plus `--mac-metadata`,
`--acls`, and `--xattrs` when metadata preservation is enabled, so they keep the
important macOS metadata while still allowing `--exclude` and `--exclude-from`.

The closest tar shape is:

```bash
cd /path/to
sudo find parent-directory ! -type s -print0 | sudo tar --mac-metadata --acls --xattrs -cpf - --null --no-recursion -T - | pv -rabt | nc -4 192.168.1.20 17000
```

and on the receiver:

```bash
nc -4 -l 192.168.1.20 17000 | pv -rabt | sudo tar --mac-metadata --acls --xattrs -xpf - -C /path/to/destination-root
```

That is reasonable, but `ditto` has fewer Mac-specific footguns.

## Incremental Repo Sync With Rsync

After the initial archive copy, use rsync for repeated catch-up runs by passing
`--rsync-dest`. This is best when the new Mac is mounted locally, for example
over SMB:

```bash
uv run macsetup sync auto-sync --rsync-dest /path/to/dest/ /path/to/source/
```

Dry-run first when changing excludes or destination paths:

```bash
uv run macsetup sync auto-sync --rsync-dest /path/to/dest/ --dry-run --itemize /path/to/source/
```

The wrapper uses these high-level defaults:

- `-a`: archive mode; recursive copy, symlinks, modes, mtimes, owner/group, and
  device/special files where allowed.
- `--acls`, `--xattrs`, `--fileflags`, `--crtimes`: preserve macOS metadata when
  the installed rsync supports those flags.
- `--whole-file`: send changed files whole instead of doing rsync's rolling
  delta algorithm. This is usually faster for LAN/SMB/local-volume transfers.
- `--no-compress`: avoid wasting CPU compressing data on a local network.
- `--partial`: keep partial files if a run is interrupted.
- `--delete --delete-after`: mirror removals, but perform deletes after the
  transfer pass.
- `--info=progress2`: show whole-transfer progress when supported.

Important: the wrapper does not use `--checksum`. Rsync's default quick check is
already size plus modification time. `--checksum` would force extra full-file
reads and is usually the wrong choice for very large trees.

Default generated-state excludes come from the same managed filter used by
`mrsync` and tar-mode initial copies. They include:

```text
.venv/
__pycache__/
node_modules/
.uv-cache/
.ruff_cache/
.hypothesis/
.tmp/
CMakeFiles/
build/
dist/
target/
```

Add one-off excludes with:

```bash
uv run macsetup sync auto-sync --rsync-dest /path/to/dest/ --exclude '.custom-cache/' /path/to/source/
```

Put machine-local or project-specific excludes in `local/rsync-excludes.txt`.
That file is ignored by Git:

```bash
uv run macsetup sync auto-sync --rsync-dest /path/to/dest/ --exclude-from local/rsync-excludes.txt /path/to/source/
```

Disable the built-in excludes with:

```bash
uv run macsetup sync auto-sync --rsync-dest /path/to/dest/ --no-default-excludes /path/to/source/
```

Use that only for narrow trees. It does not make `~/Library` safe to copy.

Normal rsync trailing-slash semantics still apply:

- `/path/to/source/` copies the contents of `source` into the destination.
- `/path/to/source` copies the `source` directory itself into the destination
  when the destination is a directory.

## Final Target-Side Rsync Pull

For the final pass of a machine move, the safest shape is to close all
applications on the old machine, including Terminal, keep that machine awake,
and run the catch-up from the new machine. Enable Remote Login/SSH on the old
machine first, then run:

```bash
uv run macsetup sync rsync-from --host 192.168.1.10 --user matt --source /Users/matt/path-to-source-tree/ --dest "$HOME/path-to-source-tree/" --dry-run --itemize
```

The command uses the same tuned rsync defaults and generated-state excludes as
`rsync-repos`: archive mode, whole-file LAN copies, no compression, partial
files, delete-after mirroring, progress output when supported, and optional
`local/rsync-excludes.txt`. Use it for selected trees or for a home-root pass
that explicitly excludes `Library/` and app-owned database packages. Remove
`--dry-run --itemize` only after the preview shows the expected changes.

Use `--ssh-port` and repeated `--ssh-option` values for custom SSH setups:

```bash
uv run macsetup sync rsync-from --host 192.168.1.10 --user matt --ssh-port 2222 --ssh-option BatchMode=yes --source /Users/matt/path-to-source-tree/ --dest "$HOME/path-to-source-tree/" --dry-run --itemize
```

## Direct Rsync Daemon Catch-Up

For rsync catch-up without SSH and without an SMB mount, run a temporary
authenticated rsync daemon on the new Mac. This is direct TCP rsync, not `nc`;
the rsync protocol is bidirectional, so it cannot be safely wrapped as a simple
one-way pipe.

On the new Mac:

```bash
uv run macsetup sync rsync-daemon-server --bind 192.168.1.20 --port 18730 --dest /path/to/destination-root
```

The server generates a one-time password, writes a temporary `rsyncd.conf` and
secrets file under `~/.macsetup/sync/rsyncd`, binds only to the private LAN
address, scopes `hosts allow` to the bind interface subnet, and runs:

```text
rsync --daemon --no-detach --config <generated-config> --address <bind-ip> --port <port>
```

On the old Mac, provide the printed password via environment variable or a
local password file:

```bash
MACSETUP_RSYNC_PASSWORD='<printed-password>' uv run macsetup sync auto-sync --method rsync-daemon --host 192.168.1.20 --rsync-daemon-port 18730 --source /path/to/source/
```

Equivalently, use the explicit daemon client command:

```bash
uv run macsetup sync rsync-daemon-sync --host 192.168.1.20 --port 18730 --password-file local/rsync-daemon-password.txt /path/to/source/
```

The daemon client uses the same tuned rsync options as `rsync-repos`, including
`--whole-file`, `--no-compress`, generated-file excludes, optional
`local/rsync-excludes.txt`, and `--info=progress2` when supported. `pv` is not
inserted into daemon-mode rsync because the daemon protocol is bidirectional;
rsync's own progress output is the correct progress layer.

## Performance Notes

Archive mode has lower orchestration overhead than SSH rsync, but tar with
metadata preservation and many excludes is still a single-process filesystem
walk. For source-code trees, the fastest safe variants to try are:

- Add `--no-mac-metadata` when ACLs, xattrs, and resource forks are irrelevant.
- Use `--no-sudo` for user-owned trees to avoid privileged extraction overhead.
- Use target-side `rsync-from` for the final catch-up when the old machine has
  SSH enabled and all applications have been closed.
- Use direct rsync daemon catch-up for repeated syncs; it avoids SSH and SMB
  while preserving rsync's quick-check behavior.
- Use `ditto` with `--no-default-excludes` only when you truly want a whole-tree
  copy of a narrow non-`Library`, non-app-database tree or you have temporarily
  moved very large generated directories out of the source path.
- Keep `pv` enabled for visibility, but leave `--estimate-size` off unless an
  ETA is worth the extra full-tree pre-scan.
- Unix socket files are skipped in tar mode. They are live local process
  endpoints, not durable data, and pax/tar archives cannot represent them.

## References

- Migration Assistant transfers user accounts, apps, documents, and settings:
  <https://support.apple.com/en-gb/102613>
- Time Machine backups can be restored through Migration Assistant:
  <https://support.apple.com/en-us/HT203981>
- macOS Full Disk Access is separate from Unix permissions and sudo:
  <https://support.apple.com/en-asia/guide/security/secddd1d86a6/web>
- Apple keychain copying guidance and Local Items/iCloud Keychain limits:
  <https://support.apple.com/en-by/guide/keychain-access/kyca1121/mac>
