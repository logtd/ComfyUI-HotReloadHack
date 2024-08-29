# ComfyUI-HotReloadHack
Hot reloading for custom node developers

This is a very hacky way to get hot reloading of custom nodes. It probably has bugs or doesn't work for complex workflows.

## How to Use

Having this node pack installed will automatically start a watchdog that looks for changes in custom node repos.
Changing a file in those repos will reload the new nodes and invalidate the cache for all nodes in that repo.

### Important

In its current state, it doesn't invalidate the cache for the output, so Comfy doesn't know it needs to rerun.

To force your nodes with updated code to run, add `IS_CHANGED = True` to the class you're working in.

For example:
```
class ExampleNode:

  IS_CHANGED = True  # <---- Add this

  @classmethod
  def INPUT_TYPES(s):
      return {"required": { 
          "model": ("MODEL",),
      }}
  RETURN_TYPES = ("MODEL",)
  FUNCTION = "apply"

  CATEGORY = "example"
  def apply(self, model):
      print('this will run with new code')
      return (model,)
```
