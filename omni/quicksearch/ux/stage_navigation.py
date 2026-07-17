"""Stage hierarchy navigation and prim manipulation.

Provides the actions bound to the Stage-window hotkeys:

* copy selected prim paths to the clipboard
* toggle the active state of selected prims (with layer fallback)
* collapse / expand the currently selected prim
* select the next visible prim in the Stage tree
"""

import carb
import omni.kit.app
import omni.ui as ui
import omni.usd


class StageNavigationHandler:
    """Operates on the Stage widget and the current USD selection."""

    _IGNORED_TOP_LEVEL_PRIMS = {"Render"}
    _IGNORED_TOP_LEVEL_PREFIXES = ("OmniverseKit_",)

    def __init__(self):
        self._expanded_paths = set()
        self._collapsed_paths = set()

    def reset(self):
        self._expanded_paths = set()
        self._collapsed_paths = set()

    # -- public actions -------------------------------------------------------

    def copy_selected_prim_paths(self):
        selected_paths = self._get_selected_paths()
        if not selected_paths:
            carb.log_info("[QuickSearchUX] No selected prims to copy")
            return

        text = "\n".join(selected_paths)
        if self._copy_text_to_clipboard(text):
            carb.log_info(
                f"[QuickSearchUX] Copied {len(selected_paths)} selected prim path(s) to clipboard"
            )
        else:
            carb.log_warn("[QuickSearchUX] Could not copy selected prim paths to clipboard")

    def toggle_selected_prims_active_state(self):
        context = omni.usd.get_context()
        stage = context.get_stage()
        if not stage:
            return

        selected_paths = self._get_selected_paths()
        if not selected_paths:
            carb.log_info("[QuickSearchUX] No selected prims to toggle active state")
            return

        toggled_count = 0
        for path in selected_paths:
            prim = stage.GetPrimAtPath(path)
            try:
                if prim.IsValid():
                    prim.SetActive(not prim.IsActive())
                    toggled_count += 1
                    continue

                carb.log_info(
                    f"[QuickSearchUX] Prim {path} is not valid on composed stage, trying layer fallback"
                )
                if self._set_inactive_prim_active(stage, path):
                    carb.log_info(f"[QuickSearchUX] Reactivated prim via layer fallback: {path}")
                    toggled_count += 1
                else:
                    carb.log_warn(f"[QuickSearchUX] Layer fallback could not find prim spec: {path}")
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not toggle prim active state for {path}: {exc}")

        if toggled_count:
            carb.log_info(
                f"[QuickSearchUX] Toggled active state for {toggled_count} selected prim(s)"
            )

    def collapse_current_hierarchy(self):
        stage_widget = self._get_stage_widget()
        selected_path = self._get_selected_path()
        if stage_widget and selected_path:
            stage_widget.collapse(selected_path)
            self._expanded_paths.discard(selected_path)
            self._collapsed_paths.add(selected_path)

    def expand_current_hierarchy(self):
        stage_widget = self._get_stage_widget()
        selected_path = self._get_selected_path()
        if stage_widget and selected_path:
            stage_widget.expand(selected_path)
            self._expanded_paths.add(selected_path)
            self._collapsed_paths.discard(selected_path)

    def select_next_visible_prim(self):
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

    # -- stage widget / selection helpers -------------------------------------

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
    def _get_selected_paths() -> list[str]:
        context = omni.usd.get_context()
        selected_paths = context.get_selection().get_selected_prim_paths()
        return [path for path in selected_paths if isinstance(path, str) and path]

    @staticmethod
    def _get_selected_path() -> str | None:
        selected_paths = StageNavigationHandler._get_selected_paths()
        return selected_paths[0] if selected_paths else None

    @staticmethod
    def _set_selected_path(path: str):
        selection = omni.usd.get_context().get_selection()
        try:
            selection.set_selected_prim_paths([path], False)
        except TypeError:
            selection.set_selected_prim_paths([path], True)

    @staticmethod
    def _copy_text_to_clipboard(text: str) -> bool:
        if not text:
            return False

        try:
            import omni.kit.clipboard

            copy_fn = getattr(omni.kit.clipboard, "copy", None)
            if callable(copy_fn):
                copy_fn(text)
                return True
        except Exception:
            pass

        try:
            app = omni.kit.app.get_app()
            for method_name in ("set_clipboard", "set_clipboard_text", "copy_to_clipboard"):
                method = getattr(app, method_name, None)
                if callable(method):
                    method(text)
                    return True
        except Exception:
            pass

        return False

    @staticmethod
    def _set_inactive_prim_active(stage, path: str) -> bool:
        layers = []

        for getter in (
            lambda: stage.GetEditTarget().GetLayer(),
            stage.GetSessionLayer,
            stage.GetRootLayer,
        ):
            try:
                layer = getter()
                if layer:
                    layers.append(layer)
            except Exception:
                pass

        seen = set()
        for layer in layers:
            layer_id = id(layer)
            if layer_id in seen:
                continue
            seen.add(layer_id)
            try:
                layer_name = (
                    getattr(layer, "identifier", None)
                    or getattr(layer, "realPath", None)
                    or "<anonymous layer>"
                )
                prim_spec = layer.GetPrimAtPath(path)
                if prim_spec is None:
                    carb.log_info(f"[QuickSearchUX] Prim spec {path} not found in layer: {layer_name}")
                    continue
                prim_spec.active = True
                carb.log_info(f"[QuickSearchUX] Prim spec {path} set active in layer: {layer_name}")
                return True
            except Exception:
                continue

        return False

    # -- visible-path discovery ----------------------------------------------

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

            visible_paths.append(prim.GetPath().pathString)

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

    @staticmethod
    def _expansion_targets(stage_widget):
        targets = [stage_widget]

        attr_names = (
            "tree_view", "_tree_view", "tree", "_tree",
            "model", "_model", "delegate", "_delegate",
            "stage_tree", "_stage_tree", "stage_model", "_stage_model",
        )
        for attr_name in attr_names:
            target = getattr(stage_widget, attr_name, None)
            if target is not None:
                targets.append(target)

        for getter_name in ("get_tree_view", "get_model"):
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
            "visible_prim_paths", "visible_paths", "flattened_prim_paths",
            "displayed_prim_paths", "shown_prim_paths",
        )
        method_names = (
            "get_visible_prim_paths", "get_visible_paths", "get_flattened_prim_paths",
            "get_displayed_prim_paths", "get_shown_prim_paths",
            "get_visible_items", "get_displayed_items",
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
