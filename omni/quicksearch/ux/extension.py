import asyncio
import os
import tempfile
from urllib.parse import unquote, urlparse

import carb
import carb.input
import omni.client
import omni.ext
import omni.kit.app
import omni.ui as ui
import omni.usd
from pxr import Sdf, UsdGeom

from omni.kit.window.quicksearch import QuickSearchRegistry
from omni.kit.window.quicksearch.quicksearch_window import QuickSearchWindow

from .model import UnifiedQuickSearchModel, set_menu_snapshot


ACTION_ID = "ShowUnifiedQuickSearch"
ACTION_DISPLAY_NAME = "Unified Quick Search"
LAYOUT_QUICK_LOAD_ACTION_ID = "RunLayoutQuickLoadCtrl8"
LAYOUT_QUICK_LOAD_ACTION_DISPLAY_NAME = "Layout->Quick Load"
LAYOUT_QUICK_LOAD_ACTION_CANDIDATES = (
    ("isaacsim.app.setup", "layout_quick_load"),
    ("omni.kit.quicklayout", "quicklayout_quick_load"),
    ("omni.kit.quicklayout", "quick_load"),
)
COPY_SELECTED_PRIMS_ACTION_ID = "CopySelectedPrimPaths"
COPY_SELECTED_PRIMS_ACTION_DISPLAY_NAME = "Stage->Copy Selected Prim Paths"
TOGGLE_SELECTED_PRIMS_ACTIVE_STATE_ACTION_ID = "ToggleSelectedPrimActiveState"
TOGGLE_SELECTED_PRIMS_ACTIVE_STATE_ACTION_DISPLAY_NAME = "Stage->Toggle Selected Prim Active State"
LAYOUT_QUICK_LOAD_PATH_CANDIDATES = (
    ("Layout", "Quick Load"),
    ("Quicklayout", "Quick Load"),
    ("Quicklayout", "Quicklayout Quick Load"),
)
CREATE_PROJECT_SUBFOLDERS = (
    "_input",
    "_output",
    "CAD",
    "nova",
    "nova/scripts",
    "nova/configs",
    "omniverse/scripts",
    "omniverse/assets/environments/hdri",
    "omniverse/assets/materials",
    "omniverse/assets/periphery",
    "omniverse/assets/robots",
    "omniverse/assets/tools",
    "omniverse/assets/workpieces",
    "export/video",
    "export/still",
    "export/mesh",
    "sandbox",
)
PREVIEW_STAGE_FILENAMES = {"main.usd", "main.usda"}
PREVIEW_IMAGE_FILENAME = "preview.png"

README_TEMPLATE = """# Project Structure

Welcome! This document provides an overview of the project's folder structure to help you navigate and organize your work.

## Directory Overview

### `_input/`
Let the customers upload files here.

### `_output/`
Please place any files, datasets, or resources for customers and other stackholders here.

### `CAD/`
- CAD data
- other building blocks

### `export/`
This directory is used for renderings and mesh exports, including:
- Video renderings
- Still images
- Mesh exports (e.g., `.stl` files)

### `omniverse/`
This is your main working directory for Omniverse projects and scripts. Store your:
- Scene files (e.g., `main.usda`)
- Project assets
- Python scripts and automation tools

### `nova/`
This folder is designated for your NOVA scripts and configs.

### `sandbox/`
If you want to ignore this folder structure and go nuts, feel free to do so in here.
"""


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
        self._hotkey_fallback_mode = False
        self._ctrl_f_hotkey_registered = False
        self._ctrl_8_hotkey_registered = False
        self._action_registry = None
        self._hotkey_registry = None
        self._snapshot_task = None
        self._preview_capture_task = None
        self._stage_event_sub = None
        self._exclusive = False
        self._expanded_paths = set()
        self._collapsed_paths = set()
        self._menu_trigger_map = {}
        self._file_menu_items = []
        self._create_project_dialog = None
        self._message_window = None
        self._gridroom_asset_source = None

    def on_startup(self, ext_id: str):
        self._ext_id = omni.ext.get_extension_name(ext_id)
        self._extension_name = self._ext_id
        ext_path = omni.kit.app.get_app().get_extension_manager().get_extension_path_by_module(__name__)
        self._gridroom_asset_source = self._normalize_path(f"{ext_path}/omni/quicksearch/ux/gridroom")
        self._expanded_paths = set()
        self._collapsed_paths = set()
        self._menu_trigger_map = {}

        self._subscription = QuickSearchRegistry().register_quick_search_model(
            "Quick Search UX",
            UnifiedQuickSearchModel,
            None,
            accept_fn=self._accept_provider,
            exclusive_fn=self._is_exclusive,
            priority=20,
            flat_search=True,
        )

        self._snapshot_task = asyncio.ensure_future(self._capture_menu_snapshot())
        self._register_stage_save_preview_hook()
        self._register_file_menu_entry()
        self._register_hotkeys()
        carb.log_info("[QuickSearchUX] Registered unified quick-search provider")

    def on_shutdown(self):
        if self._snapshot_task and not self._snapshot_task.done():
            self._snapshot_task.cancel()
        self._snapshot_task = None
        if self._preview_capture_task and not self._preview_capture_task.done():
            self._preview_capture_task.cancel()
        self._preview_capture_task = None
        self._deregister_stage_save_preview_hook()
        self._deregister_hotkeys()
        self._deregister_file_menu_entry()
        self._subscription = None
        self._expanded_paths = set()
        self._collapsed_paths = set()
        self._menu_trigger_map = {}
        if self._window:
            self._window.destroy()
            self._window = None
        carb.log_info("[QuickSearchUX] Unregistered unified quick-search provider")

    def _register_stage_save_preview_hook(self):
        try:
            event_stream = omni.usd.get_context().get_stage_event_stream()
            self._stage_event_sub = event_stream.create_subscription_to_pop(
                self._on_stage_event,
                name="QuickSearchUX stage save preview",
            )
        except Exception as exc:
            self._stage_event_sub = None
            carb.log_warn(f"[QuickSearchUX] Could not subscribe to stage events: {exc}")

    def _deregister_stage_save_preview_hook(self):
        self._stage_event_sub = None

    def _on_stage_event(self, event):
        if not self._is_save_stage_event(event):
            return

        if self._preview_capture_task and not self._preview_capture_task.done():
            self._preview_capture_task.cancel()

        self._preview_capture_task = asyncio.ensure_future(self._capture_preview_for_saved_main_stage())

    @staticmethod
    def _is_save_stage_event(event) -> bool:
        event_type = int(getattr(event, "type", -1))
        for name in dir(omni.usd.StageEventType):
            if "SAVE" not in name.upper() or name.startswith("_"):
                continue
            value = getattr(omni.usd.StageEventType, name, None)
            if value is not None and event_type == int(value):
                return True
        return False

    async def _capture_preview_for_saved_main_stage(self):
        preview_target, is_remote_target = self._get_preview_target_for_current_stage()
        if not preview_target:
            return

        app = omni.kit.app.get_app()
        await app.next_update_async()
        await app.next_update_async()

        if is_remote_target:
            temp_preview_path = os.path.join(tempfile.gettempdir(), f"quicksearchux_preview_{os.getpid()}.png")
            capture_ok = await self._capture_active_viewport_to_file(temp_preview_path)
            write_ok = capture_ok and self._write_binary_file_to_omni_url(temp_preview_path, preview_target)
            try:
                if os.path.exists(temp_preview_path):
                    os.remove(temp_preview_path)
            except Exception:
                pass
            success = bool(write_ok)
        else:
            success = await self._capture_active_viewport_to_file(preview_target)

        if success:
            carb.log_info(f"[QuickSearchUX] Updated preview image: {preview_target}")
        else:
            carb.log_warn("[QuickSearchUX] Could not capture viewport preview image")

    @staticmethod
    def _get_preview_target_for_current_stage() -> tuple[str | None, bool]:
        context = omni.usd.get_context()
        stage_refs = [Extension._normalize_path(context.get_stage_url())]

        stage = context.get_stage()
        if stage:
            root_layer = stage.GetRootLayer()
            if root_layer:
                stage_refs.append(Extension._normalize_path(getattr(root_layer, "realPath", "")))
                stage_refs.append(Extension._normalize_path(getattr(root_layer, "identifier", "")))

        for stage_ref in stage_refs:
            target = Extension._preview_target_from_stage_reference(stage_ref)
            if target is not None:
                return target

        return None, False

    @staticmethod
    def _preview_target_from_stage_reference(stage_ref: str) -> tuple[str, bool] | None:
        value = Extension._normalize_path(stage_ref)
        if not value or value.startswith("anon:"):
            return None

        local_path = Extension._to_local_filesystem_path(value)
        if local_path:
            if os.path.basename(local_path).lower() not in PREVIEW_STAGE_FILENAMES:
                return None
            return os.path.join(os.path.dirname(local_path), PREVIEW_IMAGE_FILENAME), False

        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            return None

        stage_path = unquote(parsed.path or "")
        stage_name = stage_path.rsplit("/", 1)[-1].lower() if stage_path else ""
        if stage_name not in PREVIEW_STAGE_FILENAMES:
            return None

        stage_dir = stage_path.rsplit("/", 1)[0] if "/" in stage_path else ""
        preview_remote_path = f"{stage_dir}/{PREVIEW_IMAGE_FILENAME}" if stage_dir else f"/{PREVIEW_IMAGE_FILENAME}"
        target_url = f"{parsed.scheme}://{parsed.netloc}{preview_remote_path}"
        return target_url, True

    @staticmethod
    def _write_binary_file_to_omni_url(local_file_path: str, destination_url: str) -> bool:
        try:
            with open(local_file_path, "rb") as source_stream:
                payload = source_stream.read()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not read temporary preview image: {exc}")
            return False

        try:
            result = omni.client.write_file(destination_url, payload)
            if Extension._omni_result_ok(result):
                return True
            carb.log_warn(f"[QuickSearchUX] Could not write preview image to {destination_url}: {result}")
            return False
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not write preview image to {destination_url}: {exc}")
            return False

    @staticmethod
    def _to_local_filesystem_path(path_or_url: str) -> str | None:
        value = Extension._normalize_path(path_or_url)
        if not value or value.startswith("anon:"):
            return None

        if value.lower().startswith("file:"):
            parsed = urlparse(value)
            decoded_path = unquote(parsed.path or "")

            if parsed.netloc and parsed.netloc not in ("", "localhost"):
                unc_path = f"//{parsed.netloc}{decoded_path}"
                return os.path.abspath(unc_path)

            if len(decoded_path) >= 3 and decoded_path[0] == "/" and decoded_path[2] == ":":
                decoded_path = decoded_path[1:]

            decoded_path = decoded_path.replace("/", os.sep)
            return os.path.abspath(decoded_path)

        if "://" in value:
            return None

        return os.path.abspath(value)

    @staticmethod
    async def _capture_active_viewport_to_file(output_path: str) -> bool:
        try:
            from omni.kit.viewport.utility import capture_viewport_to_file, get_active_viewport
        except Exception:
            return False

        viewport_api = get_active_viewport()
        if viewport_api is None:
            return False

        camera_path = str(getattr(viewport_api, "camera_path", "") or "")
        if camera_path and not camera_path.lower().endswith("persp"):
            carb.log_warn(
                f"[QuickSearchUX] Active viewport camera is not perspective: {camera_path}. Capturing anyway."
            )

        try:
            capture_viewport_to_file(viewport_api, output_path)
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Viewport capture failed: {exc}")
            return False

        app = omni.kit.app.get_app()
        for _ in range(30):
            if os.path.exists(output_path):
                try:
                    if os.path.getsize(output_path) > 0:
                        break
                except Exception:
                    pass
            await app.next_update_async()
        else:
            carb.log_warn(f"[QuickSearchUX] Preview image was not created: {output_path}")
            return False

        return True

    def show_window(self):
        self._exclusive = True
        self._capture_menu_snapshot_once()
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

            self._menu_trigger_map = trigger_map or {}
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

    def _register_file_menu_entry(self):
        try:
            from omni.kit.menu.utils import MenuItemDescription, add_menu_items

            self._file_menu_items = [
                MenuItemDescription(
                    name="Create Project",
                    onclick_fn=self._run_create_project_script,
                )
            ]
            add_menu_items(self._file_menu_items, "File")
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not register File menu entry: {exc}")
            self._file_menu_items = []

    def _deregister_file_menu_entry(self):
        if not self._file_menu_items:
            return
        try:
            from omni.kit.menu.utils import remove_menu_items

            remove_menu_items(self._file_menu_items, "File")
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not deregister File menu entry: {exc}")
        finally:
            self._file_menu_items = []

    def _run_create_project_script(self):
        try:
            from omni.kit.window.filepicker import FilePickerDialog

            if self._create_project_dialog:
                self._create_project_dialog.hide()
                self._create_project_dialog = None

            filter_options = ["Project Name (*)"]

            dialog = FilePickerDialog(
                "Create Project - Enter project name",
                apply_button_label="Create",
                click_apply_handler=lambda filename, dirname: self._create_project_from_selection(filename, dirname),
                item_filter_options=filter_options,
                item_filter_fn=lambda item: self._filepicker_filter(item),
            )
            self._create_project_dialog = dialog
            dialog.show()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not open Omniverse file browser: {exc}")

    def _create_project_from_selection(self, filename: str, dirname: str):
        try:
            if self._create_project_dialog:
                self._create_project_dialog.hide()
                self._create_project_dialog = None

            project_root, main_usd_path = self._build_project_paths(filename, dirname)
            if not project_root or not main_usd_path:
                carb.log_info("[QuickSearchUX] Create Project canceled")
                return

            context = omni.usd.get_context()
            stage_url = self._normalize_path(context.get_stage_url())
            if self._is_saved_stage(stage_url):
                message = "Current stage is already saved. Please open a new stage first and run Create Project again."
                self._show_message("Create Project", message)
                carb.log_warn(f"[QuickSearchUX] {message}")
                return

            self._create_project_structure(project_root)
            self._copy_gridroom_asset_to_project(project_root)
            self._merge_project_prims_into_current_stage()
            saved = context.save_as_stage(main_usd_path)
            if saved is False:
                raise RuntimeError(f"Could not save stage to: {main_usd_path}")
            self._open_stage_in_context(main_usd_path)

            carb.log_info(f"[QuickSearchUX] Project created: {project_root}")
            carb.log_info(f"[QuickSearchUX] Main stage file: {main_usd_path}")
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not create project: {exc}")

    @staticmethod
    def _filepicker_filter(item) -> bool:
        return True

    @staticmethod
    def _normalize_path(path: str) -> str:
        return str(path or "").strip().replace("\\", "/")

    @staticmethod
    def _sanitize_project_name(value: str) -> str:
        project_name = str(value or "").strip().replace("\\", "/").split("/")[-1]
        if project_name.lower().endswith(".usd") or project_name.lower().endswith(".usda"):
            project_name = project_name.rsplit(".", 1)[0]
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            project_name = project_name.replace(char, "")
        return project_name.strip()

    @classmethod
    def _build_project_paths(cls, filename: str, dirname: str) -> tuple[str | None, str | None]:
        project_name = cls._sanitize_project_name(filename)
        base_dir = cls._normalize_path(dirname)
        if not project_name or not base_dir:
            return None, None

        if base_dir.endswith("/"):
            project_root = f"{base_dir}{project_name}"
        else:
            project_root = f"{base_dir}/{project_name}"
        main_usd_path = f"{project_root}/omniverse/main.usda"
        return project_root, main_usd_path

    @staticmethod
    def _is_saved_stage(stage_url: str) -> bool:
        normalized = Extension._normalize_path(stage_url)
        return bool(normalized) and not normalized.startswith("anon:")

    @staticmethod
    def _create_project_structure(project_root: str):
        Extension._create_folder(project_root)
        for rel_path in CREATE_PROJECT_SUBFOLDERS:
            rel = rel_path.replace("\\", "/")
            target = f"{project_root}/{rel}" if project_root else rel
            Extension._create_folder(target)

        readme_path = f"{project_root}/README.md" if project_root else "README.md"
        Extension._write_text_file(readme_path, README_TEMPLATE)

    @staticmethod
    def _omni_result_ok(result) -> bool:
        if isinstance(result, tuple):
            result = result[0]
        name = str(getattr(result, "name", result)).upper()
        return "OK" in name or "ALREADY" in name

    @staticmethod
    def _create_folder(path: str):
        result = omni.client.create_folder(path)
        if not Extension._omni_result_ok(result):
            raise RuntimeError(f"Could not create folder: {path} ({result})")

    @staticmethod
    def _write_text_file(path: str, content: str):
        payload = content.encode("utf-8")
        result = omni.client.write_file(path, payload)
        if not Extension._omni_result_ok(result):
            raise RuntimeError(f"Could not write file: {path} ({result})")

    def _copy_gridroom_asset_to_project(self, project_root: str):
        source_root = self._gridroom_asset_source
        if not source_root:
            raise RuntimeError("Gridroom source path is not initialized")
        if not os.path.isdir(source_root):
            raise RuntimeError(f"Gridroom asset folder not found: {source_root}")

        destination_root = f"{project_root}/omniverse/assets/environments/gridroom"
        self._create_folder(destination_root)

        for local_dir, _, file_names in os.walk(source_root):
            relative_dir = os.path.relpath(local_dir, source_root).replace("\\", "/")
            destination_dir = destination_root if relative_dir == "." else f"{destination_root}/{relative_dir}"
            self._create_folder(destination_dir)

            for file_name in file_names:
                source_file = os.path.join(local_dir, file_name)
                destination_file = f"{destination_dir}/{file_name}"
                with open(source_file, "rb") as source_stream:
                    payload = source_stream.read()
                result = omni.client.write_file(destination_file, payload)
                if not self._omni_result_ok(result):
                    raise RuntimeError(f"Could not write gridroom asset file: {destination_file} ({result})")

    @staticmethod
    def _merge_project_prims_into_current_stage():
        context = omni.usd.get_context()
        stage = context.get_stage()
        if not stage:
            created = context.new_stage()
            if created is False:
                raise RuntimeError("Could not create new stage")
            stage = context.get_stage()
            if not stage:
                raise RuntimeError("No active stage after creation")

        stage.SetStartTimeCode(0)
        stage.SetEndTimeCode(1000)
        stage.SetTimeCodesPerSecond(60)
        stage.SetMetadata("metersPerUnit", 1.0)
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)

        world = Extension._define_xform_if_missing(stage, "/World")
        stage.SetDefaultPrim(world.GetPrim())

        Extension._define_xform_if_missing(stage, "/World/cell")
        Extension._define_xform_if_missing(stage, "/World/cell/workspace_robot1")
        Extension._define_xform_if_missing(stage, "/World/cell/workspace_robot1/tools")
        Extension._define_xform_if_missing(stage, "/World/cell/workspace_robot1/workpieces")
        Extension._define_xform_if_missing(stage, "/World/cell/workspace_robot1/periphery")
        Extension._define_xform_if_missing(stage, "/World/cell/workspace_robot2")
        Extension._define_xform_if_missing(stage, "/World/cell/workspace_robot2/tools")
        Extension._define_xform_if_missing(stage, "/World/cell/workspace_robot2/workpieces")
        Extension._define_xform_if_missing(stage, "/World/cell/workspace_robot2/periphery")
        Extension._define_xform_if_missing(stage, "/World/periphery")
        Extension._define_xform_if_missing(stage, "/World/environment")

        Extension._define_xform_if_missing(stage, "/Environment")
        Extension._define_gridroom_payload(stage)

        stage.SetDefaultPrim(world.GetPrim())

    @staticmethod
    def _define_xform_if_missing(stage, path: str):
        prim = stage.GetPrimAtPath(path)
        if prim and prim.IsValid():
            return UsdGeom.Xform(prim)
        return UsdGeom.Xform.Define(stage, path)

    @staticmethod
    def _define_gridroom_payload(stage):
        gridroom_prim = stage.GetPrimAtPath("/Environment/gridroom")
        if not gridroom_prim or not gridroom_prim.IsValid():
            gridroom_prim = stage.DefinePrim("/Environment/gridroom")

        gridroom_payload = Sdf.Payload("./assets/environments/gridroom/gridroom.usd")
        payload_list = Sdf.PayloadListOp.CreateExplicit([gridroom_payload])
        gridroom_prim.SetMetadata("payload", payload_list)

    def _show_message(self, title: str, text: str):
        if self._message_window:
            try:
                self._message_window.visible = False
            except Exception:
                pass

        window = ui.Window(title, width=560, height=160, flags=ui.WINDOW_FLAGS_MODAL)
        self._message_window = window
        with window.frame:
            with ui.VStack(spacing=10):
                ui.Spacer(height=4)
                ui.Label(text, word_wrap=True)
                with ui.HStack(height=26):
                    ui.Spacer()

                    def close_window():
                        if self._message_window:
                            self._message_window.visible = False
                            self._message_window = None

                    ui.Button("OK", width=80, clicked_fn=close_window)

    @staticmethod
    def _open_stage_in_context(main_usd_path: str):
        context = omni.usd.get_context()
        opened = context.open_stage(main_usd_path)
        if opened is False:
            carb.log_warn(f"[QuickSearchUX] Stage created but could not be opened automatically: {main_usd_path}")

    def _register_hotkeys(self):
        self._register_ctrl_f_hotkey()
        self._register_stage_arrow_hotkeys()
        self._register_keyboard_fallback()

    def _register_ctrl_f_hotkey(self):
        self._ctrl_f_hotkey_registered = False
        self._ctrl_8_hotkey_registered = False
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

            self._action_registry.register_action(
                self._ext_id,
                LAYOUT_QUICK_LOAD_ACTION_ID,
                self._trigger_layout_quick_load,
                display_name=LAYOUT_QUICK_LOAD_ACTION_DISPLAY_NAME,
                description="Run Layout > Quick Load from menu bar",
                tag="Quick Search UX",
            )

            self._hotkey_registry = get_hotkey_registry()
            self._hotkey = self._hotkey_registry.register_hotkey(self._ext_id, key, self._ext_id, ACTION_ID)
            self._ctrl_f_hotkey_registered = self._hotkey is not None

            preferred_ctrl_8_key = self._preferred_keyboard_input_for_digit_8()
            if preferred_ctrl_8_key is None:
                carb.log_warn("[QuickSearchUX] Could not resolve keyboard enum for digit 8, using event fallback only")
                self._ctrl_8_hotkey_registered = False
            else:
                try:
                    ctrl_8_hotkey = self._hotkey_registry.register_hotkey(
                        self._ext_id,
                        KeyCombination(preferred_ctrl_8_key, carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL),
                        self._ext_id,
                        LAYOUT_QUICK_LOAD_ACTION_ID,
                    )
                    self._ctrl_8_hotkey_registered = ctrl_8_hotkey is not None
                except Exception as exc:
                    carb.log_warn(f"[QuickSearchUX] Could not register Ctrl+8 hotkey: {exc}")
                    self._ctrl_8_hotkey_registered = False
            if not self._ctrl_f_hotkey_registered:
                raise RuntimeError("Ctrl+F hotkey registration returned no handle")
            self._hotkey_fallback_mode = False
        except ImportError:
            self._hotkey_fallback_mode = True
            self._register_keyboard_fallback()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not register Ctrl+F hotkey: {exc}")
            self._hotkey_fallback_mode = True
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
                    COPY_SELECTED_PRIMS_ACTION_ID,
                    COPY_SELECTED_PRIMS_ACTION_DISPLAY_NAME,
                    "Copy selected prim paths to clipboard.",
                    self._copy_selected_prim_paths,
                    carb.input.KeyboardInput.C,
                    carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL | carb.input.KEYBOARD_MODIFIER_FLAG_SHIFT,
                ),
                (
                    TOGGLE_SELECTED_PRIMS_ACTIVE_STATE_ACTION_ID,
                    TOGGLE_SELECTED_PRIMS_ACTIVE_STATE_ACTION_DISPLAY_NAME,
                    "Toggle active state for selected prims.",
                    self._toggle_selected_prims_active_state,
                    None,
                    None,
                ),
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
                if key is not None and modifiers is not None:
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
            if self._is_plain_stage_backspace(event):
                self._toggle_selected_prims_active_state()

            use_ctrl_f_fallback = self._hotkey_fallback_mode or not self._ctrl_f_hotkey_registered
            use_ctrl_8_fallback = self._hotkey_fallback_mode or not self._ctrl_8_hotkey_registered

            is_ctrl_f = event.input == carb.input.KeyboardInput.F and bool(
                event.modifiers & carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL
            )
            if use_ctrl_f_fallback and is_ctrl_f:
                self.show_window()

            is_ctrl_8 = bool(event.modifiers & carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL) and self._is_digit_8_input(
                event.input
            )
            if use_ctrl_8_fallback and is_ctrl_8:
                self._trigger_layout_quick_load()

            is_ctrl_shift_c = (
                event.input == carb.input.KeyboardInput.C
                and bool(event.modifiers & carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL)
                and bool(event.modifiers & carb.input.KEYBOARD_MODIFIER_FLAG_SHIFT)
            )
            if self._hotkey_fallback_mode and is_ctrl_shift_c:
                self._copy_selected_prim_paths()
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
        if not has_focus:
            return False

        return True

    @staticmethod
    def _keyboard_inputs_for_digit_8() -> list:
        names = (
            "KEY_8",
            "NUM_8",
            "NUMPAD_8",
            "KP_8",
            "D8",
            "_8",
        )
        values = []
        seen = set()
        for name in names:
            key = getattr(carb.input.KeyboardInput, name, None)
            if key is None:
                continue
            key_id = int(key)
            if key_id in seen:
                continue
            seen.add(key_id)
            values.append(key)
        return values

    @staticmethod
    def _preferred_keyboard_input_for_digit_8():
        preferred_names = ("KEY_8", "NUMPAD_8", "NUM_8", "KP_8", "D8", "_8")
        for name in preferred_names:
            key = getattr(carb.input.KeyboardInput, name, None)
            if key is not None:
                return key
        return None

    @staticmethod
    def _is_digit_8_input(key_input) -> bool:
        for known_key in Extension._keyboard_inputs_for_digit_8():
            if key_input == known_key:
                return True

        key_name = getattr(key_input, "name", "") or str(key_input)
        normalized = str(key_name).upper().replace(" ", "")
        if "F8" in normalized:
            return False
        return normalized.endswith("_8") or normalized.endswith("8") and any(
            token in normalized for token in ("KEY", "NUM", "NUMPAD", "KP", "D8")
        )

    def _trigger_layout_quick_load(self):
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

        if not self._menu_trigger_map:
            self._capture_menu_snapshot_once()

        for path in LAYOUT_QUICK_LOAD_PATH_CANDIDATES:
            trigger_fn = self._menu_trigger_map.get(path)
            if not trigger_fn:
                continue
            try:
                trigger_fn()
                carb.log_info(f"[QuickSearchUX] Triggered menu action: {' > '.join(path)}")
                return
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not trigger {' > '.join(path)}: {exc}")

        carb.log_warn("[QuickSearchUX] Quick Load action not found in known actions/paths")

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
        selected_paths = Extension._get_selected_paths()
        if not selected_paths:
            return None
        return selected_paths[0]

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

    def _copy_selected_prim_paths(self):
        selected_paths = self._get_selected_paths()
        if not selected_paths:
            carb.log_info("[QuickSearchUX] No selected prims to copy")
            return

        text = "\n".join(selected_paths)
        if self._copy_text_to_clipboard(text):
            carb.log_info(f"[QuickSearchUX] Copied {len(selected_paths)} selected prim path(s) to clipboard")
        else:
            carb.log_warn("[QuickSearchUX] Could not copy selected prim paths to clipboard")

    def _toggle_selected_prims_active_state(self):
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

                carb.log_info(f"[QuickSearchUX] Prim {path} is not valid on composed stage, trying layer fallback")
                if self._set_inactive_prim_active(stage, path):
                    carb.log_info(f"[QuickSearchUX] Reactivated prim via layer fallback: {path}")
                    toggled_count += 1
                else:
                    carb.log_warn(f"[QuickSearchUX] Layer fallback could not find prim spec: {path}")
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not toggle prim active state for {path}: {exc}")

        if toggled_count:
            carb.log_info(f"[QuickSearchUX] Toggled active state for {toggled_count} selected prim(s)")

    @staticmethod
    def _set_inactive_prim_active(stage, path: str) -> bool:
        layers = []

        try:
            edit_target = stage.GetEditTarget()
            if edit_target:
                edit_layer = edit_target.GetLayer()
                if edit_layer:
                    layers.append(edit_layer)
        except Exception:
            pass

        try:
            session_layer = stage.GetSessionLayer()
            if session_layer:
                layers.append(session_layer)
        except Exception:
            pass

        try:
            root_layer = stage.GetRootLayer()
            if root_layer:
                layers.append(root_layer)
        except Exception:
            pass

        seen = set()
        for layer in layers:
            layer_id = id(layer)
            if layer_id in seen:
                continue
            seen.add(layer_id)
            try:
                layer_name = getattr(layer, "identifier", None) or getattr(layer, "realPath", None) or "<anonymous layer>"
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
