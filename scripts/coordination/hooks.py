"""Optional native-hook feedback helper; not installed or trusted automatically.

Hooks supplement the supervisor. They are not the permission/authority boundary.
This conservative profile denies every tool call, for self-contained prompts.
"""
import json
import sys


def respond(event):
    name = event.get("hook_event_name")
    if name == "PreToolUse":
        return {"hookSpecificOutput": {"hookEventName": name,
                "permissionDecision": "deny",
                "permissionDecisionReason": "This packet uses structured patch output; tool execution is disabled."}}
    if name == "Stop":
        # Do not turn completion into an unlimited continuation loop. The external
        # supervisor validates the response and requests bounded corrections.
        return {"continue": True}
    if name == "SessionStart":
        return {"hookSpecificOutput": {"hookEventName": name,
                "additionalContext": "Return the structured result. The external supervisor owns checks, patches and completion."}}
    return {}


def main():
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("object required")
        print(json.dumps(respond(event)))
        return 0
    except (ValueError, TypeError):
        print("Invalid hook input", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
