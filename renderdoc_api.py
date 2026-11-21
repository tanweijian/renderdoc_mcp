"""RenderDoc API adapters used by the MCP tool handlers."""

from __future__ import print_function

import base64
import threading

import renderdoc as rd


STAGES = (
    ("vertex", rd.ShaderStage.Vertex),
    ("hull", rd.ShaderStage.Hull),
    ("domain", rd.ShaderStage.Domain),
    ("geometry", rd.ShaderStage.Geometry),
    ("pixel", rd.ShaderStage.Pixel),
    ("compute", rd.ShaderStage.Compute),
    ("task", rd.ShaderStage.Task),
    ("mesh", rd.ShaderStage.Mesh),
    ("amplification", rd.ShaderStage.Amplification),
)


def _text(value):
    return str(value)


def _resource(value):
    return _text(value)


def _numbers(value):
    try:
        return [int(item) for item in value]
    except TypeError:
        return []


def _float_values(value):
    try:
        return [float(item) for item in value]
    except TypeError:
        return []


def _modification_value(value):
    color = value
    for unused in range(3):
        if hasattr(color, "floatValue"):
            color = _float_values(color.floatValue)
            break
        if hasattr(color, "col"):
            color = color.col
        else:
            color = []
            break
    if not isinstance(color, list):
        color = []
    return {
        "color": color,
        "depth": float(value.depth),
        "stencil": int(value.stencil),
    }


def _descriptor(descriptor):
    return {
        "resource": _resource(descriptor.resource),
        "view": _resource(descriptor.view),
        "type": _text(descriptor.type),
        "format": _text(descriptor.format),
        "byte_offset": int(descriptor.byteOffset),
        "byte_size": int(descriptor.byteSize),
        "first_mip": int(descriptor.firstMip),
        "num_mips": int(descriptor.numMips),
        "first_slice": int(descriptor.firstSlice),
        "num_slices": int(descriptor.numSlices),
    }


def _subresource(args):
    sub = rd.Subresource()
    sub.mip = int(args.get("mip", 0))
    sub.slice = int(args.get("slice", 0))
    sub.plane = int(args.get("plane", 0))
    return sub


class RenderDocTools(object):
    def __init__(self, context):
        self.context = context

    def call(self, name, args):
        methods = {
            "get_capture_info": self.get_capture_info,
            "list_actions": self.list_actions,
            "list_resources": self.list_resources,
            "get_resource_usage": self.get_resource_usage,
            "get_pipeline_state": self.get_pipeline_state,
            "get_texture_data": self.get_texture_data,
            "get_buffer_data": self.get_buffer_data,
            "get_pixel_history": self.get_pixel_history,
        }
        if name not in methods:
            raise ValueError("unknown tool: {}".format(name))
        return methods[name](args)

    def _ui_call(self, callback):
        """Run a short CaptureContext read on RenderDoc's UI thread.

        CaptureContext's cached action and resource lists are owned by the UI
        context. HTTP requests run on worker threads, so returning those
        objects directly would leave a close/reload race. The callback copies
        the values needed by a tool before the worker continues.
        """
        result = []
        error = []
        completed = threading.Event()

        def invoke():
            try:
                result.append(callback())
            except BaseException as exception:
                error.append(exception)
            finally:
                completed.set()

        self.context.InvokeOntoUIThread(invoke)
        if not completed.wait(30.0):
            raise RuntimeError("RenderDoc UI thread did not respond")
        if error:
            raise error[0]
        return result[0] if result else None

    def _require_capture(self):
        if not self._ui_call(lambda: bool(self.context.IsCaptureLoaded())):
            raise RuntimeError("No capture is open in RenderDoc")

    def _controller(self):
        controller = self._ui_call(self.context.GetBlockingController)
        if controller is None:
            raise RuntimeError("The replay controller is not available")
        return controller

    def _resource_maps(self):
        def collect():
            textures = []
            for item in self.context.GetTextures():
                textures.append({
                    "kind": "texture",
                    "resource_id": item.resourceId,
                    "name": self.context.GetResourceName(item.resourceId),
                    "width": int(item.width),
                    "height": int(item.height),
                    "depth": int(item.depth),
                    "mips": int(item.mips),
                    "array_size": int(item.arraysize),
                    "format": _text(item.format),
                    "byte_size": int(item.byteSize),
                })

            buffers = []
            for item in self.context.GetBuffers():
                buffers.append({
                    "kind": "buffer",
                    "resource_id": item.resourceId,
                    "name": self.context.GetResourceName(item.resourceId),
                    "length": int(item.length),
                    "gpu_address": int(item.gpuAddress),
                })

            mapping = {}
            for item in textures + buffers:
                mapping[_resource(item["resource_id"])] = item
            return textures, buffers, mapping

        textures, buffers, mapping = self._ui_call(collect)
        return textures, buffers, mapping

    def _resolve_resource(self, value):
        textures, buffers, mapping = self._resource_maps()
        query = _text(value)
        if query in mapping:
            item = mapping[query]
            return item["resource_id"], item["name"], item["kind"]
        for item in textures + buffers:
            if item["name"] == query:
                return item["resource_id"], item["name"], item["kind"]
        raise ValueError("Resource not found: {}".format(query))

    def get_capture_info(self, args):
        self._require_capture()
        controller = self._controller()
        api = controller.GetAPIProperties()
        frame = controller.GetFrameInfo()
        capture_file, current_event, selected_event = self._ui_call(
            lambda: (self.context.GetCaptureFilename(), self.context.CurEvent(),
                     self.context.CurSelectedEvent())
        )
        return {
            "capture_file": capture_file,
            "api": _text(api.pipelineType),
            "current_event": int(current_event),
            "selected_event": int(selected_event),
            "frame": {
                "frame_number": int(frame.frameNumber),
                "compressed_file_size": int(frame.compressedFileSize),
                "uncompressed_file_size": int(frame.uncompressedFileSize),
                "capture_time": int(frame.captureTime),
                "stats": {
                    "recorded": bool(frame.stats.recorded),
                    "draw_calls": int(frame.stats.draws.calls),
                    "dispatch_calls": int(frame.stats.dispatches.calls),
                },
            },
            "features": {
                "pixel_history": bool(api.pixelHistory),
                "shader_debugging": bool(api.shaderDebugging),
            },
        }

    def _action(self, action, structured_file):
        name = _text(action.customName)
        if not name:
            try:
                name = _text(action.GetName(structured_file))
            except Exception:
                name = ""
        return {
            "event_id": int(action.eventId),
            "action_id": int(action.actionId),
            "name": name,
            "flags": _text(action.flags),
            "num_indices": int(action.numIndices),
            "num_instances": int(action.numInstances),
            "dispatch_dimension": _numbers(action.dispatchDimension),
            "copy_source": _resource(action.copySource),
            "copy_destination": _resource(action.copyDestination),
            "outputs": [_resource(resource) for resource in action.outputs
                         if _resource(resource) != _resource(rd.ResourceId())],
            "depth_output": _resource(action.depthOut),
            "children": [self._action(child, structured_file) for child in action.children],
        }

    def list_actions(self, args):
        self._require_capture()
        limit = min(max(int(args.get("limit", 2000)), 1), 10000)
        draws_only = bool(args.get("draws_only", False))
        def collect():
            structured_file = self.context.GetStructuredFile()
            result = []

            def visit(actions):
                for action in actions:
                    if len(result) >= limit:
                        return
                    is_draw = bool(action.flags & rd.ActionFlags.Drawcall)
                    if not draws_only or is_draw:
                        result.append(self._action(action, structured_file))
                    visit(action.children)

            visit(self.context.CurRootActions())
            return result

        result = self._ui_call(collect)
        return {"actions": result, "truncated": len(result) >= limit}

    def _texture(self, texture):
        return {
            "kind": texture["kind"],
            "resource": _resource(texture["resource_id"]),
            "name": texture["name"],
            "width": texture["width"],
            "height": texture["height"],
            "depth": texture["depth"],
            "mips": texture["mips"],
            "array_size": texture["array_size"],
            "format": texture["format"],
            "byte_size": texture["byte_size"],
        }

    def _buffer(self, buffer):
        return {
            "kind": buffer["kind"],
            "resource": _resource(buffer["resource_id"]),
            "name": buffer["name"],
            "length": buffer["length"],
            "gpu_address": buffer["gpu_address"],
        }

    def list_resources(self, args):
        self._require_capture()
        kind = args.get("kind", "all")
        limit = min(max(int(args.get("limit", 2000)), 1), 10000)
        textures, buffers, unused = self._resource_maps()
        resources = []
        if kind in ("all", "textures"):
            resources.extend([self._texture(texture) for texture in textures])
        if kind in ("all", "buffers"):
            resources.extend([self._buffer(buffer) for buffer in buffers])
        return {"resources": resources[:limit], "truncated": len(resources) > limit}

    def get_resource_usage(self, args):
        self._require_capture()
        resource, name, unused_kind = self._resolve_resource(args.get("resource"))
        usage = self._controller().GetUsage(resource)
        return {"resource": _resource(resource), "name": name,
                "uses": [{"event_id": int(item.eventId), "usage": _text(item.usage)} for item in usage]}

    def _stage_resources(self, pipe, stage):
        return {
            "read_only": [_descriptor(item.descriptor) for item in pipe.GetReadOnlyResources(stage, True)],
            "read_write": [_descriptor(item.descriptor) for item in pipe.GetReadWriteResources(stage, True)],
        }

    def get_pipeline_state(self, args):
        self._require_capture()
        current_event = self._ui_call(self.context.CurEvent)
        pipe = self._controller().GetPipelineState()
        result = {
            "event_id": int(current_event),
            "outputs": [_descriptor(item) for item in pipe.GetOutputTargets()],
            "depth_target": _descriptor(pipe.GetDepthTarget()),
            "stages": {},
        }
        for name, stage in STAGES:
            try:
                shader = pipe.GetShader(stage)
                reflection = pipe.GetShaderReflection(stage)
                result["stages"][name] = {
                    "shader": _resource(shader),
                    "entry_point": _text(reflection.entryPoint) if reflection else "",
                    "debuggable": bool(reflection.debugInfo.debuggable) if reflection else False,
                    "resources": self._stage_resources(pipe, stage),
                }
            except Exception:
                # Backends can omit stages that do not exist in their pipeline.
                continue
        return result

    def get_texture_data(self, args):
        self._require_capture()
        resource, name, kind = self._resolve_resource(args.get("resource"))
        if kind != "texture":
            raise ValueError("Resource is not a texture: {}".format(resource))
        data = bytes(self._controller().GetTextureData(resource, _subresource(args)))
        max_bytes = min(max(int(args.get("max_bytes", 4 * 1024 * 1024)), 1), 16 * 1024 * 1024)
        return {"resource": _resource(resource), "name": name, "byte_length": len(data),
                "truncated": len(data) > max_bytes,
                "data_base64": base64.b64encode(data[:max_bytes]).decode("ascii")}

    def get_buffer_data(self, args):
        self._require_capture()
        resource, name, kind = self._resolve_resource(args.get("resource"))
        if kind != "buffer":
            raise ValueError("Resource is not a buffer: {}".format(resource))
        offset = max(int(args.get("offset", 0)), 0)
        length = min(max(int(args.get("length", 4096)), 1), 16 * 1024 * 1024)
        data = bytes(self._controller().GetBufferData(resource, offset, length))
        return {"resource": _resource(resource), "name": name, "offset": offset,
                "byte_length": len(data),
                "data_base64": base64.b64encode(data).decode("ascii")}

    def get_pixel_history(self, args):
        self._require_capture()
        resource, name, kind = self._resolve_resource(args.get("resource"))
        if kind != "texture":
            raise ValueError("Resource is not a texture: {}".format(resource))
        controller = self._controller()
        if "event_id" in args:
            controller.SetFrameEvent(max(int(args["event_id"]), 1), True)
        history = controller.PixelHistory(
            resource,
            max(int(args.get("x", 0)), 0),
            max(int(args.get("y", 0)), 0),
            _subresource(args),
            rd.CompType.Typeless,
        )
        return {
            "resource": _resource(resource),
            "history": [
                {
                    "event_id": int(item.eventId),
                    "passed": bool(item.Passed()),
                    "primitive_id": int(item.primitiveID),
                    "pre_modification": _modification_value(item.preMod),
                    "shader_output": _modification_value(item.shaderOut),
                    "post_modification": _modification_value(item.postMod),
                    "unbound_pixel_shader": bool(item.unboundPS),
                    "depth_test_failed": bool(item.depthTestFailed),
                    "stencil_test_failed": bool(item.stencilTestFailed),
                }
                for item in history
            ],
        }
