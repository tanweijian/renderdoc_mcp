# RenderDoc MCP extension

This is a dependency-free Python extension that exposes the currently open RenderDoc capture as a local MCP server over Streamable HTTP. It uses the Python standard library included in RenderDoc and does not require a separate Python installation or package manager.

## Install

Copy this directory to the RenderDoc extension directory:

- Windows: `%APPDATA%\\qrenderdoc\\extensions\\renderdoc_mcp`
- Linux: `~/.local/share/qrenderdoc/extensions/renderdoc_mcp`

Then enable **Tools → Manage Extensions → RenderDoc MCP**.

## Start

Use **Tools → RenderDoc MCP → Start**. The extension prints the endpoint in RenderDoc's Python output:

```text
http://127.0.0.1:8765/mcp
```

Configure an MCP client to use that URL as a Streamable HTTP server. No external Python installation, package manager, or stdio process is required.

To start MCP automatically when RenderDoc opens, check **Enabled** for RenderDoc MCP in **Tools → Manage Extensions**, then select **Tools → RenderDoc MCP → Auto Start → On**. The setting defaults to off and applies the next time the extension loads.

The extension stores `auto_start` as a boolean in `renderdoc_mcp.json` in RenderDoc's user configuration directory (`%APPDATA%\qrenderdoc` on Windows).

Select **Auto Start → Off** to disable future automatic starts without stopping the current service. Use **Stop** to stop the service for the current session without changing Auto Start.

When RenderDoc has PySide2 support, the entry that matches the saved setting is grayed out: only **On** is selectable when Auto Start is off, and only **Off** is selectable when it is on.

The RenderDoc status bar shows a green dot while MCP is running and a red dot while it is stopped or failed to start. The tooltip on the dot shows the active endpoint or failure state. Unloading the extension or exiting RenderDoc stops the HTTP server and closes its listening socket.

The HTTP listener uses Python's `http.server` and `socketserver` modules. Capture analysis runs in worker threads and uses RenderDoc's UI-thread dispatch API for UI-owned data.

The server is bound to loopback and accepts requests at `/mcp`. It implements the MCP `initialize`, `ping`, `tools/list`, and `tools/call` methods.

The default port is `8765`. Set the `RENDERDOC_MCP_PORT` environment variable before launching RenderDoc to choose another port. If the default port is occupied, the extension automatically selects an available local port and prints the resulting endpoint.

## Tools

- `get_capture_info`
- `list_actions`
- `list_resources`
- `get_resource_usage`
- `get_pipeline_state`
- `get_texture_data`
- `get_buffer_data`
- `get_pixel_history`

Capture-dependent calls return an error when no capture is open. Resource arguments accept either the `ResourceId::...` string returned by `list_resources` or the RenderDoc resource name.

`get_pixel_history` accepts an optional `event_id` when the history must be evaluated at a specific replay event. Supplying it moves RenderDoc's current replay event so the UI and the returned analysis refer to the same state.

The extension uses RenderDoc's blocking replay controller from its request worker threads. A tool call can therefore take longer when replay analysis is expensive.
