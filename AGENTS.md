# AGENTS

## Environment assumptions (local)
- Isaac Sim install path: `A:\isaac-sim-6-0`.
- Physics target is **PhysX** (not Newton). Keep PhysX-oriented actions/code paths.
- Kit logs: `C:/Users/{username}/.nvidia-omniverse/logs/Kit/Isaac-Sim Full/6.0/kit_YYYYMMDD_######.log`.
- USD tools root: `A:\Tools`.
- PowerShell profile with USD env setup: `C:\Users\Vitus\Documents\PowerShell\PowerShell_profile.ps1`.
- The profile exports `USDROOT`, `USD_INSTALL_DIR`, `PXR_MTLX_STDLIB_SEARCH_PATHS`, `PXR_USD_WINDOWS_DLL_PATH`, `PYTHONPATH`, and prepends USD binaries/libs to `Path`.

## Repo shape and real entrypoints
- This repo is a single Omniverse extension package; there is no monorepo/task runner/CI config here.
- Extension manifest: `config/extension.toml`.
- Python module entrypoint: `omni/quicksearch/ux/extension.py` (`Extension` class lifecycle).
- Search model and action inventory: `omni/quicksearch/ux/model.py`.
- Menubar snapshot capture feeding search results: `omni/quicksearch/ux/menu_snapshot.py`.
- Hotkey/action registration and keyboard fallback logic: `omni/quicksearch/ux/hotkeys.py`.

## Run + verify workflow
- Enable by adding this repo's parent directory to Extension Search Paths, then enable **Quick Search UX** in Isaac Sim (see `README.md`).
- Fast syntax check for touched files:
  - `python -m compileall "omni/quicksearch/ux/extension.py" "omni/quicksearch/ux/model.py"`
- Manual validation is primary in-app verification:
  - `Ctrl+F` opens unified search.
  - Menu/stage actions execute from search results.
  - Toolbar buttons and hotkeys still work (`Ctrl+8`, `Shift+Escape`, Stage hotkeys).

## Agent gotchas for this codebase
- `extension.py` prewarms a `QuickSearchWindow`; changes to indexing often require reasoning about window lifecycle (`show_window`, prewarm task, rebuild flow).
- Menu actions are derived from runtime UI/menu state; missing entries are often timing/snapshot issues, not static data issues.
- Some dependencies in `extension.toml` are optional; keep graceful fallbacks and warning logs when APIs are unavailable.
