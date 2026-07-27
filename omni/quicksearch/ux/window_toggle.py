"""Temporarily maximize the window under the mouse cursor and restore it.

Feature (bound to ``Shift+Space`` by :mod:`~.hotkeys`):

* First press: the window currently under the mouse cursor is *undocked* and
  resized to fill the whole application window. The current workspace layout is
  remembered beforehand via :func:`omni.ui.Workspace.dump_workspace`.
* Second press (with a window maximized): the previously captured workspace
  layout is restored via :func:`omni.ui.Workspace.restore_workspace`, bringing
  every window - including the maximized one - back to where it was.

The window under the cursor is resolved with
``omni.kit.hotkeys.core.hovered_window.get_hovered_window`` when available, with
a small inline DPI-aware hit-test as a fallback.
"""

from typing import List, Optional

import carb
import omni.ui as ui


class WindowMaximizeToggle:
    """Owns the maximize/restore state for the ``Shift+Space`` shortcut."""

    def __init__(self):
        self._saved_workspace: Optional[List] = None
        self._maximized_title: Optional[str] = None

    # -- public API -----------------------------------------------------------

    def toggle(self):
        """Maximize the hovered window, or restore the layout if one is active."""
        if self._maximized_title is not None:
            self._restore()
        else:
            self._maximize_hovered()

    def reset(self):
        """Drop any pending state without touching the current layout."""
        self._saved_workspace = None
        self._maximized_title = None

    # -- maximize -------------------------------------------------------------

    def _maximize_hovered(self):
        window = self._get_hovered_window()
        if window is None:
            carb.log_info("[QuickSearchUX] No window under cursor to maximize")
            return

        try:
            self._saved_workspace = ui.Workspace.dump_workspace()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not dump workspace: {exc}")
            self._saved_workspace = None
            return

        title = window.title
        try:
            self._hide_other_windows(title)
            if window.docked:
                window.undock()
            self._fill_app_window(window)
            window.visible = True
            window.focus()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not maximize window '{title}': {exc}")
            # Best-effort rollback so we don't leave a half-maximized layout.
            self._restore()
            return

        self._maximized_title = title
        carb.log_info(f"[QuickSearchUX] Maximized window '{title}'")

    def _hide_other_windows(self, keep_title: str):
        for other in ui.Workspace.get_windows():
            if not isinstance(other, ui.Window):
                continue
            if other.title == keep_title:
                continue
            if not other.visible:
                continue
            try:
                other.visible = False
            except Exception:
                continue

    @staticmethod
    def _fill_app_window(window: ui.Window):
        width = ui.Workspace.get_main_window_width()
        height = ui.Workspace.get_main_window_height()
        window.position_x = 0
        window.position_y = 0
        window.width = width
        window.height = height

    # -- restore --------------------------------------------------------------

    def _restore(self):
        workspace = self._saved_workspace
        self._saved_workspace = None
        self._maximized_title = None
        if not workspace:
            return
        try:
            ui.Workspace.restore_workspace(workspace)
            carb.log_info("[QuickSearchUX] Restored previous workspace layout")
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not restore workspace: {exc}")

    # -- hovered-window resolution -------------------------------------------

    def _get_hovered_window(self) -> Optional[ui.Window]:
        pos = self._cursor_position_pixels()
        if pos is None:
            return None
        pos_x, pos_y = pos

        try:
            from omni.kit.hotkeys.core.hovered_window import get_hovered_window

            return get_hovered_window(pos_x, pos_y)
        except Exception:
            pass

        return self._hit_test_windows(pos_x, pos_y)

    @staticmethod
    def _cursor_position_pixels():
        try:
            import carb.windowing
            import omni.appwindow

            app_window = omni.appwindow.get_default_app_window()
            if not app_window:
                return None
            os_window = app_window.get_window()
            windowing = carb.windowing.acquire_windowing_interface()
            pos = windowing.get_cursor_position(os_window)
            return float(pos[0]), float(pos[1])
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not read cursor position: {exc}")
            return None

    @staticmethod
    def _hit_test_windows(pos_x: float, pos_y: float) -> Optional[ui.Window]:
        dpi = ui.Workspace.get_dpi_scale() or 1.0
        x = pos_x / dpi
        y = pos_y / dpi
        floating: Optional[ui.Window] = None
        docked: Optional[ui.Window] = None
        for window in ui.Workspace.get_windows():
            if not isinstance(window, ui.Window) or not window.visible:
                continue
            is_docked = window.docked or window.dock_id != 0
            if is_docked and not (
                isinstance(window, ui.ToolBar) or window.is_selected_in_dock()
            ):
                continue
            if (
                window.position_x < x < window.position_x + window.width
                and window.position_y < y < window.position_y + window.height
            ):
                if window.docked:
                    docked = docked or window
                else:
                    floating = floating or window
        return floating or docked
