"""JSON/text presentation; services never print user output."""

import dataclasses
import json
import sys


class Presenter:
    def __init__(self, format="json", stdout=None, stderr=None):
        self.format = format
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr

    def write(self, result, event=None):
        if self.format == "json":
            data = dataclasses.asdict(result)
            if event is not None:
                data["event"] = event
            print(json.dumps(data, ensure_ascii=False), file=self.stdout, flush=True)
        else:
            if result.stdout:
                self.stdout.write(result.stdout)
            if result.stderr:
                self.stderr.write(result.stderr)
                if not result.stderr.endswith("\n"):
                    self.stderr.write("\n")
            if result.data is not None and event != "result":
                if "text" in result.data:
                    self.stdout.write(result.data["text"])
                else:
                    print(
                        json.dumps(result.data, ensure_ascii=False, indent=2),
                        file=self.stdout,
                    )
            self.stdout.flush()
            self.stderr.flush()

    def log_event(self, result):
        self.write(dataclasses.replace(result, operation="job.follow"), event="logs")
