# ComfyUI-HotReloadHack
Hot reloading for custom node developers

This is a very hacky way to get hot reloading of custom nodes. It probably has bugs or doesn't work for complex workflows.

## How to Use

Having this node pack installed will automatically start a watchdog that looks for changes in custom node repos.

When a file is changed the Comfy execution graph cache will be crawled and nodes within that changed repo or downstream of those nodes will be invalidated. The next run will use updated code for those nodes.


