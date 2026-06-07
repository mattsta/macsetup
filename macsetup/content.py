from __future__ import annotations

from collections.abc import Sequence
from importlib.resources import files


def _template(name: str) -> str:
    return (
        files("macsetup.defaults")
        .joinpath("templates", name)
        .read_text(encoding="utf-8")
    )


def _template_blocks(name: str) -> tuple[tuple[str, str], ...]:
    marker_prefix = "# macsetup-template-block:"
    blocks: list[tuple[str, str]] = []
    current_name: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        if current_name is None:
            return
        blocks.append((current_name, "\n".join(current_lines).strip("\n")))
        current_lines = []

    for line in _template(name).splitlines():
        if line.startswith(marker_prefix):
            flush()
            current_name = line.removeprefix(marker_prefix).strip()
            if not current_name:
                raise ValueError(f"empty template block name in {name}")
            continue
        if current_name is None:
            if line.strip():
                raise ValueError(f"content before first template block in {name}")
            continue
        current_lines.append(line)
    flush()
    if not blocks:
        raise ValueError(f"no template blocks found in {name}")
    return tuple(blocks)


def _join_blocks(blocks: Sequence[tuple[str, str]]) -> str:
    return "\n\n".join(body for _, body in blocks if body.strip())


ZPROFILE_BLOCKS = _template_blocks("zprofile")
ZPROFILE_BLOCK = _join_blocks(ZPROFILE_BLOCKS)
ZSHRC_BLOCKS = _template_blocks("zshrc")
ZSHRC_BLOCK = _join_blocks(ZSHRC_BLOCKS)
INPUTRC_BLOCKS = _template_blocks("inputrc")
INPUTRC_BLOCK = _join_blocks(INPUTRC_BLOCKS)
KPHOEN_ZSH_THEME = _template("kphoen.zsh-theme")
GLOBAL_GITIGNORE = _template("gitignore_global")
RSYNC_GLOBAL_FILTER = _template("rsync_global.filter")
RSYNC_LOCAL_FILTER_EXAMPLE = _template("rsync_local.filter.example")
IPYTHON_STARTUP = _template("ipython_startup.py")
NVIM_INIT_BLOCK = _template("nvim_init.lua")
NVIM_MACSETUP_LUA = _template("nvim_macsetup.lua")
GIT_LOG_PAGER_C = _template("git-log-pager.c")

LOCAL_BIN_PATH_BLOCK = """
if [[ -d "$HOME/.local/bin" ]]; then
  export PATH="$HOME/.local/bin:$PATH"
fi
""".strip()

MRSYNC_SHELL_BLOCK = """
function mrsync() {
  "$HOME/.local/bin/mrsync" "$@"
}
""".strip()


def dnsmasq_config(
    *,
    servers: tuple[str, ...],
    passthrough_domains: tuple[str, ...],
    blocked_domains: tuple[str, ...],
    enable_blocklist: bool,
) -> str:
    lines = [
        "all-servers",
        *(f"server={server}" for server in servers),
        "",
        "# Domains with trailing # use the system default resolver instead.",
        *(f"server=/{domain}/#" for domain in passthrough_domains),
    ]
    if enable_blocklist:
        lines.extend(["", "# Domains below intentionally return NXDOMAIN."])
        lines.extend(f"server=/{domain}/" for domain in blocked_domains)
    else:
        lines.extend(
            [
                "",
                "# Managed focus blocklist disabled with --disable-dns-blocklist.",
                *(f"# server=/{domain}/" for domain in blocked_domains),
            ]
        )
    return "\n".join(lines).strip() + "\n"
