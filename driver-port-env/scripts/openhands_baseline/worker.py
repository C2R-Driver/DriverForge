"""Run the official OpenHands CLI agent once inside the experiment namespace."""

import json
import os
import sys
from pathlib import Path


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def main():
    from openhands.sdk import LLM, Conversation
    from openhands.tools.preset.default import get_default_agent
    from pydantic import SecretStr

    config = json.loads(Path(sys.argv[1]).read_text())
    home = Path(os.environ["HOME"])
    options = config["llm"] | {
        "log_completions": True,
        "log_completions_folder": str(home / "llm-responses"),
    }
    llm = LLM(**options, api_key=SecretStr(os.environ["C2R_OPENHANDS_API_KEY"]))
    # Official preset: terminal, file editor, task tracker; no browser or delegation.
    agent = get_default_agent(llm, cli_mode=True)
    conversation = None
    state = {"state": "RUNNING", "error": None}

    def snapshot():
        metrics = (
            conversation.state.stats.usage_to_metrics
            if conversation is not None
            else {"agent": llm.metrics, "condenser": agent.condenser.llm.metrics}
        )
        write(
            home / "metrics.json",
            {slot: value.model_dump(mode="json") for slot, value in metrics.items()},
        )

    def on_event(event):
        with (home / "events.jsonl").open("a") as stream:
            stream.write(event.model_dump_json() + "\n")
        snapshot()

    try:
        conversation = Conversation(
            agent=agent,
            workspace=config["workspace"],
            persistence_dir=home / "conversations",
            callbacks=[on_event],
            # SDK requires an integer. This is an effectively unbounded guard,
            # not a translation budget; retain SDK-native stuck detection.
            max_iteration_per_run=sys.maxsize,
        )
        if config.get("preflight_command"):
            from openhands.tools.terminal import TerminalAction

            observation = conversation.execute_tool(
                "terminal",
                TerminalAction(command=config["preflight_command"], timeout=60),
            )
            write(home / "preflight.json", observation.model_dump(mode="json"))
        else:
            conversation.send_message(Path(config["prompt"]).read_text())
            conversation.run()
        state.update(
            state="RETURNED",
            execution_status=str(conversation.state.execution_status),
        )
    except Exception as error:
        state.update(state="MODEL_FAILED", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        snapshot()
        write(home / "worker-result.json", state)
        if conversation is not None:
            conversation.close()


if __name__ == "__main__":
    main()
