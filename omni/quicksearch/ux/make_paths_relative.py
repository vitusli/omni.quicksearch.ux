"""File menu action to convert absolute USD asset paths to relative paths.

Shows a preview popup with all candidate path rewrites. Optionally collects
online/external assets into the root layer folder before rewriting paths.
"""

import asyncio
import hashlib
import os
import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import carb
import omni.client
import omni.ui as ui
import omni.usd
from pxr import Sdf

from .paths import normalize_path, omni_result_ok, to_local_filesystem_path

_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_WIN_ABS_RE = re.compile(r"^[a-zA-Z]:[\\/]")
_DEBUG = True
_DEBUG_TO_STDOUT = True


@dataclass
class _PathCandidate:
    layer: object
    layer_name: str
    kind: str
    owner_path: str
    old_path: str
    proposed_path: str
    can_make_relative: bool
    collect_recommended: bool
    collect_reason: str
    collect_source_path: str | None
    apply_fn: callable


_COL_STYLE = {
    "Rectangle::row_even": {"background_color": 0xFF2A2A2A},
    "Rectangle::row_odd": {"background_color": 0xFF242424},
    "Rectangle::row_sel": {"background_color": 0xFF3A5A78},
    "Rectangle::header_bg": {"background_color": 0xFF1B1B1B},
    "Rectangle::group_bg": {"background_color": 0xFF303030},
    "Label::header": {"color": 0xFFBBBBBB, "font_size": 13},
    "Label::group": {"color": 0xFFE0E0E0, "font_size": 14},
    "Label::cell": {"color": 0xFFD0D0D0, "font_size": 13},
    "Label::badge_collect": {"color": 0xFF66B2FF, "font_size": 13},
    "Label::badge_relative": {"color": 0xFF8FD08F, "font_size": 13},
    "Label::detail_key": {"color": 0xFF9A9A9A, "font_size": 13},
    "Label::detail_val": {"color": 0xFFEAEAEA, "font_size": 13},
    "Rectangle::detail_bg": {"background_color": 0xFF1E1E1E},
}


class _GroupRow(ui.AbstractItem):
    """A layer-group header row."""

    def __init__(self, layer_name: str, count: int):
        super().__init__()
        self.layer_name = layer_name
        self.count = count


class _CandidateRow(ui.AbstractItem):
    """A single candidate row bound to a _PathCandidate."""

    def __init__(self, number: int, candidate: _PathCandidate):
        super().__init__()
        self.number = number
        self.candidate = candidate


class _CandidateTableModel(ui.AbstractItemModel):
    """Flat model with interleaved group-header rows and candidate rows."""

    COLUMNS = ("#", "Type", "Kind", "Prim / Attribute", "Reason", "Status")

    def __init__(self, candidates: list[_PathCandidate]):
        super().__init__()
        self._rows: list = []
        by_layer: dict[str, list[_PathCandidate]] = {}
        for cand in candidates:
            by_layer.setdefault(cand.layer_name, []).append(cand)

        number = 0
        for layer_name in sorted(by_layer.keys()):
            group_items = sorted(
                by_layer[layer_name], key=lambda c: (c.kind, c.owner_path, c.old_path)
            )
            self._rows.append(_GroupRow(layer_name, len(group_items)))
            for cand in group_items:
                number += 1
                self._rows.append(_CandidateRow(number, cand))

    def get_item_children(self, item):
        if item is not None:
            return []
        return self._rows

    def get_item_value_model_count(self, item):
        return len(self.COLUMNS)

    def get_item_value_model(self, item, column_id: int):
        return None


class _CandidateTableDelegate(ui.AbstractItemDelegate):
    """Renders group headers and candidate rows with zebra striping."""

    COLUMN_WIDTHS = (44, 90, 200, ui.Fraction(1.0), 130, 120)

    def __init__(self, on_select):
        super().__init__()
        self._on_select = on_select
        self._row_counter = 0

    def build_branch(self, model, item, column_id, level, expanded):
        return

    def build_header(self, column_id: int = 0):
        with ui.ZStack(height=26):
            ui.Rectangle(name="header_bg")
            with ui.HStack():
                ui.Spacer(width=8)
                ui.Label(_CandidateTableModel.COLUMNS[column_id], name="header")

    def build_widget(self, model, item, column_id, level, expanded):
        if isinstance(item, _GroupRow):
            self._build_group_cell(item, column_id)
        elif isinstance(item, _CandidateRow):
            self._build_candidate_cell(item, column_id)

    def _build_group_cell(self, item: _GroupRow, column_id: int):
        with ui.ZStack(height=28):
            ui.Rectangle(name="group_bg")
            if column_id == 0:
                with ui.HStack():
                    ui.Spacer(width=8)
                    leaf = os.path.basename(item.layer_name) or item.layer_name
                    ui.Label(
                        f"{leaf}   ({item.count})",
                        name="group",
                        tooltip=item.layer_name,
                    )

    def _build_candidate_cell(self, item: _CandidateRow, column_id: int):
        cand = item.candidate
        row_bg = "row_even" if item.number % 2 == 0 else "row_odd"

        with ui.ZStack(height=26):
            ui.Rectangle(name=row_bg)
            btn = ui.InvisibleButton()
            btn.set_mouse_pressed_fn(lambda x, y, b, m, _c=cand: self._on_select(_c))

            with ui.HStack():
                ui.Spacer(width=8)
                if column_id == 0:
                    ui.Label(str(item.number), name="cell")
                elif column_id == 1:
                    if cand.collect_recommended:
                        ui.Label("collect", name="badge_collect")
                    else:
                        ui.Label("relative", name="badge_relative")
                elif column_id == 2:
                    ui.Label(cand.kind, name="cell", tooltip=cand.kind)
                elif column_id == 3:
                    owner = cand.owner_path
                    ui.Label(owner, name="cell", tooltip=owner)
                elif column_id == 4:
                    ui.Label(cand.collect_reason or "-", name="cell")
                elif column_id == 5:
                    if cand.collect_recommended and not cand.can_make_relative:
                        ui.Label("needs collect", name="cell")
                    else:
                        ui.Label("ready", name="cell")


class MakePathsRelativeHandler:
    """Registers ``File > Make all paths relative`` and executes the rewrite."""

    def __init__(self):
        self._file_menu_items = []
        self._message_window = None
        self._preview_window = None
        self._collect_checkbox_model = None
        self._preview_model = None
        self._preview_delegate = None
        self._preview_detail_models = None

    def register_menu_entry(self):
        try:
            from omni.kit.menu.utils import MenuItemDescription, add_menu_items

            self._file_menu_items = [
                MenuItemDescription(
                    name="Make all paths relative",
                    onclick_fn=self._open_preview_dialog,
                )
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

    def _open_preview_dialog(self):
        try:
            context = omni.usd.get_context()
            stage = context.get_stage()
            if not stage:
                self._show_message("Make all paths relative", "No active stage found.")
                return

            root_layer = stage.GetRootLayer()
            root_location = self._layer_location(root_layer)
            self._debug(f"root_layer={normalize_path(getattr(root_layer, 'identifier', '') or '<none>')}")
            self._debug(f"root_location={root_location}")
            if not root_location:
                self._show_message(
                    "Make all paths relative",
                    "Could not determine root layer location.",
                )
                return

            candidates = self._build_candidates(stage, root_location)
            self._debug(f"candidate_count={len(candidates)}")
            if not candidates:
                self._show_message(
                    "Make all paths relative",
                    "No absolute paths found that can be rewritten.",
                )
                return

            self._show_preview(candidates, root_location)
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not prepare path preview: {exc}")
            self._show_message(
                "Make all paths relative",
                f"Could not prepare preview.\n\nReason: {exc}",
            )

    def _show_preview(self, candidates: list[_PathCandidate], root_location: str):
        self._close_preview()

        collect_candidates = [c for c in candidates if c.collect_recommended]
        direct_candidates = [c for c in candidates if not c.collect_recommended]
        default_collect = bool(collect_candidates)

        collect_target = _Collector.preview_target_dir(root_location)

        self._preview_window = ui.Window(
            "Make all paths relative",
            width=1180,
            height=760,
            flags=ui.WINDOW_FLAGS_MODAL,
        )

        collect_model = ui.SimpleBoolModel(default_collect)
        self._collect_checkbox_model = collect_model

        detail_owner = ui.SimpleStringModel("")
        detail_layer = ui.SimpleStringModel("")
        detail_from = ui.SimpleStringModel("")
        detail_to = ui.SimpleStringModel("")

        def _on_select(cand: _PathCandidate):
            target = cand.proposed_path if cand.can_make_relative else "(will be resolved after collect)"
            detail_owner.set_value(cand.owner_path)
            detail_layer.set_value(cand.layer_name)
            detail_from.set_value(cand.old_path)
            detail_to.set_value(target)

        model = _CandidateTableModel(candidates)
        delegate = _CandidateTableDelegate(_on_select)
        # Keep strong references so the C++ side does not GC the Python model.
        self._preview_model = model
        self._preview_delegate = delegate
        self._preview_detail_models = (detail_owner, detail_layer, detail_from, detail_to)

        with self._preview_window.frame:
            with ui.VStack(spacing=0, style=_COL_STYLE):
                # ---- Summary bar ----
                with ui.ZStack(height=64):
                    ui.Rectangle(name="header_bg")
                    with ui.HStack():
                        ui.Spacer(width=12)
                        with ui.VStack(spacing=4):
                            ui.Spacer(height=8)
                            ui.Label(
                                f"{len(candidates)} path(s) can be rewritten",
                                name="group",
                            )
                            ui.Label(
                                f"Root: {root_location}",
                                name="detail_key",
                                tooltip=root_location,
                            )
                            ui.Spacer(height=8)
                        with ui.VStack(width=280, spacing=4):
                            ui.Spacer(height=10)
                            ui.Label(
                                f"Collect candidates: {len(collect_candidates)}",
                                name="badge_collect",
                            )
                            ui.Label(
                                f"Direct relative: {len(direct_candidates)}",
                                name="badge_relative",
                            )
                            ui.Spacer(height=10)
                        ui.Spacer(width=12)

                ui.Spacer(height=6)

                # ---- Collect option ----
                with ui.HStack(height=26):
                    ui.Spacer(width=12)
                    if collect_candidates:
                        ui.CheckBox(model=collect_model, width=22)
                        ui.Label(
                            "Collect online / outside-root assets into "
                            f"'{os.path.basename(collect_target)}' before rewriting",
                            name="cell",
                            tooltip=collect_target,
                        )
                    else:
                        ui.Label(
                            "No online / external paths detected. Collect not needed.",
                            name="cell",
                        )
                    ui.Spacer()

                ui.Spacer(height=6)

                # ---- Table ----
                with ui.ScrollingFrame(
                    height=ui.Fraction(1),
                    horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF,
                ):
                    ui.TreeView(
                        model,
                        delegate=delegate,
                        root_visible=False,
                        header_visible=True,
                        column_widths=list(delegate.COLUMN_WIDTHS),
                    )

                # ---- Detail panel ----
                with ui.ZStack(height=104):
                    ui.Rectangle(name="detail_bg")
                    with ui.HStack():
                        ui.Spacer(width=12)
                        with ui.VStack(spacing=3):
                            ui.Spacer(height=6)
                            self._detail_line("Prim:", detail_owner)
                            self._detail_line("Layer:", detail_layer)
                            self._detail_line("From:", detail_from)
                            self._detail_line("To:", detail_to)
                            ui.Spacer(height=6)
                        ui.Spacer(width=12)

                # ---- Buttons ----
                with ui.HStack(height=44):
                    ui.Spacer()

                    def _apply():
                        do_collect = bool(collect_model.as_bool)
                        self._debug(f"apply_clicked collect_enabled={do_collect}")
                        self._apply_candidates(candidates, root_location, do_collect)

                    ui.Button("Cancel", width=120, height=30, clicked_fn=self._close_preview)
                    ui.Spacer(width=8)
                    ui.Button("Apply", width=120, height=30, clicked_fn=_apply)
                    ui.Spacer(width=12)

    @staticmethod
    def _detail_line(key: str, model: ui.SimpleStringModel):
        with ui.HStack(height=18, spacing=8):
            ui.Label(key, name="detail_key", width=52)
            ui.StringField(model=model, read_only=True, name="detail_val")

    def _close_preview(self):
        if self._preview_window:
            try:
                self._preview_window.visible = False
            except Exception:
                pass
            self._preview_window = None

    def _apply_candidates(
        self,
        candidates: list[_PathCandidate],
        root_location: str,
        collect_enabled: bool,
    ):
        self._close_preview()
        asyncio.ensure_future(
            self._apply_candidates_async(candidates, root_location, collect_enabled)
        )

    async def _apply_candidates_async(
        self,
        candidates: list[_PathCandidate],
        root_location: str,
        collect_enabled: bool,
    ):
        try:
            collector = _Collector(root_location)
            changed_layers = set()
            changed_count = 0
            log_lines = []

            for candidate in candidates:
                new_path = None
                if collect_enabled and candidate.collect_recommended:
                    collect_source = candidate.collect_source_path or candidate.old_path
                    self._debug(
                        f"collect_try kind={candidate.kind} prim={candidate.owner_path} "
                        f"source={collect_source}"
                    )
                    is_usd_ref = candidate.kind.startswith(("reference", "payload", "sublayer"))
                    collected_target = await collector.collect_async(collect_source, is_usd=is_usd_ref)
                    if collected_target:
                        self._debug(f"collect_ok source={collect_source} target={collected_target}")
                        new_path = self._to_relative_asset_path(collected_target, candidate.layer)
                    else:
                        self._debug(f"collect_failed source={collect_source}")
                        continue

                if not new_path and candidate.can_make_relative:
                    new_path = candidate.proposed_path

                if not new_path or new_path == candidate.old_path:
                    continue

                candidate.apply_fn(new_path)
                changed_count += 1
                changed_layers.add(candidate.layer)
                log_lines.append(
                    f"layer={candidate.layer_name} kind={candidate.kind} prim={candidate.owner_path} "
                    f"from='{candidate.old_path}' to='{new_path}'"
                )
                self._debug(
                    f"applied kind={candidate.kind} prim={candidate.owner_path} "
                    f"from={candidate.old_path} to={new_path}"
                )

            failed_layers = []
            for layer in changed_layers:
                try:
                    layer.Save()
                except Exception as exc:
                    failed_layers.append(f"{layer.identifier}: {exc}")
                    carb.log_warn(f"[QuickSearchUX] Could not save layer {layer.identifier}: {exc}")

            if changed_count == 0:
                message = "No paths were changed."
                if collect_enabled and collector.failures:
                    message = f"{message}\n\nCollect failures occurred. Check the log."
                self._show_message("Make all paths relative", message)
                return

            for line in log_lines:
                carb.log_info(f"[QuickSearchUX] {line}")

            if collector.failures:
                for failure in collector.failures:
                    carb.log_warn(f"[QuickSearchUX] Collect failed: {failure}")

            summary = (
                f"Changed {changed_count} path(s) in {len(changed_layers)} layer(s). "
                "See log for full path list."
            )
            self._debug(
                f"apply_done changed_count={changed_count} changed_layers={len(changed_layers)} "
                f"collect_failures={len(collector.failures)} save_failures={len(failed_layers)}"
            )
            if failed_layers or collector.failures:
                summary = f"{summary}\n\nSome operations failed. Check the log."
            self._show_message("Make all paths relative", summary)
        except Exception as exc:
            carb.log_warn(f"[QuickSearchUX] Could not apply path rewrite: {exc}")
            self._show_message(
                "Make all paths relative",
                f"Could not apply changes.\n\nReason: {exc}",
            )

    def _build_candidates(self, stage, root_location: str) -> list[_PathCandidate]:
        candidates = []
        used_layers = list(stage.GetUsedLayers() or [])
        self._debug(f"used_layers={len(used_layers)}")

        for layer in used_layers:
            if not layer or getattr(layer, "anonymous", False):
                self._debug("skip_layer anonymous_or_none")
                continue

            layer_location = self._layer_location(layer)
            if not self._is_layer_in_stage_scope(layer_location, root_location):
                self._debug(f"skip_layer outside_scope layer={layer_location}")
                continue

            layer_name = normalize_path(getattr(layer, "identifier", "") or "<unknown-layer>")
            before_count = len(candidates)
            self._collect_sublayer_candidates(layer, layer_name, root_location, candidates)
            self._collect_prim_candidates(layer, layer_name, root_location, candidates)
            self._debug(f"layer_scan_done layer={layer_name} added={len(candidates) - before_count}")

        return candidates

    def _is_layer_in_stage_scope(self, layer_location: str, root_location: str) -> bool:
        if not layer_location:
            return False
        if normalize_path(layer_location) == normalize_path(root_location):
            return True

        root_dir_local = self._root_dir_local(root_location)
        layer_local = to_local_filesystem_path(layer_location)
        if root_dir_local and layer_local:
            root_norm = os.path.normcase(os.path.abspath(root_dir_local))
            layer_norm = os.path.normcase(os.path.abspath(layer_local))
            return layer_norm == root_norm or layer_norm.startswith(root_norm + os.sep)

        root_scheme = self._scheme_and_host(root_location)
        layer_scheme = self._scheme_and_host(layer_location)
        if root_scheme and layer_scheme and root_scheme == layer_scheme:
            root_dir = self._root_dir_url(root_location)
            layer_path = posixpath.normpath(normalize_path(urlparse(layer_location).path or ""))
            return bool(root_dir) and (
                layer_path == root_dir
                or layer_path.startswith(root_dir.rstrip("/") + "/")
            )

        return False

    def _collect_sublayer_candidates(self, layer, layer_name: str, root_location: str, out: list[_PathCandidate]):
        sublayers = list(layer.subLayerPaths)
        for index, old_path in enumerate(sublayers):
            old_path = str(old_path or "")
            if not old_path:
                continue

            proposed = self._to_relative_asset_path(old_path, layer)
            can_relative = proposed != old_path
            resolved_source = self._resolve_asset_path_for_layer(old_path, layer)
            collect_recommended, collect_reason = self._collect_recommendation(
                resolved_source,
                root_location,
            )
            if not can_relative and not collect_recommended:
                continue

            def _apply(new_path, _layer=layer, _index=index):
                _layer.subLayerPaths[_index] = new_path

            self._append_candidate(
                out,
                _PathCandidate(
                    layer=layer,
                    layer_name=layer_name,
                    kind="sublayer",
                    owner_path="/",
                    old_path=old_path,
                    proposed_path=proposed,
                    can_make_relative=can_relative,
                    collect_recommended=collect_recommended,
                    collect_reason=collect_reason,
                    collect_source_path=resolved_source,
                    apply_fn=_apply,
                ),
            )

    def _collect_prim_candidates(self, layer, layer_name: str, root_location: str, out: list[_PathCandidate]):
        def _visit(path):
            spec = layer.GetObjectAtPath(path)
            if isinstance(spec, Sdf.PrimSpec):
                owner_path = str(path)
                self._collect_list_candidates(
                    layer,
                    layer_name,
                    root_location,
                    owner_path,
                    spec.referenceList,
                    "reference",
                    out,
                )
                self._collect_list_candidates(
                    layer,
                    layer_name,
                    root_location,
                    owner_path,
                    spec.payloadList,
                    "payload",
                    out,
                )
            elif isinstance(spec, Sdf.AttributeSpec):
                self._collect_attribute_candidates(
                    layer,
                    layer_name,
                    root_location,
                    str(path),
                    spec,
                    out,
                )
            return True

        layer.Traverse("/", _visit)

    def _collect_attribute_candidates(
        self,
        layer,
        layer_name: str,
        root_location: str,
        owner_path: str,
        attr_spec,
        out: list[_PathCandidate],
    ):
        type_name = str(getattr(attr_spec, "typeName", "") or "").lower()
        if "asset" not in type_name:
            return

        # 1) Default value (single asset or asset[])
        default_value = getattr(attr_spec, "default", None)
        self._collect_asset_value_candidates(
            layer,
            layer_name,
            root_location,
            owner_path,
            attr_spec,
            default_value,
            time_code=None,
            out=out,
        )

        # 2) Time-sampled values. Kit stores material inputs (incl. textures)
        #    as time samples, so these must be scanned as well.
        try:
            attr_path = attr_spec.path
            time_samples = list(layer.ListTimeSamplesForPath(attr_path) or [])
        except Exception:
            time_samples = []

        for time_code in time_samples:
            try:
                sample_value = layer.QueryTimeSample(attr_path, time_code)
            except Exception:
                continue
            self._collect_asset_value_candidates(
                layer,
                layer_name,
                root_location,
                owner_path,
                attr_spec,
                sample_value,
                time_code=time_code,
                out=out,
            )

    def _collect_asset_value_candidates(
        self,
        layer,
        layer_name: str,
        root_location: str,
        owner_path: str,
        attr_spec,
        value,
        time_code,
        out: list[_PathCandidate],
    ):
        if value is None:
            return

        attr_path = attr_spec.path

        def _set_default(new_path, _attr=attr_spec):
            _attr.default = Sdf.AssetPath(new_path)

        def _set_default_array(new_path, _attr=attr_spec, _idx=0):
            current = list(getattr(_attr, "default", []) or [])
            if _idx >= len(current):
                return
            current[_idx] = Sdf.AssetPath(new_path)
            _attr.default = current

        def _set_sample_single(new_path, _layer=layer, _path=attr_path, _tc=time_code):
            _layer.SetTimeSample(_path, _tc, Sdf.AssetPath(new_path))

        def _make_set_sample_array(idx):
            def _set(new_path, _layer=layer, _path=attr_path, _tc=time_code, _idx=idx):
                current = list(_layer.QueryTimeSample(_path, _tc) or [])
                if _idx >= len(current):
                    return
                current[_idx] = Sdf.AssetPath(new_path)
                _layer.SetTimeSample(_path, _tc, current)

            return _set

        source_label = "default" if time_code is None else f"time@{time_code:g}"

        if isinstance(value, Sdf.AssetPath):
            old_path = normalize_path(value.path)
            if not old_path:
                return
            apply_fn = _set_default if time_code is None else _set_sample_single
            self._maybe_add_attribute_candidate(
                layer,
                layer_name,
                root_location,
                owner_path,
                f"attribute:{source_label}",
                old_path,
                apply_fn,
                out,
            )
            return

        try:
            entries = list(value)
        except Exception:
            return

        for index, entry in enumerate(entries):
            entry_path = normalize_path(getattr(entry, "path", "") or "")
            if not entry_path:
                continue
            if time_code is None:
                apply_fn = (lambda np, _i=index: _set_default_array(np, _idx=_i))
            else:
                apply_fn = _make_set_sample_array(index)
            self._maybe_add_attribute_candidate(
                layer,
                layer_name,
                root_location,
                f"{owner_path}[{index}]",
                f"attribute:{source_label}Array",
                entry_path,
                apply_fn,
                out,
            )

    def _maybe_add_attribute_candidate(
        self,
        layer,
        layer_name: str,
        root_location: str,
        owner_path: str,
        kind: str,
        old_path: str,
        apply_fn,
        out: list[_PathCandidate],
    ):
        proposed = self._to_relative_asset_path(old_path, layer)
        can_relative = proposed != old_path
        resolved_source = self._resolve_asset_path_for_layer(old_path, layer)
        collect_recommended, collect_reason = self._collect_recommendation(
            resolved_source,
            root_location,
        )
        if not can_relative and not collect_recommended:
            return

        self._append_candidate(
            out,
            _PathCandidate(
                layer=layer,
                layer_name=layer_name,
                kind=kind,
                owner_path=owner_path,
                old_path=old_path,
                proposed_path=proposed,
                can_make_relative=can_relative,
                collect_recommended=collect_recommended,
                collect_reason=collect_reason,
                collect_source_path=resolved_source,
                apply_fn=apply_fn,
            ),
        )

    def _collect_list_candidates(
        self,
        layer,
        layer_name: str,
        root_location: str,
        owner_path: str,
        list_op,
        kind_prefix: str,
        out: list[_PathCandidate],
    ):
        list_props = (
            "explicitItems",
            "prependedItems",
            "appendedItems",
            "addedItems",
            "orderedItems",
            "deletedItems",
        )

        for prop_name in list_props:
            try:
                items = list(getattr(list_op, prop_name))
            except Exception:
                continue

            for index, item in enumerate(items):
                old_path = str(getattr(item, "assetPath", "") or "")
                if not old_path:
                    continue

                proposed = self._to_relative_asset_path(old_path, layer)
                can_relative = proposed != old_path
                resolved_source = self._resolve_asset_path_for_layer(old_path, layer)
                collect_recommended, collect_reason = self._collect_recommendation(
                    resolved_source,
                    root_location,
                )
                if not can_relative and not collect_recommended:
                    continue

                if kind_prefix == "reference":
                    def _apply(new_path, _list_op=list_op, _prop=prop_name, _idx=index):
                        current = list(getattr(_list_op, _prop))
                        current_item = current[_idx]
                        current[_idx] = Sdf.Reference(
                            new_path,
                            current_item.primPath,
                            current_item.layerOffset,
                            current_item.customData,
                        )
                        setattr(_list_op, _prop, current)
                else:
                    def _apply(new_path, _list_op=list_op, _prop=prop_name, _idx=index):
                        current = list(getattr(_list_op, _prop))
                        current_item = current[_idx]
                        current[_idx] = Sdf.Payload(
                            new_path,
                            current_item.primPath,
                            current_item.layerOffset,
                        )
                        setattr(_list_op, _prop, current)

                self._append_candidate(
                    out,
                    _PathCandidate(
                        layer=layer,
                        layer_name=layer_name,
                        kind=f"{kind_prefix}:{prop_name}",
                        owner_path=owner_path,
                        old_path=old_path,
                        proposed_path=proposed,
                        can_make_relative=can_relative,
                        collect_recommended=collect_recommended,
                        collect_reason=collect_reason,
                        collect_source_path=resolved_source,
                        apply_fn=_apply,
                    ),
                )

    def _collect_recommendation(self, resolved_asset_path: str | None, root_location: str) -> tuple[bool, str]:
        if not resolved_asset_path:
            return False, ""
        if self._is_online_asset_path(resolved_asset_path):
            return True, "online"
        if not self._is_within_root_scope(resolved_asset_path, root_location):
            return True, "outside-root"
        return False, ""

    def _is_within_root_scope(self, asset_path: str, root_location: str) -> bool:
        root_dir_local = self._root_dir_local(root_location)
        asset_local = to_local_filesystem_path(asset_path)
        if root_dir_local and asset_local:
            root_norm = os.path.normcase(os.path.abspath(root_dir_local))
            asset_norm = os.path.normcase(os.path.abspath(asset_local))
            return asset_norm == root_norm or asset_norm.startswith(root_norm + os.sep)

        root_scheme = self._scheme_and_host(root_location)
        asset_scheme = self._scheme_and_host(asset_path)
        if root_scheme and asset_scheme and root_scheme == asset_scheme:
            root_dir = self._root_dir_url(root_location)
            asset_path_posix = posixpath.normpath(normalize_path(urlparse(asset_path).path or ""))
            return bool(root_dir) and (
                asset_path_posix == root_dir
                or asset_path_posix.startswith(root_dir.rstrip("/") + "/")
            )

        root_local = to_local_filesystem_path(root_location)
        if root_local and asset_scheme:
            return False

        return False

    @staticmethod
    def _scheme_and_host(value: str) -> tuple[str, str] | None:
        parsed = urlparse(normalize_path(value))
        if not parsed.scheme or not parsed.netloc:
            return None
        return parsed.scheme.lower(), parsed.netloc.lower()

    def _to_relative_asset_path(self, asset_path: str, layer) -> str:
        value = normalize_path(asset_path)
        if not value:
            return asset_path

        if not self._is_absolute_asset_path(value):
            if ".." in value.split("/"):
                resolved = self._resolve_asset_path_for_layer(value, layer)
                if not resolved:
                    return asset_path
                value = resolved
            else:
                return asset_path

        if not self._is_absolute_asset_path(value):
            return asset_path

        layer_location = self._layer_location(layer)
        if not layer_location:
            return asset_path

        relative_url = self._relative_url_path(value, layer_location)
        if relative_url is not None:
            return relative_url

        layer_local = to_local_filesystem_path(layer_location)
        asset_local = to_local_filesystem_path(value)
        if not layer_local or not asset_local:
            return asset_path

        layer_dir = os.path.dirname(layer_local)
        if not layer_dir:
            return asset_path

        try:
            relative_path = os.path.relpath(asset_local, start=layer_dir)
        except ValueError:
            return asset_path

        return normalize_path(relative_path)

    @staticmethod
    def _layer_location(layer) -> str:
        layer_identifier = normalize_path(getattr(layer, "identifier", "") or "")
        layer_real_path = normalize_path(getattr(layer, "realPath", "") or "")
        return layer_real_path or layer_identifier

    @staticmethod
    def _is_absolute_asset_path(value: str) -> bool:
        if _URL_SCHEME_RE.match(value):
            return True
        if value.lower().startswith("file:"):
            return True
        if _WIN_ABS_RE.match(value):
            return True
        if value.startswith("//") or value.startswith("\\\\"):
            return True
        if value.startswith("/"):
            return True
        return False

    @staticmethod
    def _is_online_asset_path(value: str) -> bool:
        normalized = normalize_path(value)
        if not normalized:
            return False

        if to_local_filesystem_path(normalized):
            return False

        parsed = urlparse(normalized)
        if not parsed.scheme:
            return False
        return parsed.scheme.lower() not in ("file",)

    @staticmethod
    def _relative_url_path(asset_path: str, layer_path: str) -> str | None:
        asset_parsed = urlparse(asset_path)
        layer_parsed = urlparse(layer_path)

        if not asset_parsed.scheme or not layer_parsed.scheme:
            return None
        if asset_parsed.scheme.lower() != layer_parsed.scheme.lower():
            return None
        if asset_parsed.netloc.lower() != layer_parsed.netloc.lower():
            return None

        asset_abs = normalize_path(asset_parsed.path or "")
        layer_abs = normalize_path(layer_parsed.path or "")
        if not asset_abs or not layer_abs:
            return None

        layer_dir = posixpath.dirname(layer_abs) or "/"
        relative_path = posixpath.relpath(asset_abs, start=layer_dir)
        return normalize_path(relative_path)

    @staticmethod
    def _root_dir_local(root_location: str) -> str | None:
        local = to_local_filesystem_path(root_location)
        if not local:
            return None
        return os.path.dirname(local)

    @staticmethod
    def _root_dir_url(root_location: str) -> str:
        parsed = urlparse(normalize_path(root_location))
        if not parsed.scheme or not parsed.netloc:
            return ""
        path = normalize_path(parsed.path or "")
        return posixpath.normpath(posixpath.dirname(path)) if path else ""

    def _resolve_asset_path_for_layer(self, asset_path: str, layer) -> str | None:
        value = normalize_path(asset_path)
        if not value:
            return None
        if self._is_absolute_asset_path(value):
            return value

        layer_location = self._layer_location(layer)
        if not layer_location:
            return None

        layer_parsed = urlparse(layer_location)
        if layer_parsed.scheme and layer_parsed.netloc:
            layer_dir = posixpath.dirname(normalize_path(layer_parsed.path or "")) or "/"
            resolved_path = posixpath.normpath(posixpath.join(layer_dir, value))
            if not resolved_path.startswith("/"):
                resolved_path = f"/{resolved_path}"
            return f"{layer_parsed.scheme}://{layer_parsed.netloc}{normalize_path(resolved_path)}"

        layer_local = to_local_filesystem_path(layer_location)
        if not layer_local:
            return None
        layer_dir_local = os.path.dirname(layer_local)
        return normalize_path(os.path.abspath(os.path.join(layer_dir_local, value)))

    def _append_candidate(self, out: list[_PathCandidate], candidate: _PathCandidate):
        out.append(candidate)
        self._debug(
            f"candidate kind={candidate.kind} collect={candidate.collect_recommended} "
            f"reason={candidate.collect_reason or '-'} layer={candidate.layer_name} "
            f"owner={candidate.owner_path} old={candidate.old_path} "
            f"proposed={candidate.proposed_path}"
        )

    @staticmethod
    def _clip(text: str, max_len: int = 160) -> str:
        value = str(text or "")
        if len(value) <= max_len:
            return value
        return f"{value[: max_len - 3]}..."

    @staticmethod
    def _debug(message: str):
        if _DEBUG:
            carb.log_info(f"[QuickSearchUX][MakePathsRelative][DEBUG] {message}")
            if _DEBUG_TO_STDOUT:
                try:
                    print(f"[QuickSearchUX][MakePathsRelative][DEBUG] {message}", flush=True)
                except Exception:
                    pass

    def _show_message(self, title: str, text: str):
        self._close_preview()
        if self._message_window:
            try:
                self._message_window.visible = False
            except Exception:
                pass

        window = ui.Window(title, width=640, height=200, flags=ui.WINDOW_FLAGS_MODAL)
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


class _Collector:
    """Copies external assets into a folder next to the root layer."""

    def __init__(self, root_location: str):
        self._root_location = normalize_path(root_location)
        self._target_dir = self._build_target_dir(self._root_location)
        self._cache = {}
        self._failed = set()
        self.failures = []

    async def collect_async(self, source_path: str, is_usd: bool) -> str | None:
        """Collect a source asset.

        For USD references/payloads/sublayers this uses the official
        ``omni.kit.usd.collect`` Collector so that all sub-dependencies
        (textures, MDL, nested USD) are collected and re-mapped.

        For single non-USD assets (mdl/png/...) a plain download is used.
        """
        key = normalize_path(source_path)
        cached = self._cache.get(key)
        if cached:
            MakePathsRelativeHandler._debug(f"collector_cache_hit source={key} target={cached}")
            return cached
        if key in self._failed:
            MakePathsRelativeHandler._debug(f"collector_failed_cache_hit source={key}")
            return None

        if is_usd:
            result = await self._collect_usd_with_dependencies(key)
            if result:
                self._cache[key] = result
                return result
            self._failed.add(key)
            return None

        return self.collect(key)

    async def _collect_usd_with_dependencies(self, source_usd: str) -> str | None:
        try:
            from omni.kit.usd.collect import Collector
        except Exception as exc:
            self.failures.append(f"collect tool unavailable: {exc}")
            MakePathsRelativeHandler._debug(f"collector_tool_unavailable reason={exc}")
            # Fallback: at least copy the single USD file (textures may break).
            return self.collect(source_usd)

        # Give each collected asset its own subfolder to preserve its internal
        # relative folder structure (materials/, etc.) without collisions.
        leaf = posixpath.basename(urlparse(source_usd).path) or "asset.usd"
        stem = os.path.splitext(leaf)[0]
        digest = hashlib.sha1(source_usd.encode("utf-8")).hexdigest()[:10]
        collect_dir = self._join_path(self._target_dir, f"{digest}_{stem}")

        source_for_tool = omni.client.make_file_url_if_possible(source_usd)
        MakePathsRelativeHandler._debug(
            f"collector_tool_start source={source_for_tool} dir={collect_dir}"
        )
        try:
            collector = Collector(source_for_tool, collect_dir)
            success, collected_root = await collector.collect()
        except Exception as exc:
            self.failures.append(f"collect tool failed for {source_usd}: {exc}")
            MakePathsRelativeHandler._debug(f"collector_tool_exception reason={exc}")
            return None

        if not success or not collected_root:
            self.failures.append(f"collect tool did not produce root for {source_usd}")
            MakePathsRelativeHandler._debug(
                f"collector_tool_no_root success={success} root={collected_root}"
            )
            return None

        collected_root = normalize_path(
            to_local_filesystem_path(collected_root) or collected_root
        )
        MakePathsRelativeHandler._debug(f"collector_tool_done root={collected_root}")
        return collected_root

    def collect(self, source_path: str) -> str | None:
        key = normalize_path(source_path)
        cached = self._cache.get(key)
        if cached:
            MakePathsRelativeHandler._debug(f"collector_cache_hit source={key} target={cached}")
            return cached
        if key in self._failed:
            MakePathsRelativeHandler._debug(f"collector_failed_cache_hit source={key}")
            return None

        try:
            file_name = self._target_name_for_source(key)
            destination = self._join_path(self._target_dir, file_name)
            MakePathsRelativeHandler._debug(f"collector_read source={key}")
            payload = self._read_payload(key)
            if payload is None:
                self.failures.append(f"read failed: {key}")
                self._failed.add(key)
                MakePathsRelativeHandler._debug(f"collector_read_failed source={key}")
                return None

            MakePathsRelativeHandler._debug(f"collector_write target={destination} bytes={len(payload)}")
            if not self._write_payload(destination, payload):
                self.failures.append(f"write failed: {destination}")
                self._failed.add(key)
                MakePathsRelativeHandler._debug(f"collector_write_failed target={destination}")
                return None

            self._cache[key] = destination
            MakePathsRelativeHandler._debug(f"collector_done source={key} target={destination}")
            return destination
        except Exception as exc:
            self.failures.append(f"{key}: {exc}")
            self._failed.add(key)
            MakePathsRelativeHandler._debug(f"collector_exception source={key} reason={exc}")
            return None

    @staticmethod
    def _build_target_dir(root_location: str) -> str:
        local_root = to_local_filesystem_path(root_location)
        if local_root:
            root_dir = os.path.dirname(local_root)
            return normalize_path(os.path.join(root_dir, "_collected_external_assets"))

        parsed = urlparse(root_location)
        base_dir = posixpath.dirname(normalize_path(parsed.path or ""))
        collected_dir = posixpath.join(base_dir or "/", "_collected_external_assets")
        return f"{parsed.scheme}://{parsed.netloc}{normalize_path(collected_dir)}"

    @staticmethod
    def preview_target_dir(root_location: str) -> str:
        return _Collector._build_target_dir(normalize_path(root_location))

    @staticmethod
    def _target_name_for_source(source_path: str) -> str:
        parsed = urlparse(source_path)
        leaf = posixpath.basename(parsed.path) if parsed.path else "asset.bin"
        if not leaf:
            leaf = "asset.bin"
        digest = hashlib.sha1(source_path.encode("utf-8")).hexdigest()[:10]
        return f"{digest}_{leaf}"

    @staticmethod
    def _join_path(base_path: str, leaf: str) -> str:
        parsed = urlparse(base_path)
        if parsed.scheme and parsed.netloc:
            joined = posixpath.join(normalize_path(parsed.path or "/"), leaf)
            return f"{parsed.scheme}://{parsed.netloc}{normalize_path(joined)}"
        return normalize_path(os.path.join(base_path, leaf))

    @staticmethod
    def _read_payload(source_path: str) -> bytes | None:
        parsed = urlparse(source_path)
        if parsed.scheme.lower() in ("http", "https"):
            try:
                request = Request(source_path, headers={"User-Agent": "QuickSearchUX/1.0"})
                with urlopen(request, timeout=20) as response:
                    return response.read()
            except Exception:
                return None

        local = to_local_filesystem_path(source_path)
        if local and os.path.isfile(local):
            try:
                with open(local, "rb") as stream:
                    return stream.read()
            except Exception:
                return None

        try:
            result = omni.client.read_file(source_path)
        except Exception:
            return None

        if isinstance(result, tuple):
            if not result:
                return None
            status = result[0]
            payload = result[1] if len(result) > 1 else None
            if not omni_result_ok(status):
                return None
            if isinstance(payload, bytes):
                return payload
            if hasattr(payload, "tobytes"):
                return payload.tobytes()
            try:
                return bytes(payload)
            except Exception:
                return None

        return None

    @staticmethod
    def _write_payload(destination_path: str, payload: bytes) -> bool:
        local = to_local_filesystem_path(destination_path)
        if local:
            try:
                parent = os.path.dirname(local)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(local, "wb") as stream:
                    stream.write(payload)
                return True
            except Exception:
                return False

        try:
            parent = destination_path.rsplit("/", 1)[0]
            folder_result = omni.client.create_folder(parent)
            if not omni_result_ok(folder_result):
                return False
            write_result = omni.client.write_file(destination_path, payload)
            return omni_result_ok(write_result)
        except Exception:
            return False
