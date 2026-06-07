import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from macsetup.content import (
    GLOBAL_GITIGNORE,
    INPUTRC_BLOCK,
    INPUTRC_BLOCKS,
    KPHOEN_ZSH_THEME,
    RSYNC_GLOBAL_FILTER,
    ZPROFILE_BLOCK,
    ZPROFILE_BLOCKS,
    ZSHRC_BLOCK,
    ZSHRC_BLOCKS,
    dnsmasq_config,
)
from macsetup.recipes import DEFAULT_RECIPE, ProfileLoadError, load_recipe
from macsetup.steps import (
    HomebrewAnalyticsStep,
    HomebrewInstallStep,
    MacDefaultsStep,
    ManagedDirectoryStep,
    OhMyZshStep,
    OhMyZshThemeStep,
    SudoTouchIdStep,
    XcodeDeveloperDirectoryStep,
    XcodeLicenseStep,
    XcodeMetalToolchainStep,
    _enable_pam_tid,
    build_steps,
)


class RecipeTests(unittest.TestCase):
    def test_erlang_packages_are_omitted(self) -> None:
        formulas = {package.name for package in DEFAULT_RECIPE.brew_formulas}
        self.assertNotIn("kerl", formulas)
        self.assertNotIn("rebar3", formulas)

    def test_modern_package_replacements(self) -> None:
        formulas = {package.name for package in DEFAULT_RECIPE.brew_formulas}
        self.assertIn("node", formulas)
        self.assertNotIn("npm", formulas)
        self.assertIn("openssl@3", formulas)
        self.assertIn("universal-ctags", formulas)
        self.assertIn("git-delta", formulas)
        self.assertIn("git-lfs", formulas)
        self.assertIn("clang-format", formulas)
        self.assertNotIn("diff-so-fancy", formulas)
        self.assertIn("dust", formulas)
        self.assertIn("duf", formulas)
        self.assertIn("openssh", formulas)
        self.assertIn("bc", formulas)
        self.assertIn("broot", formulas)
        self.assertIn("llama.cpp", formulas)
        self.assertIn("tokei", formulas)
        self.assertIn("prettier", formulas)
        self.assertNotIn("prettier", DEFAULT_RECIPE.npm_global_packages)
        casks = {package.name for package in DEFAULT_RECIPE.brew_casks}
        self.assertIn("1password", casks)
        self.assertNotIn("bettertouchtool", casks)
        self.assertIn("claude", casks)
        self.assertIn("codex-app", casks)
        self.assertIn("cursor", casks)
        self.assertIn("discord", casks)
        self.assertIn("firefox", casks)
        self.assertIn("tigervnc", casks)
        self.assertIn("tg-pro", casks)
        self.assertIn("transmission", casks)
        self.assertIn("tradingview", casks)
        self.assertIn("visual-studio-code", casks)
        self.assertIn("vlc", casks)
        self.assertIn("wezterm", casks)
        self.assertIn("windscribe", casks)
        self.assertNotIn("claude-code", casks)
        self.assertNotIn("codex", casks)
        app_bundles = {
            bundle
            for package in DEFAULT_RECIPE.brew_casks
            for bundle in package.app_bundles
        }
        self.assertIn("1Password.app", app_bundles)
        self.assertNotIn("BetterTouchTool.app", app_bundles)
        self.assertIn("Claude.app", app_bundles)
        self.assertIn("Codex.app", app_bundles)
        self.assertIn("Cursor.app", app_bundles)
        self.assertIn("Discord.app", app_bundles)
        self.assertIn("Firefox.app", app_bundles)
        self.assertIn("TigerVNC.app", app_bundles)
        self.assertIn("TG Pro.app", app_bundles)
        self.assertIn("Transmission.app", app_bundles)
        self.assertIn("TradingView.app", app_bundles)
        self.assertIn("Visual Studio Code.app", app_bundles)
        self.assertIn("VLC.app", app_bundles)
        self.assertIn("Windscribe.app", app_bundles)
        windscribe = next(
            package
            for package in DEFAULT_RECIPE.brew_casks
            if package.name == "windscribe"
        )
        self.assertIn("WindscribeInstaller.app", windscribe.installer_bundles)

    def test_python_defaults_include_per_version_tooling(self) -> None:
        self.assertEqual(DEFAULT_RECIPE.python_version, "3.14")
        self.assertEqual(
            DEFAULT_RECIPE.python_versions, ("3.11", "3.12", "3.13", "3.14")
        )
        self.assertEqual(
            DEFAULT_RECIPE.python_tooling_packages,
            ("pip", "wheel", "setuptools", "uv", "poetry"),
        )
        self.assertEqual(DEFAULT_RECIPE.python_build_jobs, "auto")
        self.assertEqual(DEFAULT_RECIPE.python_build_env, ())
        self.assertNotIn("pip", DEFAULT_RECIPE.python_global_packages)
        self.assertNotIn("uv", DEFAULT_RECIPE.python_global_packages)
        self.assertNotIn("poetry", DEFAULT_RECIPE.python_global_packages)

    def test_manual_apps_cover_non_cask_installs(self) -> None:
        manual_apps = {app.name: app for app in DEFAULT_RECIPE.manual_apps}
        self.assertIn("Trello", manual_apps)
        self.assertEqual(manual_apps["Trello"].install_method, "Mac App Store")
        self.assertIn("apps.apple.com", manual_apps["Trello"].url)
        self.assertIn("Trello.app", manual_apps["Trello"].app_bundles)
        self.assertIn("packages", manual_apps["Trello"].tags)

    def test_manual_notes_cover_macos_control_center_setup(self) -> None:
        manual_notes = {note.name: note for note in DEFAULT_RECIPE.manual_notes}
        expected = {
            "Disable Tips Notifications",
            "Disable AirPlay Receiver",
            "Increase Display Resolution",
            "Increase Mouse Sensitivity",
            "Review Battery And Performance Modes",
            "Review Screen Saver And Lock Timing",
            "Restrict Spotlight Results",
            "Review Login And Background Items",
        }
        self.assertLessEqual(expected, set(manual_notes))
        self.assertIn("port 7000", manual_notes["Disable AirPlay Receiver"].detail)
        self.assertIn("Applications", manual_notes["Restrict Spotlight Results"].detail)

    def test_macos_defaults_include_screenshot_location(self) -> None:
        defaults = {setting.name: setting for setting in DEFAULT_RECIPE.macos_defaults}

        self.assertIn("screenshot-location", defaults)
        setting = defaults["screenshot-location"]
        self.assertEqual(setting.domain, "com.apple.screencapture")
        self.assertEqual(setting.key, "location")
        self.assertEqual(setting.value, "~/Desktop/Screenshots")
        self.assertEqual(setting.value_type, "path")
        self.assertEqual(setting.directories, ("~/Desktop/Screenshots",))
        self.assertIn("screenshots", setting.tags)

    def test_screenshot_location_is_managed_not_manual(self) -> None:
        steps = build_steps(DEFAULT_RECIPE)
        step_by_id = {step.id: step for step in steps}

        self.assertNotIn("macos.screenshot-location", step_by_id)
        self.assertIsInstance(
            step_by_id["macos.directory.desktop-screenshots"],
            ManagedDirectoryStep,
        )
        self.assertIsInstance(
            step_by_id["macos.defaults.screenshot-location"], MacDefaultsStep
        )

    def test_deprecated_packages_are_omitted(self) -> None:
        packages = (*DEFAULT_RECIPE.brew_formulas, *DEFAULT_RECIPE.brew_casks)
        package_names = {package.name for package in packages}
        package_tags = {tag for package in packages for tag in package.tags}
        package_notes = " ".join(package.note.lower() for package in packages)
        self.assertNotIn("sassc", package_names)
        self.assertNotIn("legacy", package_tags)
        self.assertNotIn("deprecated", package_notes)

    def test_git_settings_match_live_delta_lfs_setup(self) -> None:
        settings = {
            setting.key: setting.value for setting in DEFAULT_RECIPE.git_settings
        }
        self.assertNotIn("user.name", settings)
        self.assertNotIn("user.email", settings)
        self.assertEqual(settings["core.editor"], "nvim")
        self.assertEqual(settings["core.excludesfile"], "~/.gitignore_global")
        self.assertEqual(settings["core.pager"], "delta")
        self.assertEqual(settings["interactive.diffFilter"], "delta --color-only")
        # pager.log dispatches plain `git log` to bat and `git log -p` to delta
        # via a compiled streaming binary (no temp-file buffering, no latency).
        self.assertEqual(settings["pager.log"], "$HOME/.local/bin/git-log-pager")
        self.assertEqual(settings["pager.show"], "delta")
        self.assertEqual(settings["pager.diff"], "delta")
        self.assertEqual(settings["pager.branch"], "false")
        self.assertEqual(settings["delta.navigate"], "true")
        self.assertEqual(settings["delta.dark"], "true")
        self.assertEqual(settings["delta.line-numbers"], "false")
        self.assertEqual(settings["delta.pager"], "less -FRX")
        self.assertEqual(settings["delta.side-by-side"], "false")
        self.assertEqual(settings["delta.keep-plus-minus-markers"], "true")
        self.assertEqual(settings["delta.hunk-header-style"], "file line-number syntax")
        self.assertEqual(settings["delta.file-style"], "bold blue")
        self.assertEqual(settings["delta.max-line-distance"], "0.6")
        self.assertEqual(settings["delta.word-diff-regex"], "\\w+")
        self.assertEqual(settings["merge.conflictStyle"], "zdiff3")
        self.assertEqual(settings["diff.colorMoved"], "default")
        self.assertEqual(settings["diff.colorMovedWS"], "allow-indentation-change")
        self.assertEqual(settings["filter.lfs.clean"], "git-lfs clean -- %f")
        self.assertEqual(settings["filter.lfs.smudge"], "git-lfs smudge -- %f")
        self.assertEqual(settings["filter.lfs.process"], "git-lfs filter-process")
        self.assertEqual(settings["filter.lfs.required"], "true")
        self.assertNotIn("rebase.updateRefs", settings)

    def test_profile_overlay_can_extend_and_deny_without_code_changes(self) -> None:
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "local.toml"
            profile.write_text(
                """
[brew]
formula_deny = ["cowsay"]

[[brew.formula_extend]]
name = "helix"
tags = ["packages", "editor"]

[git]
[[git.setting_extend]]
key = "user.name"
value = "Example User"

[manual]
app_deny = ["Trello"]
note_deny = ["Disable Tips Notifications"]

[macos]
default_deny = ["screenshot-location"]

[[macos.default_extend]]
name = "example-setting"
domain = "com.example.macsetup"
key = "enabled"
value = "true"
value_type = "bool"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            recipe = load_recipe((profile,))
        formulas = {package.name for package in recipe.brew_formulas}
        settings = {setting.key: setting.value for setting in recipe.git_settings}
        macos_defaults = {setting.name: setting for setting in recipe.macos_defaults}
        manual_apps = {app.name for app in recipe.manual_apps}
        manual_notes = {note.name for note in recipe.manual_notes}
        self.assertNotIn("cowsay", formulas)
        self.assertIn("helix", formulas)
        self.assertEqual(settings["user.name"], "Example User")
        self.assertNotIn("screenshot-location", macos_defaults)
        self.assertEqual(macos_defaults["example-setting"].value_type, "bool")
        self.assertNotIn("Trello", manual_apps)
        self.assertNotIn("Disable Tips Notifications", manual_notes)

    def test_source_build_overlay_adds_build_dependencies(self) -> None:
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "local.toml"
            profile.write_text(
                """
[brew]
formulas = []

[source_builds]
[[source_builds.packages]]
name = "hiproc"
repo = "git@github.com:example/hiproc.git"
build_system = "cargo"
binaries = ["hiproc"]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            recipe = load_recipe((profile,))

        source_builds = {package.name: package for package in recipe.source_builds}
        formulas = {package.name for package in recipe.brew_formulas}
        self.assertIn("hiproc", source_builds)
        self.assertEqual(source_builds["hiproc"].binary_dir, "target/release")
        self.assertEqual(
            source_builds["hiproc"].build_commands, ("cargo build --release",)
        )
        self.assertIn("git", formulas)
        self.assertIn("rust", formulas)

    def test_rejected_source_build_dependency_blocks_profile(self) -> None:
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "local.toml"
            profile.write_text(
                """
[brew]
formula_reject = ["rust"]

[source_builds]
[[source_builds.package_extend]]
name = "hiproc"
repo = "git@github.com:example/hiproc.git"
build_system = "cargo"
binaries = ["hiproc"]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ProfileLoadError):
                load_recipe((profile,))

    def test_profile_reject_blocks_reintroduced_packages(self) -> None:
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "local.toml"
            profile.write_text(
                """
[brew]

[[brew.formula_extend]]
name = "sassc"
tags = ["packages", "build"]
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ProfileLoadError):
                load_recipe((profile,))

    def test_global_gitignore_includes_live_local_tool_patterns(self) -> None:
        for pattern in (
            ".vscode/",
            ".aider*",
            ".claude/",
            ".coverage",
            ".pytype/",
            "*.dSYM",
            "cscope.out",
        ):
            self.assertIn(pattern, GLOBAL_GITIGNORE)

    def test_mrsync_global_filter_includes_common_build_caches(self) -> None:
        expected_patterns = (
            "- __pycache__/",
            "- .venv/",
            "- .uv-cache/",
            "- .pytest_cache/",
            "- .mypy_cache/",
            "- .ruff_cache/",
            "- .hypothesis/",
            "- node_modules/",
            "- .pnpm-store/",
            "- .parcel-cache/",
            "- .turbo/",
            "- .next/",
            "- .nuxt/",
            "- .svelte-kit/",
            "- .vite/",
            "- build/",
            "- dist/",
            "- target/",
            "- coverage/",
            "- CMakeFiles/",
            "- CMakeCache.txt",
            "- cmake-build-*/",
            "- compile_commands.json",
            "- .gradle/",
            "- .build/",
            "- .terraform/",
            "- .terragrunt-cache/",
            "- bazel-*/",
            "- buck-out/",
            "- _build/",
            "- DerivedData/",
            "- *.dSYM/",
            "- .cache/",
            "- .tmp/",
        )
        for pattern in expected_patterns:
            self.assertIn(pattern, RSYNC_GLOBAL_FILTER)

    def test_mrsync_global_filter_has_no_duplicate_active_rules(self) -> None:
        active_rules = [
            line.strip()
            for line in RSYNC_GLOBAL_FILTER.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(len(active_rules), len(set(active_rules)))

    def test_dns_blocklist_is_enabled_by_default(self) -> None:
        body = dnsmasq_config(
            servers=DEFAULT_RECIPE.dns_servers,
            passthrough_domains=DEFAULT_RECIPE.dns_passthrough_domains,
            blocked_domains=DEFAULT_RECIPE.dns_blocked_domains,
            enable_blocklist=True,
        )
        active_lines = [
            line for line in body.splitlines() if line and not line.startswith("#")
        ]
        self.assertIn("server=/openai.com/", active_lines)

    def test_dns_blocklist_can_be_disabled(self) -> None:
        body = dnsmasq_config(
            servers=DEFAULT_RECIPE.dns_servers,
            passthrough_domains=DEFAULT_RECIPE.dns_passthrough_domains,
            blocked_domains=DEFAULT_RECIPE.dns_blocked_domains,
            enable_blocklist=False,
        )
        active_lines = [
            line for line in body.splitlines() if line and not line.startswith("#")
        ]
        self.assertNotIn("server=/openai.com/", active_lines)
        self.assertIn(
            "# Managed focus blocklist disabled with --disable-dns-blocklist.", body
        )
        self.assertIn("# server=/openai.com/", body)

    def test_homebrew_analytics_step_runs_after_install_step(self) -> None:
        steps = build_steps(DEFAULT_RECIPE)
        self.assertIsInstance(steps[0], XcodeDeveloperDirectoryStep)
        self.assertIsInstance(steps[1], XcodeLicenseStep)
        self.assertIsInstance(steps[2], XcodeMetalToolchainStep)
        self.assertIsInstance(steps[3], SudoTouchIdStep)
        self.assertIsInstance(steps[4], HomebrewInstallStep)
        self.assertIsInstance(steps[5], HomebrewAnalyticsStep)

    def test_oh_my_zsh_install_step_is_part_of_package_setup(self) -> None:
        steps = build_steps(DEFAULT_RECIPE)
        step = next(step for step in steps if isinstance(step, OhMyZshStep))

        self.assertIn("shell", step.tags)
        self.assertIn("packages", step.tags)

    def test_custom_kphoen_theme_is_managed_after_oh_my_zsh_install(self) -> None:
        steps = build_steps(DEFAULT_RECIPE)
        install_index = next(
            index for index, step in enumerate(steps) if isinstance(step, OhMyZshStep)
        )
        theme_index = next(
            index
            for index, step in enumerate(steps)
            if isinstance(step, OhMyZshThemeStep)
        )
        theme_step = steps[theme_index]

        self.assertLess(install_index, theme_index)
        self.assertIn("shell", theme_step.tags)
        self.assertIn("packages", theme_step.tags)
        self.assertIn("files", theme_step.tags)
        self.assertIn("file:~/.oh-my-zsh", {str(item) for item in theme_step.requires})
        self.assertIn(
            "file:~/.oh-my-zsh/themes/kphoen.zsh-theme",
            {str(item) for item in theme_step.provides},
        )

    def test_touch_id_sudo_uncomments_template_line(self) -> None:
        body = "# sudo_local\n#auth       sufficient     pam_tid.so\n"
        self.assertEqual(
            _enable_pam_tid(body),
            "# sudo_local\nauth       sufficient     pam_tid.so\n",
        )

    def test_touch_id_sudo_inserts_before_existing_active_rules(self) -> None:
        body = "# sudo_local\naccount required pam_permit.so\n"
        self.assertEqual(
            _enable_pam_tid(body),
            "# sudo_local\nauth       sufficient     pam_tid.so\naccount required pam_permit.so\n",
        )

    def test_pyenv_shell_init_disables_rehash(self) -> None:
        self.assertIn('eval "$(/opt/homebrew/bin/brew shellenv)"', ZPROFILE_BLOCK)
        self.assertIn('eval "$(pyenv init --path --no-rehash)"', ZPROFILE_BLOCK)
        self.assertIn('eval "$(pyenv init - zsh --no-rehash)"', ZSHRC_BLOCK)

    def test_zprofile_template_is_split_into_feature_blocks(self) -> None:
        block_names = [name for name, _ in ZPROFILE_BLOCKS]
        self.assertEqual(block_names, ["homebrew", "pyenv"])
        self.assertNotIn("macsetup-template-block", ZPROFILE_BLOCK)

    def test_zshrc_skips_homebrew_compinit_for_root_shells(self) -> None:
        self.assertIn("[[ ${EUID:-$(id -u)} -ne 0 ]] && command -v brew", ZSHRC_BLOCK)
        self.assertIn("compinit", ZSHRC_BLOCK)

    def test_zshrc_template_is_split_into_feature_blocks(self) -> None:
        block_names = [name for name, _ in ZSHRC_BLOCKS]
        self.assertEqual(
            block_names,
            [
                "env",
                "history",
                "oh-my-zsh",
                "options",
                "aliases",
                "optional-tools",
                "completions",
                "pyenv",
                "prompt",
                "functions",
            ],
        )
        self.assertNotIn("macsetup-template-block", ZSHRC_BLOCK)

    def test_share_history_is_unset_after_oh_my_zsh(self) -> None:
        # oh-my-zsh's lib/history.zsh runs `setopt share_history`, so our
        # `unsetopt share_history` must come AFTER the oh-my-zsh source to win, and
        # there must be exactly one copy (the dead pre-oh-my-zsh duplicate is gone).
        self.assertEqual(ZSHRC_BLOCK.count("unsetopt share_history"), 1)
        self.assertLess(
            ZSHRC_BLOCK.index('source "$ZSH/oh-my-zsh.sh"'),
            ZSHRC_BLOCK.index("unsetopt share_history"),
        )

    def test_shell_defaults_include_prefix_history_search(self) -> None:
        self.assertIn("unsetopt share_history", ZSHRC_BLOCK)
        self.assertIn("up-line-or-beginning-search", ZSHRC_BLOCK)
        self.assertIn("down-line-or-beginning-search", ZSHRC_BLOCK)
        self.assertIn("bindkey '^[[A' up-line-or-beginning-search", ZSHRC_BLOCK)
        self.assertIn('"\\e[A": history-search-backward', INPUTRC_BLOCK)
        self.assertIn('"\\e[B": history-search-forward', INPUTRC_BLOCK)
        self.assertIn("set completion-ignore-case on", INPUTRC_BLOCK)

    def test_shell_defaults_include_current_zsh_options(self) -> None:
        self.assertIn("setopt auto_cd", ZSHRC_BLOCK)
        self.assertIn("setopt auto_pushd", ZSHRC_BLOCK)
        self.assertIn("setopt pushd_ignore_dups", ZSHRC_BLOCK)
        self.assertIn("setopt pushdminus", ZSHRC_BLOCK)
        self.assertIn("setopt complete_in_word", ZSHRC_BLOCK)
        self.assertIn("setopt always_to_end", ZSHRC_BLOCK)
        self.assertIn("setopt menu_complete", ZSHRC_BLOCK)
        self.assertIn("setopt extended_history", ZSHRC_BLOCK)
        self.assertIn("setopt hist_expire_dups_first", ZSHRC_BLOCK)
        self.assertIn("setopt hist_ignore_dups", ZSHRC_BLOCK)
        self.assertIn("setopt hist_ignore_space", ZSHRC_BLOCK)
        self.assertIn("setopt hist_verify", ZSHRC_BLOCK)
        self.assertIn("setopt interactive_comments", ZSHRC_BLOCK)
        self.assertIn("setopt long_list_jobs", ZSHRC_BLOCK)
        self.assertIn("setopt prompt_subst", ZSHRC_BLOCK)
        self.assertIn("unsetopt flow_control", ZSHRC_BLOCK)

    def test_shell_pager_uses_less_not_bat(self) -> None:
        self.assertIn("export PAGER='less -FRX'", ZSHRC_BLOCK)
        self.assertIn("export LESS='-R'", ZSHRC_BLOCK)
        self.assertIn("export BAT_PAGER='less -FRX'", ZSHRC_BLOCK)
        self.assertIn("unset GIT_PAGER", ZSHRC_BLOCK)
        self.assertIn("unset NO_COLOR", ZSHRC_BLOCK)
        self.assertNotIn("export PAGER=bat", ZSHRC_BLOCK)
        self.assertNotIn("export GIT_PAGER=bat", ZSHRC_BLOCK)
        self.assertNotIn("export GIT_PAGER=cat", ZSHRC_BLOCK)

    def test_inputrc_template_is_split_into_feature_blocks(self) -> None:
        block_names = [name for name, _ in INPUTRC_BLOCKS]
        self.assertEqual(block_names, ["completion", "history"])
        self.assertNotIn("macsetup-template-block", INPUTRC_BLOCK)

    def test_zshrc_adopts_generic_live_shell_settings(self) -> None:
        self.assertIn('export ZSH="${ZSH:-$HOME/.oh-my-zsh}"', ZSHRC_BLOCK)
        self.assertIn('ZSH_THEME="${ZSH_THEME:-kphoen}"', ZSHRC_BLOCK)
        self.assertIn("plugins=(git)", ZSHRC_BLOCK)
        self.assertIn('source "$ZSH/oh-my-zsh.sh"', ZSHRC_BLOCK)
        self.assertIn("unalias -m 'g*'", ZSHRC_BLOCK)
        self.assertIn("alias ip=ipython", ZSHRC_BLOCK)
        self.assertIn("alias cat='bat'", ZSHRC_BLOCK)
        self.assertIn("alias ccat='command cat'", ZSHRC_BLOCK)
        self.assertIn("alias batp='bat --paging=always'", ZSHRC_BLOCK)
        self.assertIn("BUN_INSTALL", ZSHRC_BLOCK)
        self.assertIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS", ZSHRC_BLOCK)
        self.assertIn("broot/launcher", ZSHRC_BLOCK)
        self.assertIn("function y()", ZSHRC_BLOCK)
        self.assertIn("function dvrEncode()", ZSHRC_BLOCK)

    def test_zshrc_preserves_kphoen_theme_prompt(self) -> None:
        blocks = dict(ZSHRC_BLOCKS)
        prompt_block = blocks["prompt"]
        self.assertIn("kphoen Oh My Zsh theme owns PROMPT/RPROMPT", prompt_block)
        self.assertNotIn("PROMPT=", prompt_block)
        self.assertNotIn("RPROMPT=", prompt_block)
        self.assertLess(
            ZSHRC_BLOCK.index('ZSH_THEME="${ZSH_THEME:-kphoen}"'),
            ZSHRC_BLOCK.index("kphoen Oh My Zsh theme owns PROMPT/RPROMPT"),
        )

    def test_custom_kphoen_theme_moves_git_prompt_to_rprompt(self) -> None:
        self.assertIn(
            "PROMPT='%{$fg[red]%}%n%{$reset_color%}@%{$fg[magenta]%}%m%{$reset_color%}:%{$fg[blue]%}%~%{$reset_color%}$ '",
            KPHOEN_ZSH_THEME,
        )
        self.assertIn(
            "RPROMPT='${return_code}$(git_prompt_info)$(git_prompt_status)%{$reset_color%}'",
            KPHOEN_ZSH_THEME,
        )
        self.assertNotIn("%~%{$reset_color%}$(git_prompt_info)]", KPHOEN_ZSH_THEME)

    def test_shell_steps_install_inputrc_support(self) -> None:
        shell_step_ids = {
            step.id for step in build_steps(DEFAULT_RECIPE) if "shell" in step.tags
        }
        self.assertIn("shell.zprofile.homebrew", shell_step_ids)
        self.assertIn("shell.zprofile.pyenv", shell_step_ids)
        self.assertIn("shell.oh-my-zsh-theme.kphoen", shell_step_ids)
        self.assertIn("shell.zshrc.env", shell_step_ids)
        self.assertIn("shell.zshrc.history", shell_step_ids)
        self.assertIn("shell.zshrc.options", shell_step_ids)
        self.assertIn("shell.zshrc.functions", shell_step_ids)
        self.assertIn("shell.inputrc.completion", shell_step_ids)
        self.assertIn("shell.inputrc.history", shell_step_ids)
        self.assertNotIn("shell.zprofile", shell_step_ids)
        self.assertNotIn("shell.zshrc", shell_step_ids)
        self.assertNotIn("shell.inputrc", shell_step_ids)

    def test_sync_steps_install_mrsync_support(self) -> None:
        sync_step_ids = {
            step.id for step in build_steps(DEFAULT_RECIPE) if "sync" in step.tags
        }
        self.assertIn("sync.rsync-global-filter", sync_step_ids)
        self.assertIn("sync.rsync-local-filter-example", sync_step_ids)
        self.assertIn("sync.mrsync-launcher", sync_step_ids)
        self.assertIn("sync.mrsync-shell", sync_step_ids)

    def test_git_log_pager_builds_before_its_git_config(self) -> None:
        from macsetup.graph import StepGraph

        steps = build_steps(DEFAULT_RECIPE)
        ids = [step.id for step in steps]
        self.assertIn("git.log-pager", ids)
        ordered = [step.id for step in StepGraph(tuple(steps)).ordered(steps)]
        self.assertLess(
            ordered.index("git.log-pager"),
            ordered.index("git.config.pager.log"),
            "the dispatch binary must be compiled before pager.log references it",
        )


if __name__ == "__main__":
    unittest.main()
