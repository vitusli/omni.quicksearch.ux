"""Quick Search UX extension entry point.

Thin coordinator that wires together the feature handlers:

* :class:`~.menu_snapshot.MenuSnapshotCapture` - menubar snapshot for search
* :class:`~.preview_capture.PreviewCaptureHandler` - viewport preview on save
* :class:`~.create_project.CreateProjectHandler` - File > Create Project
* :class:`~.stage_navigation.StageNavigationHandler` - Stage prim navigation
* :class:`~.hotkeys.HotkeyManager` - hotkeys, actions and keyboard fallback
"""

import asyncio

import carb
import omni.ext
import omni.kit.app

from omni.kit.window.quicksearch import QuickSearchRegistry
from omni.kit.window.quicksearch.quicksearch_window import QuickSearchWindow

from .create_project import CreateProjectHandler
from .hotkeys import HotkeyManager
from .menu_snapshot import MenuSnapshotCapture
from .model import UnifiedQuickSearchModel
from .paths import normalize_path
from .preview_capture import PreviewCaptureHandler
from .stage_navigation import StageNavigationHandler


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
        self._stage_nav = None
        self._hotkeys = None
        self._snapshot_task = None

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
        self._stage_nav = StageNavigationHandler()
        self._hotkeys = HotkeyManager(
            self._ext_id,
            show_window=self.show_window,
            stage_nav=self._stage_nav,
            get_menu_trigger_map=lambda: self._menu_snapshot.trigger_map,
            capture_menu_snapshot=self._menu_snapshot.capture_once,
        )

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
        self._preview_capture.start()
        self._create_project.register_menu_entry()
        self._hotkeys.register()
        carb.log_info("[QuickSearchUX] Registered unified quick-search provider")

    def on_shutdown(self):
        if self._snapshot_task and not self._snapshot_task.done():
            self._snapshot_task.cancel()
        self._snapshot_task = None

        if self._preview_capture:
            self._preview_capture.stop()
        if self._hotkeys:
            self._hotkeys.deregister()
        if self._create_project:
            self._create_project.deregister_menu_entry()
        if self._stage_nav:
            self._stage_nav.reset()

        self._subscription = None
        if self._window:
            self._window.destroy()
            self._window = None
        carb.log_info("[QuickSearchUX] Unregistered unified quick-search provider")

    # -- quick-search window --------------------------------------------------

    def show_window(self):
        self._exclusive = True
        self._menu_snapshot.capture_once()
        if not self._window:
            self._window = QuickSearchWindow()
        else:
            self._window.show()
        asyncio.ensure_future(self._clear_exclusive_next_frame())

    def _is_exclusive(self):
        return self._exclusive

    def _accept_provider(self):
        return self._exclusive

    async def _clear_exclusive_next_frame(self):
        await omni.kit.app.get_app().next_update_async()
        self._exclusive = False
