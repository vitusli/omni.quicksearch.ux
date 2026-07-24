"""Hotkey and action registration.

Registers:

* ``Ctrl+F``  -> open the unified quick-search window
* ``Ctrl+8``  -> trigger Layout > Quick Load
* ``Shift+Space`` -> toggle maximize/restore for the focused window
* Stage-window navigation/manipulation hotkeys (Left/Right/Down, Ctrl+Shift+C)
* A raw keyboard fallback used when the hotkeys core is unavailable and for the
  plain ``Backspace`` "toggle active state" shortcut in the Stage window.
"""

import carb
import carb.input
import omni.ui as ui

ACTION_ID = "ShowUnifiedQuickSearch"
ACTION_DISPLAY_NAME = "Unified Quick Search"

LAYOUT_QUICK_LOAD_ACTION_ID = "RunLayoutQuickLoadCtrl8"
LAYOUT_QUICK_LOAD_ACTION_DISPLAY_NAME = "Layout->Quick Load"
LAYOUT_QUICK_LOAD_ACTION_CANDIDATES = (
    ("isaacsim.app.setup", "layout_quick_load"),
    ("omni.kit.quicklayout", "quicklayout_quick_load"),
    ("omni.kit.quicklayout", "quick_load"),
)

WINDOW_MAXIMIZE_TOGGLE_ACTION_CANDIDATES = (
    ("omni.kit.window.workspace", "toggle_current_window_maximized"),
    ("omni.kit.window.workspace", "toggle_window_maximized"),
    ("omni.kit.window.workspace", "toggle_maximize_focused_window"),
    ("omni.kit.window.workspace", "toggle_focused_window_maximized"),
)

WINDOW_MAXIMIZE_TOGGLE_MENU_PATH_CANDIDATES = (
    ("Window", "Maximize Window"),
    ("Window", "Maximize Current Window"),
    ("Window", "Toggle Window Maximized"),
    ("Window", "Restore Window"),
)

TOGGLE_FOCUSED_WINDOW_MAXIMIZE_ACTION_ID = "ToggleFocusedWindowMaximize"
TOGGLE_FOCUSED_WINDOW_MAXIMIZE_ACTION_DISPLAY_NAME = "Toggle Focused Window Maximize"
LAYOUT_QUICK_LOAD_PATH_CANDIDATES = (
    ("Layout", "Quick Load"),
    ("Quicklayout", "Quick Load"),
    ("Quicklayout", "Quicklayout Quick Load"),
)

COPY_SELECTED_PRIMS_ACTION_ID = "CopySelectedPrimPaths"
COPY_SELECTED_PRIMS_ACTION_DISPLAY_NAME = "Stage->Copy Selected Prim Paths"
TOGGLE_SELECTED_PRIMS_ACTIVE_STATE_ACTION_ID = "ToggleSelectedPrimActiveState"
TOGGLE_SELECTED_PRIMS_ACTIVE_STATE_ACTION_DISPLAY_NAME = "Stage->Toggle Selected Prim Active State"

SHIFT_SPACE_IMPL_VERSION = "2026-07-24-redock-v2"


class HotkeyManager:
    """Owns action/hotkey registration and the keyboard-event fallback."""

    def __init__(self, ext_id: str, *, show_window, stage_nav, get_menu_trigger_map,
                 capture_menu_snapshot):
        self._ext_id = ext_id
        self._show_window = show_window
        self._stage_nav = stage_nav
        self._get_menu_trigger_map = get_menu_trigger_map
        self._capture_menu_snapshot = capture_menu_snapshot

        self._action_registry = None
        self._hotkey_registry = None
        self._hotkey = None
        self._hotkey_fallback_mode = False
        self._ctrl_f_hotkey_registered = False
        self._ctrl_8_hotkey_registered = False
        self._shift_space_hotkey_registered = False

        self._window_restore_states = {}
        self._window_toggle_action = None
        self._window_toggle_action_checked = False
        self._window_toggle_menu_trigger = None
        self._window_toggle_menu_path = None
        self._window_toggle_menu_checked = False
        self._last_window_toggle_source = None
        self._window_toggle_debug_dumped = False
        self._last_toggled_window_key = None
        self._last_toggled_window_ref = None

        self._keyboard = None
        self._input = None
        self._keyboard_sub_id = None

    # -- lifecycle ------------------------------------------------------------

    def register(self):
        carb.log_info(f"[QuickSearchUX][Shift+Space] Impl version: {SHIFT_SPACE_IMPL_VERSION}")
        self._register_ctrl_f_hotkey()
        self._register_stage_hotkeys()
        self._register_keyboard_fallback()

    def deregister(self):
        if self._hotkey_registry:
            try:
                self._hotkey_registry.deregister_all_hotkeys_for_extension(self._ext_id)
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not deregister hotkeys: {exc}")
            self._hotkey_registry = None
            self._hotkey = None
            self._ctrl_f_hotkey_registered = False
            self._ctrl_8_hotkey_registered = False
            self._shift_space_hotkey_registered = False
        self._window_toggle_action = None
        self._window_toggle_action_checked = False
        self._window_toggle_menu_trigger = None
        self._window_toggle_menu_path = None
        self._window_toggle_menu_checked = False
        self._last_window_toggle_source = None
        self._window_toggle_debug_dumped = False
        self._last_toggled_window_key = None
        self._last_toggled_window_ref = None
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

    # -- Ctrl+F / Ctrl+8 ------------------------------------------------------

    def _register_ctrl_f_hotkey(self):
        self._ctrl_f_hotkey_registered = False
        self._ctrl_8_hotkey_registered = False
        self._shift_space_hotkey_registered = False
        try:
            from omni.kit.actions.core import get_action_registry
            from omni.kit.hotkeys.core import KeyCombination, get_hotkey_registry

            key = KeyCombination(
                carb.input.KeyboardInput.F, carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL
            )
            if not key.as_string:
                raise ImportError

            self._action_registry = get_action_registry()
            self._action_registry.register_action(
                self._ext_id,
                ACTION_ID,
                lambda: self._show_window(),
                display_name=ACTION_DISPLAY_NAME,
                description="Open unified quick search for menu and stage actions",
                tag="Quick Search UX",
            )
            self._action_registry.register_action(
                self._ext_id,
                LAYOUT_QUICK_LOAD_ACTION_ID,
                self.trigger_layout_quick_load,
                display_name=LAYOUT_QUICK_LOAD_ACTION_DISPLAY_NAME,
                description="Run Layout > Quick Load from menu bar",
                tag="Quick Search UX",
            )
            self._action_registry.register_action(
                self._ext_id,
                TOGGLE_FOCUSED_WINDOW_MAXIMIZE_ACTION_ID,
                self.toggle_focused_window_maximize,
                display_name=TOGGLE_FOCUSED_WINDOW_MAXIMIZE_ACTION_DISPLAY_NAME,
                description="Toggle maximize/restore for the focused window",
                tag="Quick Search UX",
            )

            self._hotkey_registry = get_hotkey_registry()
            self._hotkey = self._hotkey_registry.register_hotkey(
                self._ext_id, key, self._ext_id, ACTION_ID
            )
            self._ctrl_f_hotkey_registered = self._hotkey is not None

            self._register_ctrl_8_hotkey(KeyCombination)
            self._register_shift_space_hotkey(KeyCombination)

            if not self._ctrl_f_hotkey_registered:
                raise RuntimeError("Ctrl+F hotkey registration returned no handle")
            self._hotkey_fallback_mode = False
        except ImportError:
            self._hotkey_fallback_mode = True
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not register Ctrl+F hotkey: {exc}")
            self._hotkey_fallback_mode = True

    def _register_ctrl_8_hotkey(self, KeyCombination):
        preferred_key = self._preferred_keyboard_input_for_digit_8()
        if preferred_key is None:
            carb.log_warn(
                "[QuickSearchUX] Could not resolve keyboard enum for digit 8, using event fallback only"
            )
            self._ctrl_8_hotkey_registered = False
            return
        try:
            ctrl_8_hotkey = self._hotkey_registry.register_hotkey(
                self._ext_id,
                KeyCombination(preferred_key, carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL),
                self._ext_id,
                LAYOUT_QUICK_LOAD_ACTION_ID,
            )
            self._ctrl_8_hotkey_registered = ctrl_8_hotkey is not None
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not register Ctrl+8 hotkey: {exc}")
            self._ctrl_8_hotkey_registered = False

    def _register_shift_space_hotkey(self, KeyCombination):
        preferred_key = self._preferred_keyboard_input_for_space()
        if preferred_key is None:
            carb.log_warn(
                "[QuickSearchUX] Could not resolve keyboard enum for Space, using event fallback only"
            )
            self._shift_space_hotkey_registered = False
            return
        try:
            shift_space_hotkey = self._hotkey_registry.register_hotkey(
                self._ext_id,
                KeyCombination(preferred_key, carb.input.KEYBOARD_MODIFIER_FLAG_SHIFT),
                self._ext_id,
                TOGGLE_FOCUSED_WINDOW_MAXIMIZE_ACTION_ID,
            )
            self._shift_space_hotkey_registered = shift_space_hotkey is not None
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not register Shift+Space hotkey: {exc}")
            self._shift_space_hotkey_registered = False

    # -- Stage hotkeys --------------------------------------------------------

    def _register_stage_hotkeys(self):
        try:
            from omni.kit.hotkeys.core import KeyCombination, filter, get_hotkey_registry

            hotkey_registry = get_hotkey_registry()
            if self._action_registry is None:
                from omni.kit.actions.core import get_action_registry

                self._action_registry = get_action_registry()
            action_registry = self._action_registry

            ctrl_shift = (
                carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL | carb.input.KEYBOARD_MODIFIER_FLAG_SHIFT
            )
            actions = [
                (
                    COPY_SELECTED_PRIMS_ACTION_ID,
                    COPY_SELECTED_PRIMS_ACTION_DISPLAY_NAME,
                    "Copy selected prim paths to clipboard.",
                    self._stage_nav.copy_selected_prim_paths,
                    carb.input.KeyboardInput.C,
                    ctrl_shift,
                ),
                (
                    TOGGLE_SELECTED_PRIMS_ACTIVE_STATE_ACTION_ID,
                    TOGGLE_SELECTED_PRIMS_ACTIVE_STATE_ACTION_DISPLAY_NAME,
                    "Toggle active state for selected prims.",
                    self._stage_nav.toggle_selected_prims_active_state,
                    None,
                    None,
                ),
                (
                    "collapse_current_hierarchy",
                    "Stage->Collapse Current Hierarchy",
                    "Collapse current selected prim in Stage hierarchy.",
                    self._stage_nav.collapse_current_hierarchy,
                    carb.input.KeyboardInput.LEFT,
                    0,
                ),
                (
                    "expand_current_hierarchy",
                    "Stage->Expand Current Hierarchy",
                    "Expand current selected prim in Stage hierarchy.",
                    self._stage_nav.expand_current_hierarchy,
                    carb.input.KeyboardInput.RIGHT,
                    0,
                ),
                (
                    "select_next_visible_prim",
                    "Stage->Select Next Visible Prim",
                    "Select next visible prim in Stage hierarchy.",
                    self._stage_nav.select_next_visible_prim,
                    carb.input.KeyboardInput.DOWN,
                    0,
                ),
            ]

            hotkey_filter = filter.HotkeyFilter(windows=["Stage"])
            for action_id, display_name, description, fn, key, modifiers in actions:
                action_registry.register_action(
                    self._ext_id,
                    action_id,
                    fn,
                    display_name=display_name,
                    description=description,
                    tag="Quick Search UX",
                )
                if key is not None and modifiers is not None:
                    hotkey_registry.register_hotkey(
                        self._ext_id,
                        KeyCombination(key, modifiers),
                        self._ext_id,
                        action_id,
                        filter=hotkey_filter,
                    )
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not register Stage arrow hotkeys: {exc}")

    # -- Layout Quick Load ----------------------------------------------------

    def trigger_layout_quick_load(self):
        action_registry = self._action_registry
        if action_registry is None:
            try:
                from omni.kit.actions.core import get_action_registry

                action_registry = get_action_registry()
                self._action_registry = action_registry
            except Exception:
                action_registry = None

        if action_registry is not None:
            for ext_id, action_id in LAYOUT_QUICK_LOAD_ACTION_CANDIDATES:
                try:
                    action_registry.execute_action(ext_id, action_id)
                    carb.log_info(f"[QuickSearchUX] Triggered action: {ext_id} {action_id}")
                    return
                except Exception:
                    continue

        trigger_map = self._get_menu_trigger_map()
        if not trigger_map:
            self._capture_menu_snapshot()
            trigger_map = self._get_menu_trigger_map()

        for path in LAYOUT_QUICK_LOAD_PATH_CANDIDATES:
            trigger_fn = trigger_map.get(path)
            if not trigger_fn:
                continue
            try:
                trigger_fn()
                carb.log_info(f"[QuickSearchUX] Triggered menu action: {' > '.join(path)}")
                return
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not trigger {' > '.join(path)}: {exc}")

        carb.log_warn("[QuickSearchUX] Quick Load action not found in known actions/paths")

    # -- focused-window maximize ----------------------------------------------

    def toggle_focused_window_maximize(self):
        self._last_window_toggle_source = None

        if self._trigger_builtin_window_maximize_toggle():
            carb.log_info(
                f"[QuickSearchUX][Shift+Space] Used built-in toggle source: {self._last_window_toggle_source}"
            )
            return

        window = self._get_focused_workspace_window()
        if window is None:
            carb.log_info("[QuickSearchUX] No focused window to maximize")
            return

        focused_window_key = self._window_identity(window)
        pending_restore_key = self._last_toggled_window_key
        if pending_restore_key and pending_restore_key in self._window_restore_states:
            if focused_window_key != pending_restore_key:
                pending_window = self._last_toggled_window_ref
                if pending_window is None or self._window_identity(pending_window) != pending_restore_key:
                    pending_window = self._find_window_by_key(pending_restore_key)
                if pending_window is not None:
                    restore_state = self._window_restore_states.get(pending_restore_key)
                    if restore_state is not None:
                        carb.log_info(
                            f"[QuickSearchUX][Shift+Space] Restoring previously maximized window key={pending_restore_key}"
                        )
                        if self._restore_window_state(pending_window, restore_state):
                            self._window_restore_states.pop(pending_restore_key, None)
                            self._last_toggled_window_key = None
                            self._last_toggled_window_ref = None
                            carb.log_info("[QuickSearchUX] Restored focused window state")
                            return
                        carb.log_warn(
                            f"[QuickSearchUX][Shift+Space] Could not restore previously maximized window key={pending_restore_key}"
                        )
                else:
                    carb.log_warn(
                        f"[QuickSearchUX][Shift+Space] Previously maximized window not found key={pending_restore_key}"
                    )

        if self._is_window_docked(window):
            self._debug_window_toggle_discovery_once(window)
            carb.log_warn(
                "[QuickSearchUX][Shift+Space] No native dock-aware maximize toggle available; "
                "using experimental undock/redock fallback"
            )

        carb.log_info(
            f"[QuickSearchUX][Shift+Space] Using geometry fallback for window: {self._describe_window(window)}"
        )

        window_key = self._window_identity(window)
        if window_key is None:
            carb.log_warn("[QuickSearchUX] Could not resolve focused window identity")
            return

        restore_state = self._window_restore_states.get(window_key)
        if restore_state is not None:
            carb.log_info(
                f"[QuickSearchUX][Shift+Space] Restoring cached window state key={window_key}"
            )
            if self._restore_window_state(window, restore_state):
                self._window_restore_states.pop(window_key, None)
                if self._last_toggled_window_key == window_key:
                    self._last_toggled_window_key = None
                    self._last_toggled_window_ref = None
                carb.log_info("[QuickSearchUX] Restored focused window state")
                return
            carb.log_warn("[QuickSearchUX] Could not restore focused window state")
            return

        if self._maximize_window(window, window_key):
            carb.log_info("[QuickSearchUX] Maximized focused window")
            return

        carb.log_warn("[QuickSearchUX] Could not maximize focused window")

    def _trigger_builtin_window_maximize_toggle(self) -> bool:
        action_registry = self._resolve_action_registry()
        if action_registry is None:
            carb.log_info("[QuickSearchUX][Shift+Space] Action registry unavailable")

        if not self._window_toggle_action_checked:
            self._window_toggle_action_checked = True
            carb.log_info("[QuickSearchUX][Shift+Space] Resolving built-in maximize toggle action...")
            if action_registry is not None:
                for ext_id, action_id in WINDOW_MAXIMIZE_TOGGLE_ACTION_CANDIDATES:
                    try:
                        action = action_registry.get_action(ext_id, action_id)
                    except Exception:
                        action = None
                    if action is not None:
                        self._window_toggle_action = (ext_id, action_id)
                        carb.log_info(
                            f"[QuickSearchUX][Shift+Space] Found action candidate: {ext_id}/{action_id}"
                        )
                        break
                    carb.log_info(
                        f"[QuickSearchUX][Shift+Space] Missing action candidate: {ext_id}/{action_id}"
                    )

            if self._window_toggle_action is None:
                carb.log_info("[QuickSearchUX][Shift+Space] No built-in action candidate resolved")

        if self._window_toggle_action is not None and action_registry is not None:
            ext_id, action_id = self._window_toggle_action
            try:
                action_registry.execute_action(ext_id, action_id)
                self._last_window_toggle_source = f"action:{ext_id}/{action_id}"
                return True
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Built-in maximize toggle failed: {exc}")

        if not self._window_toggle_menu_checked:
            self._window_toggle_menu_checked = True
            self._window_toggle_menu_trigger = self._resolve_window_maximize_menu_trigger()

        if callable(self._window_toggle_menu_trigger):
            try:
                self._window_toggle_menu_trigger()
                self._last_window_toggle_source = (
                    f"menu:{' > '.join(self._window_toggle_menu_path)}"
                    if self._window_toggle_menu_path
                    else "menu:<unknown>"
                )
                return True
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Menu maximize toggle failed: {exc}")

        return False

    def _resolve_action_registry(self):
        action_registry = self._action_registry
        if action_registry is not None:
            return action_registry

        try:
            from omni.kit.actions.core import get_action_registry

            action_registry = get_action_registry()
            self._action_registry = action_registry
            return action_registry
        except Exception:
            return None

    def _resolve_window_maximize_menu_trigger(self):
        trigger_map = self._get_menu_trigger_map()
        if not trigger_map:
            self._capture_menu_snapshot()
            trigger_map = self._get_menu_trigger_map()
        if not trigger_map:
            carb.log_info("[QuickSearchUX][Shift+Space] No menu snapshot available for maximize toggle")
            return None

        for path in WINDOW_MAXIMIZE_TOGGLE_MENU_PATH_CANDIDATES:
            trigger_fn = trigger_map.get(path)
            if callable(trigger_fn):
                self._window_toggle_menu_path = path
                carb.log_info(
                    f"[QuickSearchUX][Shift+Space] Selected menu toggle path: {' > '.join(path)}"
                )
                return trigger_fn

        matched_paths = []
        for path, trigger_fn in trigger_map.items():
            if not callable(trigger_fn):
                continue
            if not isinstance(path, tuple) or not path:
                continue
            label = str(path[-1]).strip().lower()
            if "maximize" not in label and "restore" not in label:
                continue
            if "all" in label:
                continue
            if "window" in label or "current" in label or "focused" in label:
                matched_paths.append(path)
                self._window_toggle_menu_path = path
                carb.log_info(
                    f"[QuickSearchUX][Shift+Space] Selected fuzzy menu toggle path: {' > '.join(path)}"
                )
                return trigger_fn

        if matched_paths:
            carb.log_info(
                "[QuickSearchUX][Shift+Space] Fuzzy matches seen: "
                + ", ".join(" > ".join(path) for path in matched_paths)
            )

        self._log_window_menu_candidates(trigger_map)
        carb.log_info("[QuickSearchUX][Shift+Space] No suitable maximize/restore menu entry found")

        return None

    def _debug_window_toggle_discovery_once(self, focused_window):
        if self._window_toggle_debug_dumped:
            return
        self._window_toggle_debug_dumped = True

        self._log_action_registry_maximize_candidates()
        self._log_workspace_method_candidates()
        self._log_window_method_candidates(focused_window)

    def _log_action_registry_maximize_candidates(self):
        action_registry = self._resolve_action_registry()
        if action_registry is None:
            carb.log_info("[QuickSearchUX][Shift+Space] Action discovery skipped: registry unavailable")
            return

        get_actions = getattr(action_registry, "get_actions", None)
        if not callable(get_actions):
            carb.log_info("[QuickSearchUX][Shift+Space] Action discovery skipped: get_actions() unavailable")
            return

        try:
            actions = get_actions()
        except Exception as exc:
            carb.log_info(f"[QuickSearchUX][Shift+Space] Action discovery failed: {exc}")
            return

        keywords = ("max", "maximize", "restore", "window", "dock", "tab")
        matches = []
        for item in actions or []:
            ext_id = None
            action_id = None

            if isinstance(item, tuple) and len(item) >= 2:
                ext_id, action_id = item[0], item[1]
            else:
                ext_id = getattr(item, "extension_id", None) or getattr(item, "ext_id", None)
                action_id = getattr(item, "action_id", None) or getattr(item, "id", None)

            if not ext_id or not action_id:
                continue

            text = f"{ext_id}/{action_id}".lower()
            if any(keyword in text for keyword in keywords):
                matches.append(f"{ext_id}/{action_id}")

        if not matches:
            carb.log_info("[QuickSearchUX][Shift+Space] Action discovery: no maximize-related actions found")
            return

        carb.log_info(f"[QuickSearchUX][Shift+Space] Action discovery found {len(matches)} candidate(s)")
        for candidate in matches[:80]:
            carb.log_info(f"[QuickSearchUX][Shift+Space] Action candidate: {candidate}")
        if len(matches) > 80:
            carb.log_info(
                f"[QuickSearchUX][Shift+Space] Action candidate list truncated (+{len(matches) - 80} more)"
            )

    @staticmethod
    def _log_workspace_method_candidates():
        try:
            methods = []
            keywords = ("max", "restore", "focus", "active", "window", "dock", "tab")
            for name in dir(ui.Workspace):
                lowered = str(name).lower()
                if any(keyword in lowered for keyword in keywords):
                    methods.append(name)
            if not methods:
                carb.log_info("[QuickSearchUX][Shift+Space] Workspace method discovery: no candidates")
                return
            carb.log_info(
                "[QuickSearchUX][Shift+Space] Workspace method candidates: " + ", ".join(sorted(methods)[:120])
            )
        except Exception:
            pass

    @staticmethod
    def _log_window_method_candidates(window):
        if window is None:
            return
        try:
            methods = []
            keywords = ("max", "restore", "dock", "tab", "float", "external", "focus", "position", "size")
            for name in dir(window):
                lowered = str(name).lower()
                if any(keyword in lowered for keyword in keywords):
                    methods.append(name)
            if not methods:
                carb.log_info("[QuickSearchUX][Shift+Space] Focused-window method discovery: no candidates")
                return
            carb.log_info(
                "[QuickSearchUX][Shift+Space] Focused-window method candidates: "
                + ", ".join(sorted(methods)[:160])
            )
        except Exception:
            pass

    @staticmethod
    def _log_window_menu_candidates(trigger_map):
        try:
            candidates = []
            for path, trigger_fn in trigger_map.items():
                if not callable(trigger_fn) or not isinstance(path, tuple) or not path:
                    continue
                if str(path[0]).strip().lower() != "window":
                    continue

                label = " > ".join(str(part) for part in path)
                normalized = label.lower()
                if any(token in normalized for token in ("maximize", "restore", "dock", "panel", "tab", "window")):
                    candidates.append(label)

            if not candidates:
                carb.log_info("[QuickSearchUX][Shift+Space] Window menu candidates: <none>")
                return

            carb.log_info(f"[QuickSearchUX][Shift+Space] Window menu candidates count: {len(candidates)}")
            for candidate in candidates[:120]:
                carb.log_info(f"[QuickSearchUX][Shift+Space] Window menu candidate: {candidate}")
            if len(candidates) > 120:
                carb.log_info(
                    f"[QuickSearchUX][Shift+Space] Window menu candidate list truncated (+{len(candidates) - 120} more)"
                )
        except Exception:
            pass

    # -- keyboard fallback ----------------------------------------------------

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
            self._keyboard_sub_id = self._input.subscribe_to_keyboard_events(
                self._keyboard, self._on_keyboard_event
            )
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not register keyboard fallback: {exc}")

    def _on_keyboard_event(self, event):
        if event.type == carb.input.KeyboardEventType.KEY_PRESS:
            if self._is_plain_stage_backspace(event):
                self._stage_nav.toggle_selected_prims_active_state()

            use_ctrl_f_fallback = self._hotkey_fallback_mode or not self._ctrl_f_hotkey_registered
            use_ctrl_8_fallback = self._hotkey_fallback_mode or not self._ctrl_8_hotkey_registered
            use_shift_space_fallback = (
                self._hotkey_fallback_mode or not self._shift_space_hotkey_registered
            )

            has_ctrl = bool(event.modifiers & carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL)
            has_shift = bool(event.modifiers & carb.input.KEYBOARD_MODIFIER_FLAG_SHIFT)
            has_alt = bool(event.modifiers & carb.input.KEYBOARD_MODIFIER_FLAG_ALT)

            if use_ctrl_f_fallback and has_ctrl and event.input == carb.input.KeyboardInput.F:
                self._show_window()

            if use_ctrl_8_fallback and has_ctrl and self._is_digit_8_input(event.input):
                self.trigger_layout_quick_load()

            if use_shift_space_fallback and has_shift and not has_ctrl and not has_alt:
                if self._is_space_input(event.input):
                    self.toggle_focused_window_maximize()

            is_ctrl_shift_c = (
                event.input == carb.input.KeyboardInput.C
                and has_ctrl
                and bool(event.modifiers & carb.input.KEYBOARD_MODIFIER_FLAG_SHIFT)
            )
            if self._hotkey_fallback_mode and is_ctrl_shift_c:
                self._stage_nav.copy_selected_prim_paths()
        return True

    def _maximize_window(self, window, window_key) -> bool:
        restore_state = self._capture_window_restore_state(window, window_key)
        rect = restore_state.get("rect")
        if rect is None:
            return False

        if restore_state.get("was_docked"):
            undock_fn = getattr(window, "undock", None)
            if callable(undock_fn):
                try:
                    undock_fn()
                except Exception:
                    pass

        if self._set_window_maximized_state(window, True):
            self._window_restore_states = {window_key: restore_state}
            self._last_toggled_window_key = window_key
            self._last_toggled_window_ref = window
            return True

        viewport_rect = self._default_maximized_rect()
        if viewport_rect is None:
            return False

        if self._write_window_rect(window, viewport_rect):
            self._window_restore_states = {window_key: restore_state}
            self._last_toggled_window_key = window_key
            self._last_toggled_window_ref = window
            return True

        return False

    def _restore_window_state(self, window, restore_state) -> bool:
        rect = restore_state.get("rect")
        was_docked = bool(restore_state.get("was_docked"))

        if was_docked and self._redock_window(window, restore_state):
            return True

        if rect is None:
            return False

        if self._set_window_maximized_state(window, False):
            return self._write_window_rect(window, rect)
        return self._write_window_rect(window, rect)

    def _capture_window_restore_state(self, window, window_key):
        dock_id = getattr(window, "dock_id", None)
        selected_in_dock = getattr(window, "selected_in_dock", None)
        state = {
            "rect": self._read_window_rect(window),
            "was_docked": self._is_window_docked(window),
            "dock_id": dock_id,
            "dock_order": getattr(window, "dock_order", None),
            "selected_in_dock": bool(selected_in_dock) if selected_in_dock is not None else None,
            "anchor_key": None,
            "window_key": window_key,
        }

        if state["was_docked"]:
            for sibling in self._list_workspace_windows():
                if sibling is window:
                    continue
                if getattr(sibling, "dock_id", None) != dock_id:
                    continue
                state["anchor_key"] = self._window_identity(sibling)
                break

        return state

    def _redock_window(self, window, restore_state) -> bool:
        if self._is_window_docked(window):
            return True

        anchor_window = self._find_window_by_key(restore_state.get("anchor_key"))
        dock_order = restore_state.get("dock_order")
        dock_id = restore_state.get("dock_id")

        if anchor_window is not None:
            for method_name in ("dock_in_window", "dock_in"):
                if self._try_method_arg_candidates(
                    window,
                    method_name,
                    (
                        (anchor_window,),
                        (anchor_window, dock_order),
                    ),
                ):
                    self._restore_selected_in_dock(window, restore_state)
                    return self._is_window_docked(window)

        if dock_id is not None:
            if self._try_method_arg_candidates(
                window,
                "dock_in",
                (
                    (dock_id,),
                    (dock_id, dock_order),
                ),
            ):
                self._restore_selected_in_dock(window, restore_state)
                return self._is_window_docked(window)

        return False

    def _restore_selected_in_dock(self, window, restore_state):
        selected = restore_state.get("selected_in_dock")
        if selected is None:
            return
        if hasattr(window, "selected_in_dock"):
            try:
                setattr(window, "selected_in_dock", bool(selected))
            except Exception:
                pass

    @staticmethod
    def _try_method_arg_candidates(target, method_name, arg_candidates) -> bool:
        method = getattr(target, method_name, None)
        if not callable(method):
            return False

        for args in arg_candidates:
            if args is None:
                continue
            filtered_args = tuple(arg for arg in args if arg is not None)
            try:
                method(*filtered_args)
                return True
            except Exception:
                continue
        return False

    def _find_window_by_key(self, key):
        if not key:
            return None
        for window in self._list_workspace_windows():
            if self._window_identity(window) == key:
                return window
        return None

    @staticmethod
    def _list_workspace_windows():
        get_windows = getattr(ui.Workspace, "get_windows", None)
        if not callable(get_windows):
            return []
        try:
            windows = get_windows()
        except Exception:
            return []
        if isinstance(windows, dict):
            return list(windows.values())
        return list(windows or [])

    @staticmethod
    def _window_identity(window):
        for attr_name in ("name", "title", "id", "window_id"):
            value = getattr(window, attr_name, None)
            if isinstance(value, str) and value:
                return f"{attr_name}:{value}"
            if isinstance(value, int):
                return f"{attr_name}:{value}"
        return str(id(window))

    @staticmethod
    def _get_focused_workspace_window():
        for getter_name in ("get_focused_window", "get_active_window", "get_window_in_focus"):
            getter = getattr(ui.Workspace, getter_name, None)
            if not callable(getter):
                continue
            try:
                window = getter()
            except Exception:
                window = None
            if window is not None:
                return window

        get_windows = getattr(ui.Workspace, "get_windows", None)
        if not callable(get_windows):
            return None

        try:
            windows = get_windows()
        except Exception:
            return None

        if isinstance(windows, dict):
            windows = list(windows.values())

        for window in windows or []:
            focused = getattr(window, "focused", None)
            if focused is not None and bool(focused):
                return window

            has_focus_fn = getattr(window, "has_focus", None)
            if callable(has_focus_fn):
                try:
                    if has_focus_fn():
                        return window
                except Exception:
                    pass

        return None

    @staticmethod
    def _set_window_maximized_state(window, maximized: bool) -> bool:
        for method_name in ("set_maximized", "set_is_maximized"):
            method = getattr(window, method_name, None)
            if callable(method):
                try:
                    method(bool(maximized))
                    return True
                except Exception:
                    pass

        for method_name in ("maximize", "restore"):
            method = getattr(window, method_name, None)
            if callable(method):
                if (method_name == "maximize" and maximized) or (method_name == "restore" and not maximized):
                    try:
                        method()
                        return True
                    except Exception:
                        pass

        for attr_name in ("maximized", "is_maximized"):
            if not hasattr(window, attr_name):
                continue
            try:
                setattr(window, attr_name, bool(maximized))
                return True
            except Exception:
                pass

        return False

    @staticmethod
    def _read_window_rect(window):
        width = HotkeyManager._read_numeric(window, "width", getter_names=("get_width",))
        height = HotkeyManager._read_numeric(window, "height", getter_names=("get_height",))
        x = HotkeyManager._read_numeric(
            window,
            "position_x",
            fallback_attr_names=("x", "left"),
            getter_names=("get_position_x", "get_x", "get_left"),
        )
        y = HotkeyManager._read_numeric(
            window,
            "position_y",
            fallback_attr_names=("y", "top"),
            getter_names=("get_position_y", "get_y", "get_top"),
        )

        if width is None or height is None:
            return None
        if x is None:
            x = 0
        if y is None:
            y = 0
        return int(x), int(y), int(width), int(height)

    @staticmethod
    def _write_window_rect(window, rect) -> bool:
        x, y, width, height = rect
        size_applied = False
        pos_applied = False

        set_size = getattr(window, "set_size", None)
        if callable(set_size):
            try:
                set_size(int(width), int(height))
                size_applied = True
            except Exception:
                pass

        set_position = getattr(window, "set_position", None)
        if callable(set_position):
            try:
                set_position(int(x), int(y))
                pos_applied = True
            except Exception:
                pass

        if not size_applied:
            width_ok = HotkeyManager._write_numeric(window, int(width), "width", setter_names=("set_width",))
            height_ok = HotkeyManager._write_numeric(
                window,
                int(height),
                "height",
                setter_names=("set_height",),
            )
            size_applied = width_ok and height_ok

        if not pos_applied:
            x_ok = HotkeyManager._write_numeric(
                window,
                int(x),
                "position_x",
                fallback_attr_names=("x", "left"),
                setter_names=("set_position_x", "set_x", "set_left"),
            )
            y_ok = HotkeyManager._write_numeric(
                window,
                int(y),
                "position_y",
                fallback_attr_names=("y", "top"),
                setter_names=("set_position_y", "set_y", "set_top"),
            )
            pos_applied = x_ok and y_ok

        return size_applied and pos_applied

    @staticmethod
    def _default_maximized_rect():
        try:
            import omni.appwindow

            app_window = omni.appwindow.get_default_app_window()
            if not app_window:
                return None

            width = None
            height = None
            for getter_name in ("get_width", "get_client_width"):
                getter = getattr(app_window, getter_name, None)
                if callable(getter):
                    try:
                        width = int(getter())
                        break
                    except Exception:
                        pass

            for getter_name in ("get_height", "get_client_height"):
                getter = getattr(app_window, getter_name, None)
                if callable(getter):
                    try:
                        height = int(getter())
                        break
                    except Exception:
                        pass

            if not width or not height:
                return None

            return 0, 0, width, height
        except Exception:
            return None

    @staticmethod
    def _describe_window(window) -> str:
        name = getattr(window, "name", None) or "<no-name>"
        title = getattr(window, "title", None) or "<no-title>"
        focused = getattr(window, "focused", None)
        docked = getattr(window, "docked", None)
        visible = getattr(window, "visible", None)
        return (
            f"name={name!r}, title={title!r}, focused={focused}, "
            f"docked={docked}, visible={visible}, id={id(window)}"
        )

    @staticmethod
    def _is_window_docked(window) -> bool:
        docked = getattr(window, "docked", None)
        if docked is not None:
            return bool(docked)

        is_docked_fn = getattr(window, "is_docked", None)
        if callable(is_docked_fn):
            try:
                return bool(is_docked_fn())
            except Exception:
                pass

        return False

    @staticmethod
    def _read_numeric(target, attr_name, fallback_attr_names=(), getter_names=()):
        value = getattr(target, attr_name, None)
        if isinstance(value, (int, float)):
            return int(value)

        for fallback_attr in fallback_attr_names:
            value = getattr(target, fallback_attr, None)
            if isinstance(value, (int, float)):
                return int(value)

        for getter_name in getter_names:
            getter = getattr(target, getter_name, None)
            if not callable(getter):
                continue
            try:
                value = getter()
            except Exception:
                continue
            if isinstance(value, (int, float)):
                return int(value)

        return None

    @staticmethod
    def _write_numeric(target, value: int, attr_name, fallback_attr_names=(), setter_names=()):
        for setter_name in setter_names:
            setter = getattr(target, setter_name, None)
            if not callable(setter):
                continue
            try:
                setter(int(value))
                return True
            except Exception:
                pass

        if hasattr(target, attr_name):
            try:
                setattr(target, attr_name, int(value))
                return True
            except Exception:
                pass

        for fallback_attr in fallback_attr_names:
            if not hasattr(target, fallback_attr):
                continue
            try:
                setattr(target, fallback_attr, int(value))
                return True
            except Exception:
                pass

        return False

    @staticmethod
    def _is_plain_stage_backspace(event) -> bool:
        if event.input != carb.input.KeyboardInput.BACKSPACE:
            return False
        if int(getattr(event, "modifiers", 0)) != 0:
            return False

        stage_window = ui.Workspace.get_window("Stage")
        if not stage_window:
            return False

        has_focus = getattr(stage_window, "focused", None)
        if has_focus is None:
            has_focus_fn = getattr(stage_window, "has_focus", None)
            has_focus = bool(has_focus_fn()) if callable(has_focus_fn) else True
        return bool(has_focus)

    # -- digit-8 key resolution ----------------------------------------------

    _DIGIT_8_KEY_NAMES = ("KEY_8", "NUMPAD_8", "NUM_8", "KP_8", "D8", "_8")

    @classmethod
    def _keyboard_inputs_for_digit_8(cls) -> list:
        values = []
        seen = set()
        for name in cls._DIGIT_8_KEY_NAMES:
            key = getattr(carb.input.KeyboardInput, name, None)
            if key is None:
                continue
            key_id = int(key)
            if key_id in seen:
                continue
            seen.add(key_id)
            values.append(key)
        return values

    @classmethod
    def _preferred_keyboard_input_for_digit_8(cls):
        for name in cls._DIGIT_8_KEY_NAMES:
            key = getattr(carb.input.KeyboardInput, name, None)
            if key is not None:
                return key
        return None

    @classmethod
    def _is_digit_8_input(cls, key_input) -> bool:
        for known_key in cls._keyboard_inputs_for_digit_8():
            if key_input == known_key:
                return True

        key_name = getattr(key_input, "name", "") or str(key_input)
        normalized = str(key_name).upper().replace(" ", "")
        if "F8" in normalized:
            return False
        return normalized.endswith("_8") or (
            normalized.endswith("8")
            and any(token in normalized for token in ("KEY", "NUM", "NUMPAD", "KP", "D8"))
        )

    # -- space key resolution -------------------------------------------------

    _SPACE_KEY_NAMES = ("SPACE", "SPACEBAR", "KEY_SPACE")

    @classmethod
    def _keyboard_inputs_for_space(cls) -> list:
        values = []
        seen = set()
        for name in cls._SPACE_KEY_NAMES:
            key = getattr(carb.input.KeyboardInput, name, None)
            if key is None:
                continue
            key_id = int(key)
            if key_id in seen:
                continue
            seen.add(key_id)
            values.append(key)
        return values

    @classmethod
    def _preferred_keyboard_input_for_space(cls):
        for name in cls._SPACE_KEY_NAMES:
            key = getattr(carb.input.KeyboardInput, name, None)
            if key is not None:
                return key
        return None

    @classmethod
    def _is_space_input(cls, key_input) -> bool:
        for known_key in cls._keyboard_inputs_for_space():
            if key_input == known_key:
                return True

        key_name = getattr(key_input, "name", "") or str(key_input)
        normalized = str(key_name).upper().replace(" ", "")
        return "SPACE" in normalized
