import asyncio

import carb
import carb.input
import omni.ext
import omni.kit.app
import omni.ui as ui
import omni.usd

from omni.kit.window.quicksearch import QuickSearchRegistry
from omni.kit.window.quicksearch.quicksearch_window import QuickSearchWindow

from .model import UnifiedQuickSearchModel, set_menu_snapshot


ACTION_ID = "ShowUnifiedQuickSearch"
ACTION_DISPLAY_NAME = "Unified Quick Search"


class Extension(omni.ext.IExt):
    _IGNORED_TOP_LEVEL_PRIMS = {"Render"}
    _IGNORED_TOP_LEVEL_PREFIXES = ("OmniverseKit_",)

    def __init__(self):
        super().__init__()
        self._ext_id = None
        self._extension_name = None
        self._subscription = None
        self._window = None
        self._hotkey = None
        self._keyboard_sub_id = None
        self._keyboard = None
        self._input = None
        self._action_registry = None
        self._hotkey_registry = None
        self._snapshot_task = None
        self._expanded_paths = set()
        self._collapsed_paths = set()

    def on_startup(self, ext_id: str):
        self._ext_id = omni.ext.get_extension_name(ext_id)
        self._extension_name = self._ext_id
        self._expanded_paths = set()
        self._collapsed_paths = set()

        self._subscription = QuickSearchRegistry().register_quick_search_model(
            "Quick Search UX",
            UnifiedQuickSearchModel,
            None,
            priority=20,
            flat_search=True,
        )

        self._snapshot_task = asyncio.ensure_future(self._capture_menu_snapshot())
        self._register_hotkeys()
        carb.log_info("[QuickSearchUX] Registered unified quick-search provider")

    def on_shutdown(self):
        if self._snapshot_task and not self._snapshot_task.done():
            self._snapshot_task.cancel()
        self._snapshot_task = None
        self._deregister_hotkeys()
        self._subscription = None
        self._expanded_paths = set()
        self._collapsed_paths = set()
        if self._window:
            self._window.destroy()
            self._window = None
        carb.log_info("[QuickSearchUX] Unregistered unified quick-search provider")

    def show_window(self):
        self._capture_menu_snapshot_once()
        if not self._window:
            self._window = QuickSearchWindow()
        else:
            self._window.show()

    async def _capture_menu_snapshot(self):
        for _ in range(120):
            if self._capture_menu_snapshot_once():
                return
            await omni.kit.app.get_app().next_update_async()

        carb.log_warn("[QuickSearchUX] Could not capture menubar snapshot during startup")

    def _capture_menu_snapshot_once(self):
        try:
            from omni.kit.mainwindow import get_main_window

            main_window = get_main_window()
            if not main_window:
                return False

            menu_bar = main_window.get_main_menu_bar()
            menu_dict, trigger_map = self._capture_menu_bar(menu_bar)
            if not menu_dict:
                return False

            set_menu_snapshot(menu_dict, trigger_map)
            return True
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Menubar snapshot not ready yet: {exc}")
            return False

    def _capture_menu_bar(self, menu_bar):
        import re
        import omni.ui as ui

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

    def _register_hotkeys(self):
        self._register_ctrl_f_hotkey()
        self._register_stage_arrow_hotkeys()

    def _register_ctrl_f_hotkey(self):
        try:
            from omni.kit.actions.core import get_action_registry
            from omni.kit.hotkeys.core import KeyCombination, get_hotkey_registry

            key = KeyCombination(carb.input.KeyboardInput.F, carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL)
            if not key.as_string:
                raise ImportError

            self._action_registry = get_action_registry()
            self._action_registry.register_action(
                self._ext_id,
                ACTION_ID,
                lambda: self.show_window(),
                display_name=ACTION_DISPLAY_NAME,
                description="Open unified quick search for menu and stage actions",
                tag="Quick Search UX",
            )
            self._hotkey_registry = get_hotkey_registry()
            self._hotkey = self._hotkey_registry.register_hotkey(self._ext_id, key, self._ext_id, ACTION_ID)
        except ImportError:
            self._register_keyboard_fallback()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not register Ctrl+F hotkey: {exc}")
            self._register_keyboard_fallback()

    def _register_stage_arrow_hotkeys(self):
        try:
            from omni.kit.hotkeys.core import KeyCombination, filter, get_hotkey_registry

            hotkey_registry = get_hotkey_registry()
            action_registry = self._action_registry
            if action_registry is None:
                from omni.kit.actions.core import get_action_registry

                action_registry = get_action_registry()
                self._action_registry = action_registry

            actions = [
                (
                    "collapse_current_hierarchy",
                    "Stage->Collapse Current Hierarchy",
                    "Collapse current selected prim in Stage hierarchy.",
                    self._collapse_current_hierarchy,
                    carb.input.KeyboardInput.LEFT,
                    0,
                ),
                (
                    "expand_current_hierarchy",
                    "Stage->Expand Current Hierarchy",
                    "Expand current selected prim in Stage hierarchy.",
                    self._expand_current_hierarchy,
                    carb.input.KeyboardInput.RIGHT,
                    0,
                ),
                (
                    "select_next_visible_prim",
                    "Stage->Select Next Visible Prim",
                    "Select next visible prim in Stage hierarchy.",
                    self._select_next_visible_prim,
                    carb.input.KeyboardInput.DOWN,
                    0,
                ),
            ]

            hotkey_filter = filter.HotkeyFilter(windows=["Stage"])
            for action_id, display_name, description, fn, key, modifiers in actions:
                action_registry.register_action(
                    self._extension_name,
                    action_id,
                    fn,
                    display_name=display_name,
                    description=description,
                    tag="Quick Search UX",
                )
                hotkey_registry.register_hotkey(
                    self._extension_name,
                    KeyCombination(key, modifiers),
                    self._extension_name,
                    action_id,
                    filter=hotkey_filter,
                )
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not register Stage arrow hotkeys: {exc}")

    def _deregister_hotkeys(self):
        if self._hotkey_registry:
            try:
                self._hotkey_registry.deregister_all_hotkeys_for_extension(self._ext_id)
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not deregister hotkeys: {exc}")
            self._hotkey_registry = None
            self._hotkey = None
        if self._action_registry:
            try:
                self._action_registry.deregister_all_actions_for_extension(self._ext_id)
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not deregister actions: {exc}")
            self._action_registry = None
        if self._keyboard_sub_id is not None and self._input and self._keyboard:
            try:
                self._input.unsubscribe_to_keyboard_events(self._keyboard, self._keyboard_sub_id)
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not unsubscribe keyboard fallback: {exc}")
            self._keyboard_sub_id = None

    def _register_keyboard_fallback(self):
        if self._keyboard_sub_id is not None:
            return
        try:
            import omni.appwindow

            app_window = omni.appwindow.get_default_app_window()
            if not app_window:
                return
            self._keyboard = app_window.get_keyboard()
            self._input = carb.input.acquire_input_interface()
            self._keyboard_sub_id = self._input.subscribe_to_keyboard_events(self._keyboard, self._on_keyboard_event)
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not register keyboard fallback: {exc}")

    def _on_keyboard_event(self, event):
        if event.type == carb.input.KeyboardEventType.KEY_PRESS:
            is_ctrl_f = event.input == carb.input.KeyboardInput.F and bool(
                event.modifiers & carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL
            )
            if is_ctrl_f:
                self.show_window()
        return True

    @staticmethod
    def _get_stage_widget():
        stage_window = ui.Workspace.get_window("Stage")
        if not stage_window:
            return None
        get_widget = getattr(stage_window, "get_widget", None)
        if not callable(get_widget):
            return None
        return get_widget()

    @staticmethod
    def _get_selected_path() -> str | None:
        context = omni.usd.get_context()
        selected_paths = context.get_selection().get_selected_prim_paths()
        if not selected_paths:
            return None
        return selected_paths[0]

    def _collapse_current_hierarchy(self):
        stage_widget = self._get_stage_widget()
        selected_path = self._get_selected_path()
        if stage_widget and selected_path:
            stage_widget.collapse(selected_path)
            self._expanded_paths.discard(selected_path)
            self._collapsed_paths.add(selected_path)

    def _expand_current_hierarchy(self):
        stage_widget = self._get_stage_widget()
        selected_path = self._get_selected_path()
        if stage_widget and selected_path:
            stage_widget.expand(selected_path)
            self._expanded_paths.add(selected_path)
            self._collapsed_paths.discard(selected_path)

    @staticmethod
    def _set_selected_path(path: str):
        selection = omni.usd.get_context().get_selection()
        try:
            selection.set_selected_prim_paths([path], False)
        except TypeError:
            selection.set_selected_prim_paths([path], True)

    @staticmethod
    def _to_path_string(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value

        path_string = getattr(value, "pathString", None)
        if isinstance(path_string, str):
            return path_string

        for attr_name in ("path", "prim_path", "usd_path", "_path", "_prim_path"):
            attr_value = getattr(value, attr_name, None)
            if isinstance(attr_value, str):
                return attr_value
            nested_path = getattr(attr_value, "pathString", None)
            if isinstance(nested_path, str):
                return nested_path

        get_path = getattr(value, "GetPath", None)
        if callable(get_path):
            try:
                path = get_path()
                if path is not None:
                    return getattr(path, "pathString", None)
            except Exception:
                pass

        return None

    def _coerce_path_list(self, stage, raw_values) -> list[str]:
        if raw_values is None:
            return []

        if isinstance(raw_values, (str, bytes)):
            raw_values = [raw_values]

        stack = list(raw_values) if isinstance(raw_values, (list, tuple, set)) else [raw_values]
        paths = []
        seen = set()

        while stack:
            value = stack.pop(0)

            if isinstance(value, dict):
                stack.extend(value.values())
                continue

            if isinstance(value, (list, tuple, set)):
                stack.extend(value)
                continue

            path = self._to_path_string(value)
            if not path or path in seen or not path.startswith("/"):
                continue

            prim = stage.GetPrimAtPath(path)
            if not prim or not prim.IsValid():
                continue

            seen.add(path)
            paths.append(path)

        return paths

    @staticmethod
    def _expansion_targets(stage_widget):
        targets = [stage_widget]

        attr_names = (
            "tree_view",
            "_tree_view",
            "tree",
            "_tree",
            "model",
            "_model",
            "delegate",
            "_delegate",
            "stage_tree",
            "_stage_tree",
            "stage_model",
            "_stage_model",
        )
        for attr_name in attr_names:
            target = getattr(stage_widget, attr_name, None)
            if target is not None:
                targets.append(target)

        getter_names = ("get_tree_view", "get_model")
        for getter_name in getter_names:
            getter = getattr(stage_widget, getter_name, None)
            if callable(getter):
                try:
                    target = getter()
                except Exception:
                    target = None
                if target is not None:
                    targets.append(target)

        unique_targets = []
        seen_ids = set()
        for target in targets:
            target_id = id(target)
            if target_id in seen_ids:
                continue
            seen_ids.add(target_id)
            unique_targets.append(target)

        return unique_targets

    def _is_path_expanded(self, stage_widget, prim) -> bool:
        prim_path = prim.GetPath()
        path = prim_path.pathString
        args = (path, prim_path, prim)

        for target in self._expansion_targets(stage_widget):
            for method_name in ("is_expanded", "is_item_expanded", "is_path_expanded"):
                method = getattr(target, method_name, None)
                if not callable(method):
                    continue
                for value in args:
                    try:
                        return bool(method(value))
                    except Exception:
                        pass

            for method_name in ("is_collapsed", "is_item_collapsed", "is_path_collapsed"):
                method = getattr(target, method_name, None)
                if not callable(method):
                    continue
                for value in args:
                    try:
                        return not bool(method(value))
                    except Exception:
                        pass

        if path in self._collapsed_paths:
            return False
        if path in self._expanded_paths:
            return True
        return True

    def _visible_paths_from_widget(self, stage, stage_widget) -> list[str]:
        attr_names = (
            "visible_prim_paths",
            "visible_paths",
            "flattened_prim_paths",
            "displayed_prim_paths",
            "shown_prim_paths",
        )
        method_names = (
            "get_visible_prim_paths",
            "get_visible_paths",
            "get_flattened_prim_paths",
            "get_displayed_prim_paths",
            "get_shown_prim_paths",
            "get_visible_items",
            "get_displayed_items",
        )

        for target in self._expansion_targets(stage_widget):
            for attr_name in attr_names:
                paths = self._coerce_path_list(stage, getattr(target, attr_name, None))
                if paths:
                    return paths

            for method_name in method_names:
                method = getattr(target, method_name, None)
                if not callable(method):
                    continue
                try:
                    raw_values = method()
                except Exception:
                    continue
                paths = self._coerce_path_list(stage, raw_values)
                if paths:
                    return paths

        return []

    def _visible_stage_prim_paths(self) -> list[str]:
        stage = omni.usd.get_context().get_stage()
        stage_widget = self._get_stage_widget()
        if not stage or not stage_widget:
            return []

        widget_paths = self._visible_paths_from_widget(stage, stage_widget)
        if widget_paths:
            return widget_paths

        visible_paths = []

        def recurse(prim):
            if not prim or not prim.IsValid():
                return

            path = prim.GetPath().pathString
            visible_paths.append(path)

            if not self._is_path_expanded(stage_widget, prim):
                return

            for child in prim.GetChildren():
                recurse(child)

        for prim in self._navigation_roots(stage):
            recurse(prim)

        return visible_paths

    def _navigation_roots(self, stage):
        roots = []
        for prim in stage.GetPseudoRoot().GetChildren():
            if not prim or not prim.IsValid():
                continue

            name = prim.GetName()
            if name in self._IGNORED_TOP_LEVEL_PRIMS:
                continue
            if any(name.startswith(prefix) for prefix in self._IGNORED_TOP_LEVEL_PREFIXES):
                continue

            roots.append(prim)
        return roots

    def _select_next_visible_prim(self):
        visible_paths = self._visible_stage_prim_paths()
        if not visible_paths:
            return

        selected_path = self._get_selected_path()
        if not selected_path:
            self._set_selected_path(visible_paths[0])
            return

        try:
            current_index = visible_paths.index(selected_path)
        except ValueError:
            self._set_selected_path(visible_paths[0])
            return

        next_index = current_index + 1
        if next_index >= len(visible_paths):
            return

        self._set_selected_path(visible_paths[next_index])
