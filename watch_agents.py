import json
import time
import os

from documentor_agent import read_log, LOG_FILE
last_size = 0
last_count = 0

print("NosPopuli Agent Monitor")
print("=" * 50)
print("Watching for agent activity...\n")

AGENT_COLORS = {
    "router":       "\033[94m",   # Blue
    "search":       "\033[96m",   # Cyan
    "bill_fetcher": "\033[93m",   # Yellow
    "translator":   "\033[92m",   # Green
    "historian":    "\033[95m",   # Magenta
    "vote_parser":  "\033[91m",   # Red
    "vote_fetcher": "\033[91m",   # Red
    "vote_mapper":  "\033[91m",   # Red
    "documentor":   "\033[90m",   # Grey
    "member_search":"\033[93m",   # Yellow
    "api":          "\033[97m",   # White
}
RESET = "\033[0m"

while True:
    try:
        if not os.path.exists(LOG_FILE):
            time.sleep(0.5)
            continue

        new_entries, _offset = read_log(last_count)

        if new_entries:
            for entry in new_entries:
                agent = entry.get("agent", "unknown")
                action = entry.get("action", "")
                timestamp = entry.get("timestamp", "")[:19]
                input_data = entry.get("input", {})
                output_data = entry.get("output", {})

                color = AGENT_COLORS.get(agent, "\033[37m")

                print(f"{color}[{timestamp}] {agent.upper()} → {action}{RESET}")

                # Print meaningful input/output
                if input_data:
                    for k, v in input_data.items():
                        if v and str(v) != "{}":
                            print(f"  IN  {k}: {str(v)[:80]}")

                if output_data:
                    for k, v in output_data.items():
                        if v and str(v) != "{}":
                            print(f"  OUT {k}: {str(v)[:80]}")

                print()

            last_count = len(log)

    except (json.JSONDecodeError, Exception):
        pass

    time.sleep(0.3)