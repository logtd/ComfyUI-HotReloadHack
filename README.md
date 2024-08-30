# ComfyUI-HotReloadHack
Hot reloading for custom node developers

This is a very hacky way to get hot reloading of custom nodes. It probably has bugs or doesn't work for complex workflows.

## Installation

`python -m pip install watchdog`

## How to Use

HotReloadHack automatically watches files in your `custom_nodes/` directory and when one changes reloads the node repo it belongs to. 
It also clears the Comfy execution cache for all nodes in that repo so Comfy knows which nodes it needs to rerun.

As a bonus it will load in new node repos, so you don't have to restart Comfy after downloading node packs.


## Examples

![example_without_hrh](https://github.com/user-attachments/assets/7f29fd52-410d-48fe-8f1a-64b6d5e122f3)

![example_with_hrh](https://github.com/user-attachments/assets/a13f6e4f-a081-43bd-89b8-e6a98483f52f)
