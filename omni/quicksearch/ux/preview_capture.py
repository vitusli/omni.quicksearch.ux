"""Viewport preview capture on stage save.

Captures the active viewport to ``preview.png`` next to a saved ``main.usd`` /
``main.usda`` stage. Supports local filesystem targets and remote omni.client
URLs (written via a temporary local file).
"""

import asyncio
import os
import tempfile
from urllib.parse import unquote, urlparse

import carb
import omni.client
import omni.kit.app
import omni.usd

from .paths import (
    PREVIEW_IMAGE_FILENAME,
    PREVIEW_STAGE_FILENAMES,
    normalize_path,
    omni_result_ok,
    to_local_filesystem_path,
)


class PreviewCaptureHandler:
    """Subscribes to stage-save events and updates the project preview image."""

    def __init__(self):
        self._stage_event_sub = None
        self._capture_task = None

    def start(self):
        try:
            event_stream = omni.usd.get_context().get_stage_event_stream()
            self._stage_event_sub = event_stream.create_subscription_to_pop(
                self._on_stage_event,
                name="QuickSearchUX stage save preview",
            )
        except Exception as exc:
            self._stage_event_sub = None
            carb.log_warn(f"[QuickSearchUX] Could not subscribe to stage events: {exc}")

    def stop(self):
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()
        self._capture_task = None
        self._stage_event_sub = None

    def _on_stage_event(self, event):
        if not self._is_save_stage_event(event):
            return

        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()

        self._capture_task = asyncio.ensure_future(self._capture_preview_for_saved_main_stage())

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
            temp_preview_path = os.path.join(
                tempfile.gettempdir(), f"quicksearchux_preview_{os.getpid()}.png"
            )
            capture_ok = await self._capture_active_viewport_to_file(temp_preview_path)
            write_ok = capture_ok and self._write_binary_file_to_omni_url(
                temp_preview_path, preview_target
            )
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
        stage_refs = [normalize_path(context.get_stage_url())]

        stage = context.get_stage()
        if stage:
            root_layer = stage.GetRootLayer()
            if root_layer:
                stage_refs.append(normalize_path(getattr(root_layer, "realPath", "")))
                stage_refs.append(normalize_path(getattr(root_layer, "identifier", "")))

        for stage_ref in stage_refs:
            target = PreviewCaptureHandler._preview_target_from_stage_reference(stage_ref)
            if target is not None:
                return target

        return None, False

    @staticmethod
    def _preview_target_from_stage_reference(stage_ref: str) -> tuple[str, bool] | None:
        value = normalize_path(stage_ref)
        if not value or value.startswith("anon:"):
            return None

        local_path = to_local_filesystem_path(value)
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
        preview_remote_path = (
            f"{stage_dir}/{PREVIEW_IMAGE_FILENAME}" if stage_dir else f"/{PREVIEW_IMAGE_FILENAME}"
        )
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
            if omni_result_ok(result):
                return True
            carb.log_warn(
                f"[QuickSearchUX] Could not write preview image to {destination_url}: {result}"
            )
            return False
        except Exception as exc:
            carb.log_warn(
                f"[QuickSearchUX] Could not write preview image to {destination_url}: {exc}"
            )
            return False

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
                f"[QuickSearchUX] Active viewport camera is not perspective: {camera_path}. "
                "Capturing anyway."
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
