# Hardcoded recipes

For tasks you run often (open Safari, go to Google, press Calculator buttons), **recipes** are more reliable than asking an LLM to guess UI elements.

## Run a recipe

```bash
brain recipe list
brain recipe run safari-google
```

No model, no API quota, faster. Still uses the Cua Driver, approval gate, and logging.

## Add your own

Create `recipes/my-task.yaml`:

```yaml
name: my-task
description: What this does
steps:
  - tool: launch_app
    args:
      bundle_id: com.apple.Safari

  - wait_seconds: 1

  - tool: hotkey
    use_launch_context: true   # fills pid + window_id from launch_app
    args:
      keys: ["cmd", "l"]

  - tool: type_text_in
    use_launch_context: true
    args:
      text: "https://example.com"
      element_index: 0        # tune after one test run

  - tool: hotkey
    use_launch_context: true
    args:
      keys: ["return"]
```

## Calibrating `element_index`

1. Run once with `auto_snapshot_after_launch: true` in config (smart mode default).
2. Open the run log or PNG under `logs/`.
3. Find the `[N]` label next to the button you want (e.g. `Seven`).
4. Put that number in your recipe YAML.

## Keyboard shortcuts vs clicks

Prefer **hotkey** steps when macOS has a shortcut (`cmd+l` for Safari address bar). They survive UI layout changes better than `element_index`.

## When to use recipes vs `run-task`

| Use | When |
|-----|------|
| `brain recipe run` | Same steps every time; you can write the sequence |
| `brain run-task --smart` | New or vague goals; LLM plans steps |
| `brain run-task --fast` | Quick experiments (less reliable) |
