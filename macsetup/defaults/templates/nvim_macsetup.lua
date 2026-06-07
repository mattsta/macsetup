if vim.g.macsetup_nvim_loaded then
  return
end
vim.g.macsetup_nvim_loaded = true

-- lazy.nvim refuses to be configured twice in the same session and aborts
-- with "Re-sourcing your config is not supported with lazy.nvim". That happens
-- when an existing init.lua already configured lazy before loading this
-- module. This module is meant to OWN the lazy configuration, so if some other
-- config got there first, bail out loudly instead of crashing: the init.lua
-- should be reduced to just bootstrapping and require("macsetup").
if vim.g.lazy_did_setup then
  vim.notify(
    "macsetup: lazy.nvim was already configured before require(\"macsetup\"). "
      .. "Reduce ~/.config/nvim/init.lua to the lazy bootstrap plus "
      .. 'require("macsetup") so this module owns the plugin setup.',
    vim.log.levels.WARN
  )
  return
end

local lazypath = vim.fn.stdpath("data") .. "/lazy/lazy.nvim"
if not (vim.uv or vim.loop).fs_stat(lazypath) then
  local lazyrepo = "https://github.com/folke/lazy.nvim.git"
  local out = vim.fn.system({ "git", "clone", "--filter=blob:none", "--branch=stable", lazyrepo, lazypath })
  if vim.v.shell_error ~= 0 then
    vim.api.nvim_echo({
      { "Failed to clone lazy.nvim:\n", "ErrorMsg" },
      { out, "WarningMsg" },
      { "\nPress any key to exit..." },
    }, true, {})
    vim.fn.getchar()
    os.exit(1)
  end
end
vim.opt.rtp:prepend(lazypath)

local ok = pcall(vim.cmd, "colorscheme sorbet")
if not ok then
  vim.cmd("colorscheme default")
end

vim.g.mapleader = " "
vim.g.maplocalleader = "\\"
vim.g.neovide_input_use_logo = 1

vim.keymap.set("", "<D-v>", "+p<CR>", { noremap = true, silent = true })
vim.keymap.set("!", "<D-v>", "<C-R>+", { noremap = true, silent = true })
vim.keymap.set("t", "<D-v>", "<C-R>+", { noremap = true, silent = true })
vim.keymap.set("v", "<D-v>", "<C-R>+", { noremap = true, silent = true })

vim.opt.showmatch = true
vim.opt.ignorecase = true
-- Keep the sign column always visible so the screen does not bounce widths
-- as diagnostics appear and vanish during editing.
vim.opt.signcolumn = "yes"
vim.opt.hlsearch = true
vim.opt.incsearch = true
vim.opt.clipboard = ""
vim.opt.mouse = ""
vim.opt.tabstop = 4
vim.opt.softtabstop = 4
vim.opt.expandtab = true
vim.opt.shiftwidth = 4
vim.opt.autoindent = true
vim.opt.wildmode = "longest,list"
vim.opt.ttyfast = true
vim.opt.lazyredraw = true
vim.opt.updatetime = 250
vim.opt.timeoutlen = 500
vim.opt.ttimeoutlen = 0

vim.g.loaded_python3_provider = 0
vim.g.loaded_ruby_provider = 0
vim.g.loaded_perl_provider = 0
vim.g.loaded_node_provider = 0

local function cmp_select_or_jump(cmp, luasnip, fallback)
  if cmp.visible() then
    cmp.select_next_item()
  elseif luasnip.expand_or_locally_jumpable and luasnip.expand_or_locally_jumpable() then
    luasnip.expand_or_jump()
  elseif luasnip.expand_or_jumpable and luasnip.expand_or_jumpable() then
    luasnip.expand_or_jump()
  else
    fallback()
  end
end

local function cmp_select_or_jump_back(cmp, luasnip, fallback)
  if cmp.visible() then
    cmp.select_prev_item()
  elseif luasnip.locally_jumpable and luasnip.locally_jumpable(-1) then
    luasnip.jump(-1)
  elseif luasnip.jumpable and luasnip.jumpable(-1) then
    luasnip.jump(-1)
  else
    fallback()
  end
end

local function enable_lsp(server, config)
  local capabilities = require("cmp_nvim_lsp").default_capabilities()
  config = config or {}
  config.capabilities = capabilities
  if vim.lsp.config and vim.lsp.enable then
    vim.lsp.config[server] = vim.tbl_deep_extend("force", vim.lsp.config[server] or {}, config)
    vim.lsp.enable(server)
  else
    require("lspconfig")[server].setup(config)
  end
end

vim.cmd("filetype plugin indent on")
vim.cmd("syntax on")
vim.cmd("filetype plugin on")

require("lazy").setup({
  spec = {
    {
      "neovim/nvim-lspconfig",
      event = { "BufReadPre", "BufNewFile" },
      dependencies = {
        { "williamboman/mason.nvim", build = ":MasonUpdate", config = true },
        {
          "williamboman/mason-lspconfig.nvim",
          opts = {
            ensure_installed = { "pyright", "ruff", "ts_ls", "eslint" },
          },
        },
        "hrsh7th/cmp-nvim-lsp",
      },
      config = function()
        enable_lsp("pyright", {
          settings = {
            python = {
              analysis = {
                autoSearchPaths = true,
                diagnosticMode = "workspace",
                useLibraryCodeForTypes = true,
              },
            },
          },
        })
        enable_lsp("ruff")
        enable_lsp("ts_ls")
        enable_lsp("eslint")

        vim.keymap.set("n", "<leader>ca", vim.lsp.buf.code_action, { desc = "Code Action" })
        vim.keymap.set({ "n", "v" }, "<leader>ca", vim.lsp.buf.code_action, { desc = "Code Action" })
        vim.keymap.set("n", "gd", vim.lsp.buf.definition, { desc = "Goto Definition" })
        vim.keymap.set("n", "gr", vim.lsp.buf.references, { desc = "References" })
        vim.keymap.set("n", "gD", vim.lsp.buf.declaration, { desc = "Goto Declaration" })
        vim.keymap.set("n", "gi", vim.lsp.buf.implementation, { desc = "Goto Implementation" })
        vim.keymap.set("n", "<leader>rn", vim.lsp.buf.rename, { desc = "Rename" })
      end,
    },
    {
      "hrsh7th/nvim-cmp",
      event = "InsertEnter",
      dependencies = {
        "hrsh7th/cmp-nvim-lsp",
        "hrsh7th/cmp-buffer",
        "hrsh7th/cmp-path",
        "L3MON4D3/LuaSnip",
        "saadparwaiz1/cmp_luasnip",
      },
      config = function()
        local cmp = require("cmp")
        local luasnip = require("luasnip")

        luasnip.config.setup({
          enable_autosnippets = false,
          history = false,
        })

        cmp.setup({
          snippet = {
            expand = function(args)
              luasnip.lsp_expand(args.body)
            end,
          },
          mapping = cmp.mapping.preset.insert({
            ["<C-d>"] = cmp.mapping.scroll_docs(-4),
            ["<C-f>"] = cmp.mapping.scroll_docs(4),
            ["<C-Space>"] = cmp.mapping.complete(),
            ["<CR>"] = cmp.mapping.confirm({
              behavior = cmp.ConfirmBehavior.Insert,
              select = false,
            }),
            ["<C-y>"] = cmp.mapping.confirm({
              behavior = cmp.ConfirmBehavior.Replace,
              select = true,
            }),
            ["<Tab>"] = cmp.mapping(function(fallback)
              cmp_select_or_jump(cmp, luasnip, fallback)
            end, { "i", "s" }),
            ["<S-Tab>"] = cmp.mapping(function(fallback)
              cmp_select_or_jump_back(cmp, luasnip, fallback)
            end, { "i", "s" }),
          }),
          sources = {
            { name = "nvim_lsp" },
            { name = "luasnip" },
            { name = "buffer" },
            { name = "path" },
          },
          completion = {
            autocomplete = false,
          },
          experimental = {
            ghost_text = false,
          },
          sorting = {
            comparators = {
              cmp.config.compare.offset,
              cmp.config.compare.exact,
              cmp.config.compare.score,
              cmp.config.compare.kind,
              cmp.config.compare.sort_text,
              cmp.config.compare.length,
              cmp.config.compare.order,
            },
          },
        })
      end,
    },
    {
      "L3MON4D3/LuaSnip",
      config = function()
        require("luasnip").config.setup({
          enable_autosnippets = false,
          history = false,
        })
      end,
    },
    {
      "nvim-telescope/telescope.nvim",
      cmd = "Telescope",
      dependencies = { "nvim-lua/plenary.nvim" },
      config = function()
        require("telescope").setup({
          defaults = {
            layout_config = {
              horizontal = { prompt_position = "top" },
              vertical = { mirror = true },
            },
          },
        })
        vim.keymap.set("n", "<leader>ff", require("telescope.builtin").find_files, { desc = "Find files" })
        vim.keymap.set("n", "<leader>fg", require("telescope.builtin").live_grep, { desc = "Live grep" })
        vim.keymap.set("n", "<leader>fb", require("telescope.builtin").buffers, { desc = "Find buffers" })
        vim.keymap.set("n", "<leader>fh", require("telescope.builtin").help_tags, { desc = "Find help" })
        vim.keymap.set("n", "<leader>fd", require("telescope.builtin").diagnostics, { desc = "Show diagnostics" })
      end,
    },
    {
      "folke/trouble.nvim",
      cmd = { "TroubleToggle", "Trouble" },
      dependencies = "nvim-tree/nvim-web-devicons",
      config = function()
        require("trouble").setup({
          auto_preview = false,
        })
        vim.keymap.set("n", "<leader>xx", "<cmd>TroubleToggle<cr>", { desc = "Toggle Trouble" })
        vim.keymap.set("n", "<leader>xw", "<cmd>TroubleToggle workspace_diagnostics<cr>", { desc = "Workspace diagnostics" })
        vim.keymap.set("n", "<leader>xd", "<cmd>TroubleToggle document_diagnostics<cr>", { desc = "Document diagnostics" })
        vim.keymap.set("n", "<leader>xl", "<cmd>TroubleToggle loclist<cr>", { desc = "Location list" })
        vim.keymap.set("n", "<leader>xq", "<cmd>TroubleToggle quickfix<cr>", { desc = "Quickfix list" })
        vim.keymap.set("n", "gR", "<cmd>TroubleToggle lsp_references<cr>", { desc = "LSP references" })
      end,
    },
  },
  install = { colorscheme = { "habamax" } },
  checker = { enabled = false },
  change_detection = { enabled = false },
})
