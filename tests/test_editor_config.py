import unittest

from macsetup.content import NVIM_MACSETUP_LUA


class EditorConfigTests(unittest.TestCase):
    def test_neovim_uses_current_lazy_lsp_stack(self) -> None:
        self.assertIn("folke/lazy.nvim.git", NVIM_MACSETUP_LUA)
        self.assertIn("williamboman/mason.nvim", NVIM_MACSETUP_LUA)
        self.assertIn("neovim/nvim-lspconfig", NVIM_MACSETUP_LUA)
        self.assertIn("hrsh7th/nvim-cmp", NVIM_MACSETUP_LUA)
        self.assertIn("nvim-telescope/telescope.nvim", NVIM_MACSETUP_LUA)
        self.assertIn("folke/trouble.nvim", NVIM_MACSETUP_LUA)

    def test_neovim_drops_old_coq_stack(self) -> None:
        self.assertNotIn("coq", NVIM_MACSETUP_LUA.lower())
        self.assertNotIn("packer", NVIM_MACSETUP_LUA.lower())

    def test_completion_is_conservative(self) -> None:
        self.assertIn("autocomplete = false", NVIM_MACSETUP_LUA)
        self.assertIn("ghost_text = false", NVIM_MACSETUP_LUA)
        self.assertIn("enable_autosnippets = false", NVIM_MACSETUP_LUA)
        self.assertNotIn("friendly-snippets", NVIM_MACSETUP_LUA)

    def test_neovim_module_is_safe_to_resource_once_loaded(self) -> None:
        self.assertIn("vim.g.macsetup_nvim_loaded", NVIM_MACSETUP_LUA)
        self.assertLess(
            NVIM_MACSETUP_LUA.index("vim.g.macsetup_nvim_loaded"),
            NVIM_MACSETUP_LUA.index('require("lazy").setup'),
        )

    def test_neovim_module_bails_if_lazy_already_configured(self) -> None:
        # If an existing init.lua already ran lazy.setup, calling it again
        # crashes with "Re-sourcing your config is not supported with
        # lazy.nvim". Guard on lazy's own flag and return before our setup.
        self.assertIn("vim.g.lazy_did_setup", NVIM_MACSETUP_LUA)
        self.assertLess(
            NVIM_MACSETUP_LUA.index("vim.g.lazy_did_setup"),
            NVIM_MACSETUP_LUA.index('require("lazy").setup'),
        )

    def test_neovim_keeps_signcolumn_stable(self) -> None:
        self.assertIn('vim.opt.signcolumn = "yes"', NVIM_MACSETUP_LUA)


if __name__ == "__main__":
    unittest.main()
