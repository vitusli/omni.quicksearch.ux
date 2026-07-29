"""Quick Search UX extension entry point.

Thin coordinator that wires together the feature handlers:

* :class:`~.menu_snapshot.MenuSnapshotCapture` - menubar snapshot for search
* :class:`~.preview_capture.PreviewCaptureHandler` - viewport preview on save
* :class:`~.create_project.CreateProjectHandler` - File > Create Project
* :class:`~.stage_navigation.StageNavigationHandler` - Stage prim navigation
* :class:`~.hotkeys.HotkeyManager` - hotkeys, actions and keyboard fallback
* :class:`~.toolbar_buttons.ToolbarButtonsManager` - Pivot / Array / Scene Optimizer toolbar buttons
"""

import asyncio

import carb
import omni.ext
import omni.kit.app

from omni.kit.window.quicksearch import QuickSearchRegistry
from omni.kit.window.quicksearch.quicksearch_window import QuickSearchWindow

from .create_project import CreateProjectHandler
from .hotkeys import HotkeyManager
from .make_paths_relative import MakePathsRelativeHandler
from .menu_snapshot import MenuSnapshotCapture
from .model import UnifiedQuickSearchModel, set_rebuild_search_index_action
from .paths import normalize_path
from .preview_capture import PreviewCaptureHandler
from .stage_navigation import StageNavigationHandler
from .toolbar_buttons import ToolbarButtonsManager
from .window_toggle import WindowMaximizeToggle


class Extension(omni.ext.IExt):
    def __init__(self):
        super().__init__()
        self._ext_id = None
        self._subscription = None
        self._window = None
        self._exclusive = False

        self._menu_snapshot = None
        self._preview_capture = None
        self._create_project = None
        self._make_paths_relative = None
        self._stage_nav = None
        self._hotkeys = None
        self._window_toggle = None
        self._toolbar_buttons = None
        self._snapshot_task = None
        self._prewarm_task = None

    # -- lifecycle ------------------------------------------------------------

    def on_startup(self, ext_id: str):
        self._ext_id = omni.ext.get_extension_name(ext_id)

        ext_path = (
            omni.kit.app.get_app()
            .get_extension_manager()
            .get_extension_path_by_module(__name__)
        )
        gridroom_asset_source = normalize_path(f"{ext_path}/omni/quicksearch/ux/gridroom")

        self._menu_snapshot = MenuSnapshotCapture()
        self._preview_capture = PreviewCaptureHandler()
        self._create_project = CreateProjectHandler(gridroom_asset_source)
        self._make_paths_relative = MakePathsRelativeHandler()
        self._stage_nav = StageNavigationHandler()
        self._window_toggle = WindowMaximizeToggle()
        self._hotkeys = HotkeyManager(
            self._ext_id,
            show_window=self.show_window,
            stage_nav=self._stage_nav,
            get_menu_trigger_map=lambda: self._menu_snapshot.trigger_map,
            capture_menu_snapshot=self._menu_snapshot.capture_once,
            window_toggle=self._window_toggle,
        )

        self._toolbar_buttons = ToolbarButtonsManager()
        self._toolbar_buttons.startup()

        self._subscription = QuickSearchRegistry().register_quick_search_model(
            "Quick Search UX",
            UnifiedQuickSearchModel,
            None,
            accept_fn=self._accept_provider,
            exclusive_fn=self._is_exclusive,
            priority=20,
            flat_search=True,
        )

        self._snapshot_task = asyncio.ensure_future(self._menu_snapshot.capture_with_retry())
        self._prewarm_task = asyncio.ensure_future(self._prewarm_window_next_frame())
        self._preview_capture.start()
        self._create_project.register_menu_entry()
        self._make_paths_relative.register_menu_entry()
        self._hotkeys.register()
        set_rebuild_search_index_action(self.rebuild_search_index)
        carb.log_info("[QuickSearchUX] Registered unified quick-search provider")

    def on_shutdown(self):
        if self._snapshot_task and not self._snapshot_task.done():
            self._snapshot_task.cancel()
        self._snapshot_task = None

        if self._prewarm_task and not self._prewarm_task.done():
            self._prewarm_task.cancel()
        self._prewarm_task = None

        if self._preview_capture:
            self._preview_capture.stop()
        if self._toolbar_buttons:
            self._toolbar_buttons.shutdown()
            self._toolbar_buttons = None
        if self._hotkeys:
            self._hotkeys.deregister()
        if self._create_project:
            self._create_project.deregister_menu_entry()
        if self._make_paths_relative:
            self._make_paths_relative.deregister_menu_entry()
        if self._stage_nav:
            self._stage_nav.reset()
        if self._window_toggle:
            self._window_toggle.reset()
        set_rebuild_search_index_action(None)

        self._subscription = None
        if self._window:
            self._window.destroy()
            self._window = None
        carb.log_info("[QuickSearchUX] Unregistered unified quick-search provider")

    def rebuild_search_index(self):
        asyncio.ensure_future(self._rebuild_search_index_next_frame())

    async def _rebuild_search_index_next_frame(self):
        await omni.kit.app.get_app().next_update_async()

        self._resync_extension_registries()

        if self._menu_snapshot:
            captured = self._menu_snapshot.capture_once()
            if not captured:
                asyncio.ensure_future(self._menu_snapshot.capture_with_retry())

        if self._window:
            try:
                self._window.destroy()
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not destroy quick-search window during rebuild: {exc}")
            finally:
                self._window = None

        self._prewarm_task = asyncio.ensure_future(self._prewarm_window_next_frame())
        carb.log_info("[QuickSearchUX] Rebuilt search index and refreshed quick-search window")

    @staticmethod
    def _resync_extension_registries():
        ext_manager = omni.kit.app.get_app().get_extension_manager()
        candidate_methods = (
            "refresh_registry",
            "refresh_registries",
            "sync_registry",
            "sync_registries",
            "resync_registry",
            "rescan_registry",
            "refresh_extension_registries",
        )

        for method_name in candidate_methods:
            method = getattr(ext_manager, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                carb.log_info(f"[QuickSearchUX] Synced extension registries using '{method_name}'")
                return
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Registry sync method '{method_name}' failed: {exc}")

        carb.log_warn("[QuickSearchUX] No extension-registry sync API found on extension manager")

    # -- quick-search window --------------------------------------------------

    def show_window(self):
        self._exclusive = True
        if not self._window:
            self._window = QuickSearchWindow()
        else:
            self._window.show()
        asyncio.ensure_future(self._refresh_menu_snapshot_next_frame())
        asyncio.ensure_future(self._clear_exclusive_next_frame())

    def _is_exclusive(self):
        return self._exclusive

    def _accept_provider(self):
        return self._exclusive

    async def _clear_exclusive_next_frame(self):
        await omni.kit.app.get_app().next_update_async()
        self._exclusive = False

    async def _refresh_menu_snapshot_next_frame(self):
        await omni.kit.app.get_app().next_update_async()
        self._menu_snapshot.capture_once()

    async def _prewarm_window_next_frame(self):
        await omni.kit.app.get_app().next_update_async()
        if self._window is not None:
            return
        try:
            self._window = QuickSearchWindow()
            hide_fn = getattr(self._window, "hide", None)
            if callable(hide_fn):
                hide_fn()
            elif hasattr(self._window, "visible"):
                self._window.visible = False
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not pre-warm quick-search window: {exc}")
