# mrsync

`mrsync` is a thin rsync wrapper for day-to-day copies. It keeps machine-wide
and machine-local ignore rules in one place while preserving normal rsync
behavior.

Install it with the sync/file setup tags:

```bash
uv run macsetup apply --tags sync --profile local/private.toml --yes
```

That installs:

- `~/.local/bin/mrsync`: launcher for `uv --directory <checkout> run mrsync`.
- `~/.config/macsetup/rsync/global.filter`: managed common cache/build rules.
- `~/.config/macsetup/rsync/local.filter.example`: example for private rules.
- `~/.zshrc`: a managed `macsetup:mrsync` block containing the `mrsync()`
  shell function.
- `~/.zprofile`: a managed `macsetup:local-bin-path` block for `~/.local/bin`.

Preview the exact writes first:

```bash
uv run macsetup preview --tags sync --profile local/private.toml
```

The shell edits use macsetup's marker-block file system. If the marker exists,
the block is replaced in place; if it does not exist, the block is appended once.
The TOML profile layer selects data and packages, while these shell blocks are
owned by typed Python steps so the update behavior is idempotent and testable.

Create private machine-local rules in:

```text
~/.config/macsetup/rsync/local.filter
```

That file is intentionally not managed by macsetup.

## Filter Order

`mrsync` builds this rsync command shape:

```text
rsync --filter='merge ~/.config/macsetup/rsync/global.filter' --filter='merge ~/.config/macsetup/rsync/local.filter' -F -F <your rsync args>
```

The first `-F` enables per-directory `.rsync-filter` files. The second `-F`
excludes the `.rsync-filter` files themselves from the transfer.

## Managed Defaults

The managed global filter is intentionally for generated state that is noisy,
large, or per-machine:

- Python environments and caches such as `.venv/`, `__pycache__/`,
  `.uv-cache/`, `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`, and
  `.hypothesis/`.
- JavaScript dependency/build caches such as `node_modules/`, `.pnpm-store/`,
  `.parcel-cache/`, `.turbo/`, `.next/`, `.nuxt/`, `.svelte-kit/`, and `.vite/`.
- Build outputs from common toolchains such as `build/`, `dist/`, `target/`,
  `CMakeFiles/`, `.gradle/`, `.build/`, `.terraform/`, `bazel-*/`,
  `buck-out/`, `_build/`, and `DerivedData/`.
- Local scratch and sidecar files such as `.cache/`, `.tmp/`, `.DS_Store`,
  AppleDouble files, coverage output, and dSYM bundles.

Rules like `- node_modules/` and `- .venv/` are recursive leaf-name matches in
rsync filter syntax, so they apply no matter how deeply those directories are
nested. Keep project-specific heavy directories in `local.filter` or a repo
`.rsync-filter` instead of adding private names to the managed template.

## Audit

Before copying, inspect the active filter hierarchy:

```bash
mrsync --audit -a source/ dest/
```

The audit prints:

- Config directory.
- Whether global/local filters are enabled.
- Whether project `.rsync-filter` support is enabled.
- Each filter file, whether it exists, and its active non-comment rules.
- The final rsync command.

## Escape Hatches

Run plain rsync directly:

```bash
command rsync -a source/ dest/
```

Disable managed global/local filters for one run:

```bash
mrsync --no-global-filters -a source/ dest/
```

Disable project `.rsync-filter` support:

```bash
mrsync --no-project-filters -a source/ dest/
```

Use an additional required filter file:

```bash
mrsync --filter-file ./copy.filter -a source/ dest/
```

Print the command without running it:

```bash
mrsync --print-command -a source/ dest/
```
