"""Temporarily maximize the window under the mouse cursor and restore it.

Feature (bound to ``Shift+Escape`` by :mod:`~.hotkeys`):

* First press: the current workspace layout is remembered via
  :func:`omni.ui.Workspace.dump_workspace`, then every other window is hidden so
  the window under the mouse cursor fills the whole application window.
* Second press (with a window maximized): the previously captured workspace
  layout is restored via :func:`omni.ui.Workspace.restore_workspace`, bringing
  every window - including the maximized one - back to where it was.

Design notes
------------
The maximized window is deliberately left *docked* whenever possible: undocking
and re-docking (or a full workspace restore of a docked viewport) tears down and
rebuilds viewport/stage widgets, which is what used to drop the current prim
selection. Hiding the sibling windows makes the dock node of the target window
expand to the full application area, achieving the same visual result without
touching the target window's docking state.

As an extra safety net every layout mutation is wrapped in a selection guard
that captures the USD selection up-front and re-applies it on the following
frames if something in the layout churn cleared it.

The window under the cursor is resolved with
``omni.kit.hotkeys.core.hovered_window.get_hovered_window`` when available, with
a small inline DPI-aware hit-test as a fallback.
"""

import asyncio
from typing import List, Optional

import carb
import omni.ui as ui


class WindowMaximizeToggle:
    """Owns the maximize/restore state for the ``Shift+Escape`` shortcut."""

    #: Minimum frames to hold the selection before checking for readiness.
    _SELECTION_MIN_FRAMES = 4
    #: Upper bound (~2s at 60 FPS) for waiting on rebuilt windows.
    _SELECTION_WAIT_FRAMES = 120
    #: Extra frame delays after which a forced selection event is emitted.
    _SELECTION_RESYNC_DELAYS = (1, 10, 30)

    def __init__(self):
        self._saved_workspace: Optional[List] = None
        self._maximized_title: Optional[str] = None
        self._hidden_titles: List[tuple] = []
        self._was_floating = False
        self._saved_geometry: Optional[tuple] = None
        self._restore_task: Optional[asyncio.Task] = None
        self._tab_task: Optional[asyncio.Task] = None

    # -- public API -----------------------------------------------------------

    def toggle(self):
        """Maximize the hovered window, or restore the layout if one is active."""
        if self._maximized_title is not None:
            self._restore()
        else:
            self._maximize_hovered()

    def reset(self):
        """Drop any pending state without touching the current layout."""
        if self._restore_task is not None and not self._restore_task.done():
            self._restore_task.cancel()
        self._restore_task = None
        if self._tab_task is not None and not self._tab_task.done():
            self._tab_task.cancel()
        self._tab_task = None
        self._saved_workspace = None
        self._maximized_title = None
        self._hidden_titles = []
        self._saved_geometry = None

    # -- selection preservation ----------------------------------------------

    @staticmethod
    def _get_selected_paths() -> Optional[List[str]]:
        try:
            import omni.usd

            context = omni.usd.get_context()
            if context is None:
                return None
            return list(context.get_selection().get_selected_prim_paths())
        except Exception:
            return None

    @staticmethod
    def _set_selected_paths(paths: List[str], force: bool = False):
        try:
            import omni.usd

            context = omni.usd.get_context()
            if context is None:
                return
            selection = context.get_selection()
            if not force and list(selection.get_selected_prim_paths()) == list(paths):
                return
            selection.set_selected_prim_paths(list(paths), False)
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not restore prim selection: {exc}")

    def _reapply_selection_later(self, paths: List[str], wait_titles=None):
        """Re-apply ``paths`` once the layout has settled.

        Window show/hide and ``restore_workspace`` rebuild windows over an
        unpredictable number of frames - on a fresh app start the Stage window
        is a deferred window that is destroyed on hide and only reappears many
        frames after the restore was requested. A fixed short frame budget
        therefore only worked once the window had already been created in the
        running session.

        So instead of guessing: keep re-applying the selection while waiting for
        ``wait_titles`` to exist and be visible (bounded by a timeout), and emit
        forced selection-changed events at several checkpoints afterwards.
        A rebuilt Stage tree syncs its highlighting from selection-*changed*
        events only, which is why the forced clear/set is required even when the
        paths already match.
        """
        if not paths:
            return

        titles = [t for t in (wait_titles or [])]

        async def _force_resync(app):
            self._set_selected_paths([], force=True)
            await app.next_update_async()
            self._set_selected_paths(paths, force=True)

        async def _reapply():
            try:
                import omni.kit.app

                app = omni.kit.app.get_app()

                # Phase 1: hold the selection while the layout rebuilds.
                for frame in range(self._SELECTION_WAIT_FRAMES):
                    await app.next_update_async()
                    self._set_selected_paths(paths)
                    if frame >= self._SELECTION_MIN_FRAMES and self._windows_ready(titles):
                        break

                # Phase 2: forced resyncs, spread out so windows that finish
                # building late still receive an event.
                for delay in self._SELECTION_RESYNC_DELAYS:
                    for _ in range(delay):
                        await app.next_update_async()
                    await _force_resync(app)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                carb.log_warn(
                    f"[QuickSearchUX] Selection restore task failed: {exc}"
                )

        if self._restore_task is not None and not self._restore_task.done():
            self._restore_task.cancel()
        try:
            self._restore_task = asyncio.ensure_future(_reapply())
        except Exception:
            self._restore_task = None
            self._set_selected_paths(paths)

    @staticmethod
    def _windows_ready(titles) -> bool:
        """True when every title resolves to an existing, visible window."""
        for title in titles:
            window = ui.Workspace.get_window(title)
            if window is None:
                return False
            try:
                if not window.visible:
                    return False
            except Exception:
                return False
        return True

    # -- visibility helpers ---------------------------------------------------

    @staticmethod
    def _set_visible(title: str, visible: bool, window=None) -> bool:
        """Show/hide a window by title.

        Order matters here:

        * Real ``ui.Window`` subclasses must be toggled through their Python
          ``visible`` property, because subclasses override its setter to run
          side effects. ``ViewportWindow`` for example flips
          ``viewport_api.updates_enabled`` there - bypassing it leaves the
          viewport frozen after it is shown again.
        * Only for ``WindowHandle`` objects (deferred-created windows yielded by
          ``Workspace.get_windows()``) do we use ``Workspace.show_window``:
          assigning ``visible`` on a handle is deprecated, and ``show_window``
          additionally rebuilds windows that were destroyed while hidden.
        """
        if window is None:
            window = ui.Workspace.get_window(title)
        if isinstance(window, ui.Window):
            try:
                window.visible = visible
                return True
            except Exception:
                pass
        try:
            ui.Workspace.show_window(title, visible)
            return True
        except Exception:
            pass
        if window is None:
            return False
        try:
            window.visible = visible
            return True
        except Exception:
            return False

    @staticmethod
    def _resume_viewport_updates():
        """Re-enable rendering on viewports that are visible again.

        Safety net for viewports that were shown through a code path which did
        not run ``ViewportWindow.visible``'s side effects (e.g. a full
        ``restore_workspace``), which would otherwise leave them frozen.
        """
        try:
            from omni.kit.viewport.window import ViewportWindow
        except Exception:
            return
        try:
            instances = list(ViewportWindow.get_instances())
        except Exception:
            return
        for viewport_window in instances:
            try:
                if not viewport_window.visible:
                    continue
                enabled = (
                    viewport_window.selected_in_dock
                    if viewport_window.docked
                    else True
                )
                viewport_window.viewport_api.updates_enabled = enabled
            except Exception:
                continue

    # -- maximize -------------------------------------------------------------

    def _maximize_hovered(self):
        window = self._get_hovered_window()
        if window is None:
            carb.log_info("[QuickSearchUX] No window under cursor to maximize")
            return

        selection = self._get_selected_paths()

        try:
            self._saved_workspace = ui.Workspace.dump_workspace()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not dump workspace: {exc}")
            self._saved_workspace = None
            return

        title = window.title
        try:
            self._was_floating = not window.docked
            if self._was_floating:
                self._saved_geometry = (
                    window.position_x,
                    window.position_y,
                    window.width,
                    window.height,
                )
            else:
                self._saved_geometry = None

            # Keep the target window's docking state untouched: hiding the
            # siblings makes its dock node expand to the full app area.
            self._hidden_titles = self._hide_other_windows(title)

            if self._was_floating:
                self._fill_app_window(window)

            self._set_visible(title, True, window)
            window.focus()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not maximize window '{title}': {exc}")
            # Best-effort rollback so we don't leave a half-maximized layout.
            self._maximized_title = title
            self._restore()
            return

        self._maximized_title = title
        if selection:
            self._reapply_selection_later(selection)
        carb.log_info(f"[QuickSearchUX] Maximized window '{title}'")

    @classmethod
    def _hide_other_windows(cls, keep_title: str) -> List[tuple]:
        """Hide every other visible window.

        Returns a list of ``(title, was_selected_in_dock)`` so the restore pass
        can bring back the exact same tab per dock group.
        """
        hidden: List[tuple] = []
        for other in ui.Workspace.get_windows():
            if other.title == keep_title:
                continue
            try:
                if not other.visible:
                    continue
            except Exception:
                continue
            try:
                selected = bool(other.docked and other.is_selected_in_dock())
            except Exception:
                selected = False
            if cls._set_visible(other.title, False, other):
                hidden.append((other.title, selected))
        return hidden

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
        hidden = self._hidden_titles
        geometry = self._saved_geometry
        was_floating = self._was_floating
        title = self._maximized_title

        self._saved_workspace = None
        self._maximized_title = None
        self._hidden_titles = []
        self._saved_geometry = None

        selection = self._get_selected_paths()

        # Cheap path: nothing about the docking layout changed while maximized,
        # so simply showing the hidden windows again is enough and avoids the
        # expensive (and selection-clobbering) full workspace restore.
        shown, missing = self._show_windows(hidden)
        if missing:
            # Some windows are destroyed when hidden (deferred-created windows
            # such as Stage) and cannot be brought back by visibility alone -
            # fall back to the full workspace restore in that case.
            carb.log_info(
                "[QuickSearchUX] Falling back to workspace restore for: "
                + ", ".join(missing)
            )
            if workspace:
                try:
                    ui.Workspace.restore_workspace(workspace)
                except Exception as exc:
                    carb.log_warn(
                        f"[QuickSearchUX] Could not restore workspace: {exc}"
                    )
            self._reselect_tabs_later(hidden, title)
        elif shown:
            if was_floating and geometry and title:
                self._restore_geometry(title, geometry)
            # Showing the windows again re-selects whatever tab was touched
            # last in each dock group, so explicitly re-select the tabs that
            # were active before maximizing - and the maximized window last, so
            # it stays the visible tab of its own dock group.
            self._reselect_tabs_later(hidden, title)
            carb.log_info("[QuickSearchUX] Restored previous window layout")
        elif workspace:
            try:
                ui.Workspace.restore_workspace(workspace)
                carb.log_info("[QuickSearchUX] Restored previous workspace layout")
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not restore workspace: {exc}")

        if selection:
            # Wait for all previously hidden windows (plus the maximized one) to
            # be back before emitting the forced selection events.
            wait_titles = [entry_title for entry_title, _ in hidden]
            if title:
                wait_titles.append(title)
            self._reapply_selection_later(selection, wait_titles)

    @classmethod
    def _show_windows(cls, entries: List[tuple]):
        """Re-show previously hidden windows.

        Returns ``(shown_any, missing_titles)`` where ``missing_titles`` lists
        windows that could not be brought back (they were destroyed while
        hidden and need a full workspace restore).
        """
        if not entries:
            return False, []
        shown = False
        missing: List[str] = []
        for title, _selected in entries:
            # ``show_window`` also rebuilds deferred-created windows through the
            # window factory registered by their owning extension.
            cls._set_visible(title, True)
            window = ui.Workspace.get_window(title)
            if window is None:
                missing.append(title)
                continue
            try:
                if not window.visible:
                    missing.append(title)
                    continue
            except Exception:
                pass
            shown = True
        return shown, missing
        return shown

    def _reselect_tabs_later(self, entries: List[tuple], keep_title: Optional[str]):
        """Re-activate the dock tabs that were active before maximizing."""
        titles = [title for title, selected in entries if selected]
        if not titles and not keep_title:
            return

        def _apply():
            for title in titles:
                if title == keep_title:
                    continue
                window = ui.Workspace.get_window(title)
                if window is None or not window.visible:
                    continue
                try:
                    window.focus()
                except Exception:
                    continue
            if keep_title:
                window = ui.Workspace.get_window(keep_title)
                if window is not None:
                    try:
                        window.focus()
                    except Exception:
                        pass

        async def _deferred():
            try:
                import omni.kit.app

                app = omni.kit.app.get_app()
                await app.next_update_async()
                _apply()
                await app.next_update_async()
                _apply()
                # Dock-tab changes drive ViewportWindow.updates_enabled, so
                # resume rendering only after the tabs settled.
                self._resume_viewport_updates()
                await app.next_update_async()
                self._resume_viewport_updates()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not restore dock tabs: {exc}")

        if self._tab_task is not None and not self._tab_task.done():
            self._tab_task.cancel()
        try:
            self._tab_task = asyncio.ensure_future(_deferred())
        except Exception:
            self._tab_task = None
            _apply()
            self._resume_viewport_updates()

    @staticmethod
    def _restore_geometry(title: str, geometry: tuple):
        window = ui.Workspace.get_window(title)
        if window is None:
            return
        try:
            window.position_x, window.position_y, window.width, window.height = geometry
        except Exception as exc:
            carb.log_warn(
                f"[QuickSearchUX] Could not restore geometry of '{title}': {exc}"
            )

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
