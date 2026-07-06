import asyncio
import re
from functools import partial
from typing import Callable, Iterable, Optional

import carb
import omni.kit.commands
import omni.kit.undo
import omni.ui as ui
import omni.usd
from pxr import Gf, Sdf, UsdGeom, UsdPhysics


ActionFn = Callable[[], None]
_MENU_SNAPSHOT = {}
_TRIGGER_MAP = {}


def set_menu_snapshot(menu_snapshot: dict, trigger_map: Optional[dict] = None):
    global _MENU_SNAPSHOT, _TRIGGER_MAP
    _MENU_SNAPSHOT = menu_snapshot or {}
    _TRIGGER_MAP = trigger_map or {}


class QuickSearchItem(ui.AbstractItem):
    def __init__(
        self,
        name: str,
        description: str = "",
        action: Optional[ActionFn] = None,
        children: Optional[list["QuickSearchItem"]] = None,
        complete_text: Optional[str] = None,
    ):
        super().__init__()
        self.name = name
        self.description = description
        self.action = action
        self.children = children or []
        self.complete_text = complete_text if complete_text is not None else (description or name)
        self.name_model = ui.SimpleStringModel(self.name)
        self.description_model = ui.SimpleStringModel(self.description)
        self.icon_model = ui.SimpleStringModel("")
        self.tooltip_model = ui.SimpleStringModel(self.description)


class UnifiedQuickSearchModel(ui.AbstractItemModel):
    def __init__(self):
        super().__init__()
        self._action_map, self._actions_by_leaf = self._build_menu_action_maps()
        self._items = self._build_items()

    def destroy(self):
        self._items = []
        self._action_map = {}
        self._actions_by_leaf = {}

    def get_item_children(self, item):
        if item is None:
            return self._items
        return item.children

    def get_item_value_model_count(self, item):
        return 4

    def get_item_value_model(self, item, column_id):
        if item is None:
            return None
        if column_id == 0:
            return item.name_model
        if column_id == 1:
            return item.description_model
        if column_id == 2:
            return item.icon_model
        if column_id == 3:
            return item.tooltip_model
        return None

    def execute(self, item):
        if item and item.action:
            item.action()

    def complete(self, current_value: str, item) -> str:
        return item.complete_text if item else current_value

    def _build_items(self) -> list[QuickSearchItem]:
        items = []
        menu_root = self._build_menu_root()
        if menu_root:
            items.append(menu_root)
        items.extend(self._build_stage_roots())
        return items

    def _build_menu_root(self) -> Optional[QuickSearchItem]:
        children = []
        seen = set()

        menu_source = _MENU_SNAPSHOT
        if not menu_source:
            menu_source = self._build_menu_dict_from_actions()

        for menu_name, entries in menu_source.items():
            if menu_name == "_":
                continue
            menu_children = list(self._flatten_menu_snapshot_entries(entries, (menu_name,), seen))
            if menu_children:
                children.append(QuickSearchItem(menu_name, menu_name, children=menu_children, complete_text=menu_name))

        if not children:
            return None

        return QuickSearchItem("Menu Bar", "Isaac Sim menu actions", children=children, complete_text="Menu Bar")

    def _build_menu_dict_from_actions(self) -> dict:
        menu_dict = {}
        for path in self._action_map:
            if len(path) < 2:
                continue
            output = menu_dict.setdefault(path[0], {})
            for name in path[1:-1]:
                output = output.setdefault(name, {})
            output.setdefault("_", []).append(path[-1])
        return menu_dict

    def _build_menu_action_maps(self) -> tuple[dict[tuple[str, ...], ActionFn], dict[str, list[tuple[tuple[str, ...], ActionFn]]]]:
        try:
            import omni.kit.menu.utils

            menus = omni.kit.menu.utils.get_merged_menus() or {}
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not read menu actions: {exc}")
            return {}, {}

        action_map = {}
        for menu_name, entries in menus.items():
            self._collect_menu_actions(entries, (self._clean_name(menu_name),), action_map)

        actions_by_leaf = {}
        for path, action in action_map.items():
            if path:
                actions_by_leaf.setdefault(path[-1], []).append((path, action))
        return action_map, actions_by_leaf

    def _collect_menu_actions(self, entries, prefix: tuple[str, ...], action_map: dict[tuple[str, ...], ActionFn]):
        for entry in entries or []:
            name = self._menu_entry_name(entry)
            if not name:
                continue

            path = (*prefix, name)
            sub_menu = getattr(entry, "sub_menu", None)
            if sub_menu:
                self._collect_menu_actions(sub_menu, path, action_map)
                continue

            action = self._menu_entry_action(entry, path)
            if action:
                action_map[path] = action

    def _flatten_menu_snapshot_entries(self, entries: dict, prefix: tuple[str, ...], seen: set[str]):
        if not isinstance(entries, dict):
            return

        for name, sub_menu in (entries or {}).items():
            if name == "_":
                continue
            path = (*prefix, name)
            path_text = " > ".join(path)
            children = list(self._flatten_menu_snapshot_entries(sub_menu, path, seen))
            if children:
                yield QuickSearchItem(name, path_text, children=children, complete_text=path_text)

        for name in (entries or {}).get("_", []):
            path = (*prefix, name)
            path_text = " > ".join(path)
            if path_text in seen:
                continue
            seen.add(path_text)
            action = partial(self._execute_menu_path, path)
            yield QuickSearchItem(name, path_text, action, complete_text=path_text)

    def _menu_entry_name(self, entry) -> str:
        name_fn = getattr(entry, "name_fn", None)
        if name_fn:
            try:
                return self._clean_name(name_fn())
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not resolve dynamic menu name: {exc}")
        return self._clean_name(getattr(entry, "name", ""))

    def _clean_name(self, name) -> str:
        return re.sub(r"[^\x00-\x7F]+", " ", str(name or "")).lstrip()

    def _menu_entry_action(self, entry, path: tuple[str, ...]) -> Optional[ActionFn]:
        onclick_action = getattr(entry, "onclick_action", None)
        unclick_action = getattr(entry, "unclick_action", None)
        if onclick_action and unclick_action:
            return partial(self._execute_actions, (onclick_action, unclick_action), path)
        if onclick_action:
            return partial(self._execute_actions, (onclick_action,), path)

        onclick_fn = getattr(entry, "onclick_fn", None)
        unclick_fn = getattr(entry, "unclick_fn", None)
        if onclick_fn and unclick_fn:
            return partial(self._execute_fns, (onclick_fn, unclick_fn), path)
        if onclick_fn:
            return partial(self._execute_fns, (onclick_fn,), path)
        return None

    def _resolve_menu_action(self, path: tuple[str, ...]) -> Optional[ActionFn]:
        trigger_fn = _TRIGGER_MAP.get(path)
        if trigger_fn:
            return partial(self._execute_trigger_fn, trigger_fn, path)

        action = self._action_map.get(path)
        if action:
            return action

        suffix_matches = [action for action_path, action in self._action_map.items() if self._endswith(action_path, path)]
        if len(suffix_matches) == 1:
            return suffix_matches[0]

        leaf_matches = self._actions_by_leaf.get(path[-1], [])
        if len(leaf_matches) == 1:
            return leaf_matches[0][1]

        return None

    def _endswith(self, candidate: tuple[str, ...], suffix: tuple[str, ...]) -> bool:
        return len(candidate) >= len(suffix) and candidate[-len(suffix) :] == suffix

    def _execute_menu_path(self, path: tuple[str, ...]):
        action = self._resolve_menu_action(path)
        if not action:
            carb.log_warn(f"[QuickSearchUX] No direct operator found for menubar path {' > '.join(path)}")
            return
        action()

    def _execute_trigger_fn(self, trigger_fn: ActionFn, path: tuple[str, ...]):
        try:
            trigger_fn()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not execute menubar trigger {' > '.join(path)}: {exc}")

    def _execute_actions(self, actions: tuple[tuple, ...], path: tuple[str, ...]):
        try:
            import omni.kit.app
            import omni.kit.actions.core
            from omni.kit.menu.utils import MenuActionControl

            async_delay = all(MenuActionControl.NODELAY not in action for action in actions)
            cleaned_actions = [
                tuple(item for item in action if item not in (MenuActionControl.NONE, MenuActionControl.NODELAY))
                for action in actions
            ]

            def execute_all():
                for action in cleaned_actions:
                    omni.kit.actions.core.execute_action(*action)

            if async_delay:

                async def execute_later():
                    await omni.kit.app.get_app().next_update_async()
                    execute_all()

                asyncio.ensure_future(execute_later())
            else:
                execute_all()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not execute menu action {' > '.join(path)}: {exc}")

    def _execute_fns(self, fns: tuple[ActionFn, ...], path: tuple[str, ...]):
        try:
            for fn in fns:
                fn()
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not execute menu function {' > '.join(path)}: {exc}")

    def _build_stage_roots(self) -> list[QuickSearchItem]:
        roots = [
            QuickSearchItem("Create Mesh", "Create mesh prims in the current stage", self._noop),
            QuickSearchItem("Create Shape", "Create USD geometry prims", self._noop),
            QuickSearchItem("Create Light", "Create USD light prims", self._noop),
            QuickSearchItem("Create", "Create common USD prims", self._noop),
            QuickSearchItem("Physics Create", "Create physics scene assets", self._noop),
            QuickSearchItem("Physics Add", "Apply physics APIs/presets to selection", self._noop),
            QuickSearchItem("Stage Menu", "Common Stage right-click actions", self._noop),
            QuickSearchItem(
                "Registered Stage Create",
                "Entries registered into the Stage/Viewport Create context menu",
                self._noop,
            ),
            QuickSearchItem(
                "Registered Stage Add",
                "Entries registered into the Stage/Viewport Add context menu",
                self._noop,
            ),
        ]

        roots[0].children = self._mesh_items()
        roots[1].children = self._shape_items()
        roots[2].children = self._light_items()
        roots[3].children = self._common_create_items()
        roots[4].children = self._physics_create_items()
        roots[5].children = self._physics_add_items()
        roots[6].children = self._stage_menu_items()
        roots[7].children = self._registered_menu_items("CREATE")
        roots[8].children = self._registered_menu_items("ADD")

        return [root for root in roots if root.children]

    def _mesh_items(self) -> list[QuickSearchItem]:
        try:
            import omni.kit.primitive.mesh

            mesh_names = omni.kit.primitive.mesh.get_geometry_mesh_prim_list()
        except Exception:
            mesh_names = ["Cube", "Sphere", "Cone", "Cylinder", "Disk", "Plane", "Torus"]

        return [
            QuickSearchItem(mesh, f"Create Mesh > {mesh}", partial(self._create_mesh_prim, mesh), complete_text=mesh)
            for mesh in mesh_names
        ]

    def _shape_items(self) -> list[QuickSearchItem]:
        try:
            shapes = omni.usd.get_geometry_standard_prim_list()
        except Exception:
            shapes = {}

        return [
            QuickSearchItem(
                prim_type,
                f"Create Shape > {prim_type}",
                partial(self._create_prim, prim_type, attrs),
                complete_text=prim_type,
            )
            for prim_type, attrs in shapes.items()
        ]

    def _light_items(self) -> list[QuickSearchItem]:
        try:
            lights = omni.usd.get_light_prim_list()
        except Exception:
            lights = []

        return [
            QuickSearchItem(
                label,
                f"Create Light > {label}",
                partial(self._create_prim, prim_type, attrs),
                complete_text=label,
            )
            for label, prim_type, attrs in lights
        ]

    def _common_create_items(self) -> list[QuickSearchItem]:
        return [
            QuickSearchItem("Camera", "Create > Camera", partial(self._create_prim, "Camera", {}), complete_text="Camera"),
            QuickSearchItem("Scope", "Create > Scope", partial(self._create_prim, "Scope", {}), complete_text="Scope"),
            QuickSearchItem("Xform", "Create > Xform", partial(self._create_prim, "Xform", {}), complete_text="Xform"),
        ]

    def _physics_create_items(self) -> list[QuickSearchItem]:
        return [
            QuickSearchItem("Physics Scene", "Physics Create > Physics Scene", self._create_physics_scene, complete_text="Physics Scene"),
            QuickSearchItem("Ground Plane", "Physics Create > Ground Plane", self._create_ground_plane, complete_text="Ground Plane"),
            QuickSearchItem("Collision Group", "Physics Create > Collision Group", self._create_collision_group, complete_text="Collision Group"),
            QuickSearchItem(
                "Rigid Body Material",
                "Physics Create > Rigid Body Material",
                self._create_rigid_body_material,
                complete_text="Rigid Body Material",
            ),
        ]

    def _physics_add_items(self) -> list[QuickSearchItem]:
        approximations = [
            ("None", UsdPhysics.Tokens.none),
            ("Convex Hull", UsdPhysics.Tokens.convexHull),
            ("Convex Decomposition", UsdPhysics.Tokens.convexDecomposition),
            ("Mesh Simplification", UsdPhysics.Tokens.meshSimplification),
            ("Bounding Cube", UsdPhysics.Tokens.boundingCube),
            ("Bounding Sphere", UsdPhysics.Tokens.boundingSphere),
        ]

        items = [
            QuickSearchItem(
                "Rigid Body with Colliders Preset",
                "Physics Add > Rigid Body with Colliders Preset",
                partial(self._apply_rigid_body, UsdPhysics.Tokens.convexHull, False),
                complete_text="Rigid Body with Colliders Preset",
            ),
            QuickSearchItem(
                "Kinematic Rigid Body with Colliders Preset",
                "Physics Add > Kinematic Rigid Body with Colliders Preset",
                partial(self._apply_rigid_body, UsdPhysics.Tokens.convexHull, True),
                complete_text="Kinematic Rigid Body with Colliders Preset",
            ),
            QuickSearchItem(
                "Colliders Preset",
                "Physics Add > Colliders Preset",
                partial(self._apply_static_collider, UsdPhysics.Tokens.none),
                complete_text="Colliders Preset",
            ),
        ]
        items.extend(
            QuickSearchItem(
                f"Rigid Body Collider: {name}",
                f"Physics Add > Rigid Body Collider > {name}",
                partial(self._apply_rigid_body, token, False),
                complete_text=f"Rigid Body Collider: {name}",
            )
            for name, token in approximations
        )
        items.extend(
            QuickSearchItem(
                f"Static Collider: {name}",
                f"Physics Add > Static Collider > {name}",
                partial(self._apply_static_collider, token),
                complete_text=f"Static Collider: {name}",
            )
            for name, token in approximations
        )
        return items

    def _registered_menu_items(self, menu_type: str) -> list[QuickSearchItem]:
        menu_entries = []
        try:
            import omni.kit.context_menu

            for extension_id in ("", "omni.kit.widget.stage", "omni.kit.viewport.window"):
                menu_entries.extend(omni.kit.context_menu.get_menu_dict(menu_type, extension_id))
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not read {menu_type} context menu entries: {exc}")

        seen = set()
        return list(self._flatten_stage_menu_entries(menu_entries, seen))

    def _stage_menu_items(self) -> list[QuickSearchItem]:
        return [
            QuickSearchItem(
                "Find in Content Browser",
                "Stage Menu > Find in Content Browser",
                self._context_menu_action("find_in_browser"),
                complete_text="Find in Content Browser",
            ),
            QuickSearchItem(
                "Group Selected",
                "Stage Menu > Group Selected",
                self._context_menu_action("group_selected_prims"),
                complete_text="Group Selected",
            ),
            QuickSearchItem(
                "Ungroup Selected",
                "Stage Menu > Ungroup Selected",
                self._context_menu_action("ungroup_selected_prims"),
                complete_text="Ungroup Selected",
            ),
            QuickSearchItem(
                "Duplicate",
                "Stage Menu > Duplicate",
                self._context_menu_action("duplicate_prim"),
                complete_text="Duplicate",
            ),
            QuickSearchItem("Delete", "Stage Menu > Delete", self._context_menu_action("delete_prim"), complete_text="Delete"),
            QuickSearchItem(
                "Refresh Reference",
                "Stage Menu > Refresh Reference",
                self._context_menu_action("refresh_payload_or_reference"),
                complete_text="Refresh Reference",
            ),
            QuickSearchItem(
                "Convert Payloads to References",
                "Stage Menu > Convert Payloads to References",
                self._context_menu_action("convert_payload_to_reference"),
                complete_text="Convert Payloads to References",
            ),
            QuickSearchItem(
                "Convert References to Payloads",
                "Stage Menu > Convert References to Payloads",
                self._context_menu_action("convert_reference_to_payload"),
                complete_text="Convert References to Payloads",
            ),
            QuickSearchItem(
                "Select Bound Objects",
                "Stage Menu > Select Bound Objects",
                self._context_menu_action("select_prims_using_material"),
                complete_text="Select Bound Objects",
            ),
            QuickSearchItem(
                "Copy URL Link",
                "Stage Menu > Copy URL Link",
                self._context_menu_action("copy_prim_url"),
                complete_text="Copy URL Link",
            ),
            QuickSearchItem(
                "Copy Prim Path",
                "Stage Menu > Copy Prim Path",
                self._context_menu_action("copy_prim_path"),
                complete_text="Copy Prim Path",
            ),
        ]

    def _flatten_stage_menu_entries(self, entries: Iterable, seen: set[str], prefix: str = ""):
        for entry in entries:
            if isinstance(entry, list):
                yield from self._flatten_stage_menu_entries(entry, seen, prefix)
                continue
            if not isinstance(entry, dict):
                continue
            if "populate_fn" in entry:
                continue
            name = entry.get("name")
            if isinstance(name, dict):
                for group, children in name.items():
                    yield from self._flatten_stage_menu_entries(children, seen, f"{prefix}{group} > ")
                continue
            if not name or str(name).endswith("/"):
                continue
            label = f"{prefix}{name}"
            if label in seen:
                continue
            action = self._stage_entry_action(entry)
            if not action:
                continue
            seen.add(label)
            yield QuickSearchItem(str(name), label, action, complete_text=str(name))

    def _stage_entry_action(self, entry: dict) -> Optional[ActionFn]:
        if entry.get("onclick_fn"):
            return partial(self._execute_menu_fn, entry)
        if entry.get("onclick_action"):
            return partial(self._execute_menu_action, entry)
        return None

    def _context_menu_action(self, method_name: str) -> ActionFn:
        def execute():
            try:
                import omni.kit.context_menu

                context_menu = omni.kit.context_menu.get_instance()
                if not context_menu:
                    carb.log_warn("[QuickSearchUX] omni.kit.context_menu is not available")
                    return
                getattr(context_menu, method_name)(self._objects())
            except Exception as exc:
                carb.log_warn(f"[QuickSearchUX] Could not execute Stage menu action {method_name}: {exc}")

        return execute

    def _objects(self) -> dict:
        context = omni.usd.get_context()
        stage = context.get_stage()
        objects = {"stage": stage, "usd_context_name": context.get_name()}
        selected_paths = context.get_selection().get_selected_prim_paths()
        prims = []
        if stage:
            for path in selected_paths:
                prim = stage.GetPrimAtPath(path)
                if prim and prim.IsValid():
                    prims.append(prim)
        if prims:
            objects["prim_list"] = prims
            objects["prim"] = prims[0]
        return objects

    def _selected_paths(self) -> list[str]:
        return [prim.GetPath().pathString for prim in self._objects().get("prim_list", [])]

    def _get_create_parent_path(self) -> str:
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return "/"

        for path in omni.usd.get_context().get_selection().get_selected_prim_paths():
            prim = stage.GetPrimAtPath(path)
            if prim and prim.IsValid() and prim.GetPath() != Sdf.Path.absoluteRootPath:
                return prim.GetPath().pathString

        default_prim = stage.GetDefaultPrim()
        if default_prim and default_prim.IsValid():
            return default_prim.GetPath().pathString
        return "/"

    @staticmethod
    def _child_path(parent_path: str, name: str) -> str:
        if parent_path == "/":
            return f"/{name}"
        return f"{parent_path}/{name}"

    def _create_mesh_prim(self, prim_type: str):
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return

        parent_path = self._get_create_parent_path()
        prim_path = omni.usd.get_stage_next_free_path(stage, self._child_path(parent_path, prim_type), False)
        omni.kit.commands.execute(
            "CreateMeshPrimWithDefaultXform",
            prim_type=prim_type,
            prim_path=prim_path,
            select_new_prim=True,
            prepend_default_prim=False,
            above_ground=True,
        )

    def _create_prim(self, prim_type: str, attributes: dict):
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return

        parent_path = self._get_create_parent_path()
        prim_path = omni.usd.get_stage_next_free_path(stage, self._child_path(parent_path, prim_type), False)
        omni.kit.commands.execute(
            "CreatePrimWithDefaultXform",
            prim_type=prim_type,
            prim_path=prim_path,
            attributes=attributes,
            select_new_prim=True,
        )

    def _create_physics_scene(self):
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return
        path = omni.usd.get_stage_next_free_path(stage, "/World/PhysicsScene", False)
        omni.kit.commands.execute("AddPhysicsScene", stage=stage, path=path)

    def _create_ground_plane(self):
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return
        path = omni.usd.get_stage_next_free_path(stage, "/World/GroundPlane", False)
        up_axis = UsdGeom.GetStageUpAxis(stage)
        omni.kit.commands.execute(
            "AddGroundPlane",
            stage=stage,
            planePath=path,
            axis=up_axis,
            size=1000.0,
            position=Gf.Vec3f(0.0, 0.0, 0.0),
            color=Gf.Vec3f(0.5, 0.5, 0.5),
        )

    def _create_collision_group(self):
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return
        path = omni.usd.get_stage_next_free_path(stage, "/World/CollisionGroup", False)
        omni.kit.commands.execute("AddCollisionGroup", stage=stage, path=path)

    def _create_rigid_body_material(self):
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return
        path = omni.usd.get_stage_next_free_path(stage, "/World/PhysicsMaterial", False)
        omni.kit.commands.execute("AddRigidBodyMaterial", stage=stage, path=Sdf.Path(path))

    def _apply_rigid_body(self, approximation, kinematic: bool):
        self._execute_for_selection(
            lambda path: omni.kit.commands.execute(
                "SetRigidBody",
                path=Sdf.Path(path),
                approximationShape=approximation,
                kinematic=kinematic,
            )
        )

    def _apply_static_collider(self, approximation):
        self._execute_for_selection(
            lambda path: omni.kit.commands.execute(
                "SetStaticCollider",
                path=Sdf.Path(path),
                approximationShape=approximation,
            )
        )

    def _execute_for_selection(self, fn: Callable[[str], None]):
        paths = self._selected_paths()
        if not paths:
            carb.log_warn("[QuickSearchUX] Select one or more prims before running this action")
            return
        with omni.kit.undo.group():
            for path in paths:
                fn(path)

    def _execute_menu_fn(self, entry: dict):
        objects = self._objects()
        if not self._menu_entry_visible(entry, objects):
            carb.log_warn(f"[QuickSearchUX] Menu entry is not available now: {entry.get('name')}")
            return
        entry["onclick_fn"](objects)

    def _execute_menu_action(self, entry: dict):
        objects = self._objects()
        if not self._menu_entry_visible(entry, objects):
            carb.log_warn(f"[QuickSearchUX] Menu action is not available now: {entry.get('name')}")
            return
        try:
            import omni.kit.actions.core

            extension_id, action_id = entry["onclick_action"]
            action = omni.kit.actions.core.acquire_action_registry().get_action(extension_id, action_id)
            params = {key: objects[key] for key in action.parameters if key in objects}
            omni.kit.actions.core.execute_action(extension_id, action_id, **params)
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not execute action {entry.get('name')}: {exc}")

    def _menu_entry_visible(self, entry: dict, objects: dict) -> bool:
        for key in ("show_fn", "enabled_fn"):
            fn = entry.get(key)
            if not fn:
                continue
            try:
                if isinstance(fn, list):
                    if not all(item(objects) for item in fn):
                        return False
                elif not fn(objects):
                    return False
            except Exception:
                return False
        return True

    def _noop(self):
        pass
