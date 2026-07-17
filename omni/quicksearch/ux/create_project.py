"""Create Project feature.

Adds a ``File > Create Project`` menu entry that asks for a project name, then
scaffolds the standard folder structure, copies the gridroom environment asset,
builds a base USD stage and saves it as ``omniverse/main.usda``.
"""

import os

import carb
import omni.client
import omni.kit.app
import omni.ui as ui
import omni.usd
from pxr import Sdf, UsdGeom

from .paths import normalize_path, omni_result_ok, is_saved_stage

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

_BASE_XFORM_PATHS = (
    "/World/cell",
    "/World/cell/workspace_robot1",
    "/World/cell/workspace_robot1/tools",
    "/World/cell/workspace_robot1/workpieces",
    "/World/cell/workspace_robot1/periphery",
    "/World/cell/workspace_robot2",
    "/World/cell/workspace_robot2/tools",
    "/World/cell/workspace_robot2/workpieces",
    "/World/cell/workspace_robot2/periphery",
    "/World/periphery",
    "/World/environment",
)


class CreateProjectHandler:
    """Registers the File menu entry and performs project scaffolding."""

    def __init__(self, gridroom_asset_source: str):
        self._gridroom_asset_source = gridroom_asset_source
        self._file_menu_items = []
        self._dialog = None
        self._message_window = None

    def register_menu_entry(self):
        try:
            from omni.kit.menu.utils import MenuItemDescription, add_menu_items

            self._file_menu_items = [
                MenuItemDescription(name="Create Project", onclick_fn=self._open_dialog)
            ]
            add_menu_items(self._file_menu_items, "File")
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not register File menu entry: {exc}")
            self._file_menu_items = []

    def deregister_menu_entry(self):
        if not self._file_menu_items:
            return
        try:
            from omni.kit.menu.utils import remove_menu_items

            remove_menu_items(self._file_menu_items, "File")
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not deregister File menu entry: {exc}")
        finally:
            self._file_menu_items = []

    # -- dialog ---------------------------------------------------------------

    def _open_dialog(self):
        try:
            from omni.kit.window.filepicker import FilePickerDialog

            if self._dialog:
                self._dialog.hide()
                self._dialog = None

            self._dialog = FilePickerDialog(
                "Create Project - Enter project name",
                apply_button_label="Create",
                click_apply_handler=self._create_project_from_selection,
                item_filter_options=["Project Name (*)"],
                item_filter_fn=lambda item: True,
            )
            self._dialog.show()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not open Omniverse file browser: {exc}")

    def _create_project_from_selection(self, filename: str, dirname: str):
        try:
            if self._dialog:
                self._dialog.hide()
                self._dialog = None

            project_root, main_usd_path = self._build_project_paths(filename, dirname)
            if not project_root or not main_usd_path:
                carb.log_info("[QuickSearchUX] Create Project canceled")
                return

            context = omni.usd.get_context()
            stage_url = normalize_path(context.get_stage_url())
            if is_saved_stage(stage_url):
                message = (
                    "Current stage is already saved. Please open a new stage first and run "
                    "Create Project again."
                )
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

    # -- path helpers ---------------------------------------------------------

    @staticmethod
    def _sanitize_project_name(value: str) -> str:
        project_name = str(value or "").strip().replace("\\", "/").split("/")[-1]
        if project_name.lower().endswith((".usd", ".usda")):
            project_name = project_name.rsplit(".", 1)[0]
        for char in '<>:"/\\|?*':
            project_name = project_name.replace(char, "")
        return project_name.strip()

    @classmethod
    def _build_project_paths(cls, filename: str, dirname: str) -> tuple[str | None, str | None]:
        project_name = cls._sanitize_project_name(filename)
        base_dir = normalize_path(dirname)
        if not project_name or not base_dir:
            return None, None

        base_dir = base_dir.rstrip("/")
        project_root = f"{base_dir}/{project_name}"
        main_usd_path = f"{project_root}/omniverse/main.usda"
        return project_root, main_usd_path

    # -- folder / file scaffolding -------------------------------------------

    def _create_project_structure(self, project_root: str):
        self._create_folder(project_root)
        for rel_path in CREATE_PROJECT_SUBFOLDERS:
            rel = rel_path.replace("\\", "/")
            target = f"{project_root}/{rel}" if project_root else rel
            self._create_folder(target)

        readme_path = f"{project_root}/README.md" if project_root else "README.md"
        self._write_text_file(readme_path, README_TEMPLATE)

    @staticmethod
    def _create_folder(path: str):
        result = omni.client.create_folder(path)
        if not omni_result_ok(result):
            raise RuntimeError(f"Could not create folder: {path} ({result})")

    @staticmethod
    def _write_text_file(path: str, content: str):
        result = omni.client.write_file(path, content.encode("utf-8"))
        if not omni_result_ok(result):
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
            destination_dir = (
                destination_root if relative_dir == "." else f"{destination_root}/{relative_dir}"
            )
            self._create_folder(destination_dir)

            for file_name in file_names:
                source_file = os.path.join(local_dir, file_name)
                destination_file = f"{destination_dir}/{file_name}"
                with open(source_file, "rb") as source_stream:
                    payload = source_stream.read()
                result = omni.client.write_file(destination_file, payload)
                if not omni_result_ok(result):
                    raise RuntimeError(
                        f"Could not write gridroom asset file: {destination_file} ({result})"
                    )

    # -- stage construction ---------------------------------------------------

    @staticmethod
    def _merge_project_prims_into_current_stage():
        context = omni.usd.get_context()
        stage = context.get_stage()
        if not stage:
            if context.new_stage() is False:
                raise RuntimeError("Could not create new stage")
            stage = context.get_stage()
            if not stage:
                raise RuntimeError("No active stage after creation")

        stage.SetStartTimeCode(0)
        stage.SetEndTimeCode(1000)
        stage.SetTimeCodesPerSecond(60)
        stage.SetMetadata("metersPerUnit", 1.0)
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)

        world = CreateProjectHandler._define_xform_if_missing(stage, "/World")
        stage.SetDefaultPrim(world.GetPrim())

        for path in _BASE_XFORM_PATHS:
            CreateProjectHandler._define_xform_if_missing(stage, path)

        CreateProjectHandler._define_xform_if_missing(stage, "/Environment")
        CreateProjectHandler._define_gridroom_payload(stage)

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

    @staticmethod
    def _open_stage_in_context(main_usd_path: str):
        if omni.usd.get_context().open_stage(main_usd_path) is False:
            carb.log_warn(
                f"[QuickSearchUX] Stage created but could not be opened automatically: {main_usd_path}"
            )

    # -- messaging ------------------------------------------------------------

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
