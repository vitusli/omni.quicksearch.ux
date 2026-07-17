"""Menu bar snapshot capture for the unified quick-search model.

Walks the main menu bar and records a nested dict of visible menu entries plus a
trigger map (path -> callable) that the search model uses to invoke actions.
"""

import re

import carb
import omni.kit.app
import omni.ui as ui

from .model import set_menu_snapshot


class MenuSnapshotCapture:
    """Captures the menubar structure and keeps a trigger map for later use."""

    def __init__(self):
        self.trigger_map = {}

    def capture_once(self) -> bool:
        try:
            from omni.kit.mainwindow import get_main_window

            main_window = get_main_window()
            if not main_window:
                return False

            menu_dict, trigger_map = self._capture_menu_bar(main_window.get_main_menu_bar())
            if not menu_dict:
                return False

            self.trigger_map = trigger_map or {}
            set_menu_snapshot(menu_dict, trigger_map)
            return True
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Menubar snapshot not ready yet: {exc}")
            return False

    async def capture_with_retry(self, attempts: int = 120):
        for _ in range(attempts):
            if self.capture_once():
                return
            await omni.kit.app.get_app().next_update_async()

        carb.log_warn("[QuickSearchUX] Could not capture menubar snapshot during startup")

    @staticmethod
    def _capture_menu_bar(menu_bar):
        menu_dict = {}
        trigger_map = {}

        def clean_name(name):
            return re.sub(r"[^\x00-\x7F]+", " ", str(name or "")).lstrip()

        def walk(menu_root, output, prefix):
            for menu_item in ui.Inspector.get_children(menu_root):
                if not getattr(menu_item, "enabled", True) or not getattr(menu_item, "visible", True):
                    continue
                if not isinstance(menu_item, (ui.Menu, ui.MenuItem)):
                    continue

                name = clean_name(getattr(menu_item, "text", ""))
                if not name:
                    continue

                path = (*prefix, name)
                if isinstance(menu_item, ui.Menu):
                    child_dict = {}
                    walk(menu_item, child_dict, path)
                    if child_dict:
                        output[name] = child_dict
                    elif menu_item.has_triggered_fn():
                        output.setdefault("_", []).append(name)
                        trigger_map[path] = menu_item.call_triggered_fn
                elif menu_item.has_triggered_fn():
                    output.setdefault("_", []).append(name)
                    trigger_map[path] = menu_item.call_triggered_fn

        walk(menu_bar, menu_dict, ())
        return menu_dict, trigger_map
