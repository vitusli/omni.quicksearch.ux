# Quick Search UX

Unified quick-search extension for Isaac Sim / Omniverse Kit. It combines menu
and stage actions into a single `Ctrl+F` search window, adds productivity
hotkeys, and places Pivot / Array / Scene Optimizer buttons directly in the
main toolbar.

## Installation

1. Add this extension's **parent folder** as an extension search path:
   `Window > Extensions > ⚙ > Extension Search Paths`.
2. Find **Quick Search UX** in the list and enable it.

## Hotkeys

| Hotkey              | Context          | Action                                          |
|---------------------|------------------|-------------------------------------------------|
| `Ctrl+F`            | Global           | Open unified quick-search window                |
| `Shift+Escape`      | Global           | Toggle maximize / restore window under cursor   |
| `Right`             | Stage (focused)  | Expand selected prim                            |
| `Left`              | Stage (focused)  | Collapse selected prim                          |
| `Down`              | Stage (focused)  | Select next visible prim                        |
| `Backspace`         | Stage (focused)  | Toggle active state of selected prims           |
| `Ctrl+Shift+C`      | Stage (focused)  | Copy selected prim paths to clipboard           |

## Features

### Unified Quick Search (`Ctrl+F`)

Opens a search window covering menu bar entries, Create (mesh/shape/light/
camera/scope/xform), Physics presets, and Stage context-menu actions in one
list. Type to filter, arrow keys to navigate, `Enter` to run. Create/Physics
actions apply relative to the current selection.

### Main Toolbar Buttons

Adds **Pivot Tool**, **Array Tool**, and **Scene Optimizer** buttons to the
main toolbar. Each button is hidden until the corresponding tool is available.

### Create Project (`File > Create Project`)

Enter a project name to scaffold the standard folder structure, a `README.md`,
the gridroom environment asset, and a base USD stage saved as
`omniverse/main.usda`. Requires a new/unsaved stage to be active.

### Make All Paths Relative (`File > Make all paths relative`)

Scans all stage layers and shows a preview before rewriting sublayer, reference,
payload, and asset attribute paths to relative ones. External assets (online or
outside the project root) are collected automatically and placed under
`omniverse/_collected_external_assets/`.

### Automatic Viewport Preview

Saving a stage named `main.usd` / `main.usda` writes a `preview.png` (active
viewport screenshot) next to it, for both local paths and Omniverse URLs.
