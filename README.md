# Quick Search UX

Unified quick-search extension for Isaac Sim / Omniverse Kit. It combines menu
and stage actions into a single `Ctrl+F` search window and adds a few
productivity features.

## Installation

1. Add this extension's **parent folder** as an extension search path:
   `Window > Extensions > ⚙ > Extension Search Paths`.
2. Find **Quick Search UX** in the list and enable it.

## Features

### Unified Quick Search (`Ctrl+F`)

Opens a search window covering menu bar entries, Create (mesh/shape/light/
camera/scope/xform), Physics presets, and Stage context-menu actions in one
list. Type to filter, use the arrow keys to navigate, `Enter` to run. Create/Physics
actions apply relative to the current selection.

### Layout Quick Load (`Ctrl+8`)

Triggers `Layout > Quick Load` from the menu bar.

### Maximize Window Under Cursor (`Shift+Escape`)

Temporarily maximizes the window currently under the mouse cursor. The window is
undocked and resized to fill the whole application window; the previous
workspace layout is remembered. Press `Shift+Escape` again to restore the
previous layout (position, size, and docking of every window).

### Stage Navigation (Stage window focused)

| Key                  | Action                                    |
|----------------------|-------------------------------------------|
| `Right` / `Left`     | Expand / collapse the selected prim       |
| `Down`               | Select the next visible prim              |
| `Backspace`          | Toggle active state of selected prims     |
| `Ctrl+Shift+C`       | Copy selected prim paths to the clipboard |

### Create Project (`File > Create Project`)

Enter a project name to scaffold the standard folder structure, a `README.md`,
the gridroom environment asset, and a base USD stage saved as
`omniverse/main.usda`. Requires a new/unsaved stage to be active.

### Make All Paths Relative (`File > Make all paths relative`)

Scans stage-scoped used layers and shows a grouped preview table (per layer)
with a details panel before applying any change.

- Rewrites paths in:
  - sublayers
  - references
  - payloads
  - asset-typed attributes, including default values and time samples
    (for example `info:mdl:sourceAsset` and material texture inputs)
- Supports optional collect for online / outside-root assets:
  - USD references/payloads/sublayers are collected with the official
    `omni.kit.usd.collect` tool, so all sub-dependencies (textures, MDL,
    nested USD) are collected and re-mapped correctly
  - single non-USD assets are downloaded directly
  - collected assets are placed under `omniverse/_collected_external_assets/`
    and the owning path is rewritten to a relative path
- Skips anonymous layers and layers outside the current root stage scope.
- Prints detailed debug and change logs to the terminal/log output.

### Automatic Viewport Preview

Saving a stage named `main.usd` / `main.usda` writes a `preview.png` (active
viewport screenshot) next to it, for both local paths and Omniverse URLs.
