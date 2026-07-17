"""Hotkey and action registration.

Registers:

* ``Ctrl+F``  -> open the unified quick-search window
* ``Ctrl+8``  -> trigger Layout > Quick Load
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
LAYOUT_QUICK_LOAD_PATH_CANDIDATES = (
    ("Layout", "Quick Load"),
    ("Quicklayout", "Quick Load"),
    ("Quicklayout", "Quicklayout Quick Load"),
)

COPY_SELECTED_PRIMS_ACTION_ID = "CopySelectedPrimPaths"
COPY_SELECTED_PRIMS_ACTION_DISPLAY_NAME = "Stage->Copy Selected Prim Paths"
TOGGLE_SELECTED_PRIMS_ACTIVE_STATE_ACTION_ID = "ToggleSelectedPrimActiveState"
TOGGLE_SELECTED_PRIMS_ACTIVE_STATE_ACTION_DISPLAY_NAME = "Stage->Toggle Selected Prim Active State"


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

        self._keyboard = None
        self._input = None
        self._keyboard_sub_id = None

    # -- lifecycle ------------------------------------------------------------

    def register(self):
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

            self._hotkey_registry = get_hotkey_registry()
            self._hotkey = self._hotkey_registry.register_hotkey(
                self._ext_id, key, self._ext_id, ACTION_ID
            )
            self._ctrl_f_hotkey_registered = self._hotkey is not None

            self._register_ctrl_8_hotkey(KeyCombination)

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

            has_ctrl = bool(event.modifiers & carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL)

            if use_ctrl_f_fallback and has_ctrl and event.input == carb.input.KeyboardInput.F:
                self._show_window()

            if use_ctrl_8_fallback and has_ctrl and self._is_digit_8_input(event.input):
                self.trigger_layout_quick_load()

            is_ctrl_shift_c = (
                event.input == carb.input.KeyboardInput.C
                and has_ctrl
                and bool(event.modifiers & carb.input.KEYBOARD_MODIFIER_FLAG_SHIFT)
            )
            if self._hotkey_fallback_mode and is_ctrl_shift_c:
                self._stage_nav.copy_selected_prim_paths()
        return True

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
