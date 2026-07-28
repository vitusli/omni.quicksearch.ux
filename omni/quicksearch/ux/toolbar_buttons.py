"""Toolbar button groups for Pivot Tool, Array Tool, and Scene Optimizer.

Originally maintained in omni.toolbar.pivot_array; merged into omni.quicksearch.ux
so that a single extension owns both the quick-search UX and these toolbar icons.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Iterable

import carb
import carb.settings
import omni.ext
import omni.kit.app
import omni.kit.context_menu
import omni.kit.actions.core
import omni.ui as ui
import omni.kit.menu.utils

from omni.kit.widget.toolbar import WidgetGroup


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _iter_menu_items(menu_key: str, merged_menus: dict):
    stack = [menu_key]
    visited = set()

    while stack:
        key = stack.pop()
        if key in visited:
            continue
        visited.add(key)

        menu_data = merged_menus.get(key)
        if not menu_data:
            continue

        for item in menu_data.get("items", []):
            yield item
            sub_key = getattr(item, "sub_menu", None)
            if isinstance(sub_key, str) and sub_key in merged_menus:
                stack.append(sub_key)


def _execute_menu_action(preferred_names: Iterable[str], root_menu: str = "Tools") -> bool:
    merged_menus = omni.kit.menu.utils.get_merged_menus() or {}
    if root_menu not in merged_menus:
        return False

    lower_names = {n.lower() for n in preferred_names}

    for item in _iter_menu_items(root_menu, merged_menus):
        name = (getattr(item, "name", "") or "").strip()
        if name.lower() not in lower_names:
            continue

        onclick_action = getattr(item, "onclick_action", None)
        if onclick_action and len(onclick_action) >= 2:
            omni.kit.actions.core.execute_action(onclick_action[0], onclick_action[1])
            return True

        onclick_fn = getattr(item, "onclick_fn", None)
        if callable(onclick_fn):
            onclick_fn()
            return True

    return False


def _has_menu_action(preferred_names: Iterable[str], root_menu: str = "Tools") -> bool:
    merged_menus = omni.kit.menu.utils.get_merged_menus() or {}
    if root_menu not in merged_menus:
        return False

    lower_names = {n.lower() for n in preferred_names}
    for item in _iter_menu_items(root_menu, merged_menus):
        name = (getattr(item, "name", "") or "").strip().lower()
        if name in lower_names:
            return True

    return False


def _build_stateless_toolbutton_style(button_name: str, icon_path: str) -> dict:
    transparent_states = [
        "",
        ":checked",
        ":pressed",
        ":selected",
        ":focused",
        ":disabled",
        ":disabled:checked",
        ":disabled:selected",
        ":pressed:checked",
        ":pressed:selected",
    ]

    style = {
        f"Button.Image::{button_name}": {"image_url": icon_path},
    }

    for state in transparent_states:
        style[f"ToolButton::{button_name}{state}"] = {"background_color": 0x00000000}
        style[f"Button::{button_name}{state}"] = {"background_color": 0x00000000}

    return style


def _icon_folder() -> Path:
    ext_path = omni.kit.app.get_app().get_extension_manager().get_extension_path_by_module(__name__)
    return Path(ext_path) / "data" / "icons"


# ---------------------------------------------------------------------------
# Button groups
# ---------------------------------------------------------------------------

class PivotButtonGroup(WidgetGroup):
    _MENU_NAMES = ("Pivot", "Pivot Tool")
    _MENU_ROOT = "Tools"

    def __init__(self):
        super().__init__()
        self._icon_folder = _icon_folder()
        self._button = None

    def get_style(self):
        return _build_stateless_toolbutton_style("pivot_tool", f"{self._icon_folder}/pivot_location.svg")

    def create(self, default_size):
        self._button = ui.ToolButton(
            name="pivot_tool",
            width=default_size,
            height=default_size,
            tooltip="Pivot Tool",
            checked=False,
            mouse_pressed_fn=lambda _x, _y, button, _m: self._on_mouse_pressed(button),
            mouse_released_fn=lambda _x, _y, button, _m: self._on_mouse_released(button),
        )
        self._button.visible = False
        return {"pivot": self._button}

    def sync_visibility_state(self):
        if self._button is not None:
            self._button.visible = _has_menu_action(self._MENU_NAMES, self._MENU_ROOT)

    def _on_mouse_pressed(self, button: int):
        if button not in (0, 1):
            return

        if self._button is not None:
            self._button.checked = False

        try:
            if _execute_menu_action(self._MENU_NAMES, self._MENU_ROOT):
                return

            # Fallback to legacy context menu if menu action cannot be found.
            button_id = "multi_sel_pivot"
            context_menu = omni.kit.context_menu.get_instance()
            if context_menu:
                objects = {"widget_name": button_id, "main_toolbar": True}
                menu_list = omni.kit.context_menu.get_menu_dict(button_id, "omni.kit.manipulator.prim.core")
                context_menu.show_context_menu(button_id, objects, menu_list, 1, delegate=ui.MenuDelegate())
        finally:
            if self._button is not None:
                self._button.checked = False

    def _on_mouse_released(self, button: int):
        super()._on_mouse_released(button)
        if self._button is not None:
            self._button.checked = False
            try:
                self._button.selected = False
            except Exception:
                pass


class ArrayButtonGroup(WidgetGroup):
    _MENU_NAMES = ("Array", "Array Tool")
    _MENU_ROOT = "Tools"

    def __init__(self, settings: carb.settings.ISettings):
        super().__init__()
        self._icon_folder = _icon_folder()
        self._button = None
        self._settings = settings

    def get_style(self):
        return _build_stateless_toolbutton_style("array_tool", f"{self._icon_folder}/array_tool.svg")

    def create(self, default_size):
        self._button = ui.ToolButton(
            name="array_tool",
            width=default_size,
            height=default_size,
            tooltip="Array Tool",
            checked=False,
            mouse_pressed_fn=lambda _x, _y, button, _m: self._on_mouse_pressed(button),
            mouse_released_fn=lambda _x, _y, button, _m: self._on_mouse_released(button),
        )
        self._button.visible = False
        return {"array": self._button}

    def sync_visibility_state(self):
        if self._button is not None:
            self._button.visible = _has_menu_action(self._MENU_NAMES, self._MENU_ROOT)

    def _on_mouse_pressed(self, button: int):
        if button not in (0, 1):
            return

        if self._button is not None:
            self._button.checked = False

        try:
            if _execute_menu_action(self._MENU_NAMES, self._MENU_ROOT):
                return

            if self._show_array_context_menu():
                return

            self._execute_array_action()
        finally:
            if self._button is not None:
                self._button.checked = False

    def _on_mouse_released(self, button: int):
        super()._on_mouse_released(button)
        if self._button is not None:
            self._button.checked = False
            try:
                self._button.selected = False
            except Exception:
                pass

    @staticmethod
    def _show_array_context_menu() -> bool:
        context_menu = omni.kit.context_menu.get_instance()
        if not context_menu:
            return False

        candidates = [
            ("multi_sel_array", "omni.kit.manipulator.prim.core"),
            ("sel_array", "omni.kit.manipulator.prim"),
            ("array", "omni.kit.manipulator.prim.core"),
            ("array", "omni.kit.manipulator.prim"),
        ]

        for button_id, extension_id in candidates:
            menu_list = omni.kit.context_menu.get_menu_dict(button_id, extension_id)
            if not menu_list:
                continue

            objects = {"widget_name": button_id, "main_toolbar": True}
            context_menu.show_context_menu(button_id, objects, menu_list, 1, delegate=ui.MenuDelegate())
            return True

        return False

    def _execute_array_action(self):
        ext_id = self._settings.get_as_string("/exts/omni.quicksearch.ux/arrayAction/extension")
        action_id = self._settings.get_as_string("/exts/omni.quicksearch.ux/arrayAction/id")
        action = None

        if ext_id and action_id:
            action = omni.kit.actions.core.get_action_registry().get_action(ext_id, action_id)

        if action is None:
            action = self._find_array_action()

        if action is None:
            carb.log_warn(
                "[omni.quicksearch.ux] Array action not found. "
                "Configure /exts/omni.quicksearch.ux/arrayAction/extension and /id settings."
            )
            return

        action.execute()

    @staticmethod
    def _find_array_action():
        registry = omni.kit.actions.core.get_action_registry()
        if not registry:
            return None

        candidates = []
        for action in registry.get_all_actions():
            text = f"{action.extension_id}:{action.id}:{action.display_name}".lower()
            if "array" in text and ("tool" in text or "create" in text or "open" in text):
                candidates.append(action)

        if not candidates:
            return None

        candidates.sort(key=lambda act: ("tool" not in act.id.lower(), act.extension_id, act.id))
        chosen = candidates[0]
        carb.log_info(
            "[omni.quicksearch.ux] Using detected array action: "
            f"{chosen.extension_id}:{chosen.id} ({chosen.display_name})"
        )
        return chosen


class SceneOptimizerButtonGroup(WidgetGroup):
    _MENU_NAMES = ("Scene Optimizer",)
    _MENU_ROOT = "Window"

    def __init__(self):
        super().__init__()
        self._icon_folder = _icon_folder()
        self._button = None

    def get_style(self):
        return _build_stateless_toolbutton_style("scene_optimizer_tool", f"{self._icon_folder}/scene_optimizer.svg")

    def create(self, default_size):
        self._button = ui.ToolButton(
            name="scene_optimizer_tool",
            width=default_size,
            height=default_size,
            tooltip="Scene Optimizer",
            checked=False,
            mouse_pressed_fn=lambda _x, _y, button, _m: self._on_mouse_pressed(button),
            mouse_released_fn=lambda _x, _y, button, _m: self._on_mouse_released(button),
        )
        self._button.visible = False
        return {"scene_optimizer": self._button}

    def sync_visibility_state(self):
        if self._button is not None:
            self._button.visible = _has_menu_action(self._MENU_NAMES, self._MENU_ROOT)

    def _on_mouse_pressed(self, button: int):
        if button not in (0, 1):
            return

        if self._button is not None:
            self._button.checked = False

        try:
            _execute_menu_action(self._MENU_NAMES, self._MENU_ROOT)
        finally:
            if self._button is not None:
                self._button.checked = False

    def _on_mouse_released(self, button: int):
        super()._on_mouse_released(button)
        if self._button is not None:
            self._button.checked = False
            try:
                self._button.selected = False
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Coordinator  (used by Extension to manage all three groups as one unit)
# ---------------------------------------------------------------------------

class ToolbarButtonsManager:
    """Registers and manages the Pivot / Array / Scene Optimizer toolbar buttons."""

    _POLL_INTERVAL = 30    # ticks between visibility checks
    _POLL_BUDGET   = 1800  # stop polling once budget exhausted or all visible

    def __init__(self):
        self._pivot_group: PivotButtonGroup | None = None
        self._array_group: ArrayButtonGroup | None = None
        self._scene_optimizer_group: SceneOptimizerButtonGroup | None = None
        self._toolbar = None
        self._visibility_update_sub = None
        self._poll_tick = 0
        self._poll_budget = self._POLL_BUDGET

    def startup(self):
        try:
            import omni.kit.window.toolbar

            self._toolbar = omni.kit.window.toolbar.get_instance()
            if self._toolbar is None:
                carb.log_warn("[omni.quicksearch.ux] Main toolbar instance not available; toolbar buttons skipped.")
                return

            settings = carb.settings.get_settings()
            self._pivot_group = PivotButtonGroup()
            self._array_group = ArrayButtonGroup(settings)
            self._scene_optimizer_group = SceneOptimizerButtonGroup()

            self._toolbar.add_widget(self._pivot_group, 100)
            self._toolbar.add_widget(self._array_group, 101)
            self._toolbar.add_widget(self._scene_optimizer_group, 102)

            self._sync_visibility()
            self._visibility_update_sub = (
                omni.kit.app.get_app()
                .get_update_event_stream()
                .create_subscription_to_pop(
                    lambda _e: self._on_update(),
                    name="omni.quicksearch.ux.toolbar_visibility_sync",
                )
            )
            carb.log_info("[omni.quicksearch.ux] Toolbar buttons registered.")
        except Exception:
            carb.log_error("[omni.quicksearch.ux] Toolbar button startup failed:\n" + traceback.format_exc())

    def shutdown(self):
        try:
            self._visibility_update_sub = None

            for attr in ("_array_group", "_scene_optimizer_group", "_pivot_group"):
                group = getattr(self, attr)
                if self._toolbar and group:
                    self._toolbar.remove_widget(group)
                    group.clean()
                setattr(self, attr, None)

            self._toolbar = None
        except Exception:
            carb.log_warn("[omni.quicksearch.ux] Toolbar button shutdown failed:\n" + traceback.format_exc())

    # -- internal -------------------------------------------------------------

    def _on_update(self):
        self._poll_tick += 1
        self._poll_budget -= 1

        if self._poll_tick >= self._POLL_INTERVAL:
            self._poll_tick = 0
            self._sync_visibility()

        if self._all_resolved() or self._poll_budget <= 0:
            self._visibility_update_sub = None

    def _sync_visibility(self):
        for group in (self._pivot_group, self._array_group, self._scene_optimizer_group):
            if group is not None:
                group.sync_visibility_state()

    def _all_resolved(self) -> bool:
        return all(
            group is not None
            and getattr(group, "_button", None) is not None
            and bool(getattr(group._button, "visible", False))
            for group in (self._pivot_group, self._array_group, self._scene_optimizer_group)
        )
