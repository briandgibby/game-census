#!/usr/bin/env python3
"""Standard-library bootstrap; all application work runs in the pinned container."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / "build/toolchain.lock.json").read_text(encoding="utf-8"))


class DriverError(Exception):
    pass


def run(command, *, env=None, capture=False, safe_error=False):
    result = subprocess.run(command, cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace",
                            stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)
    if result.returncode:
        # Only the application's authored JSON diagnostic is safe to surface.
        # Never echo arbitrary Docker stdout/stderr, which can include secrets.
        if capture and safe_error:
            try:
                diagnostic = json.loads(result.stderr)
            except (ValueError, TypeError):
                diagnostic = None
            if isinstance(diagnostic, dict) and diagnostic.get("status") == "failed" and isinstance(diagnostic.get("error"), str):
                raise DriverError(diagnostic["error"])
        raise DriverError(f"{command[0]} operation failed (exit {result.returncode}). Check Docker status, configuration, port availability and the preceding diagnostic.")
    return result.stdout if capture else None


def doctor():
    if not shutil.which("docker"):
        raise DriverError("Docker is missing. Install Docker Desktop with Compose, start its Linux engine, then retry.")
    engine = run(["docker", "info", "--format", "{{.OSType}}"], capture=True).strip()
    if engine != "linux":
        raise DriverError("Docker must be running Linux containers. Switch its engine and retry.")
    compose = run(["docker", "compose", "version", "--short"], capture=True).strip()
    print(json.dumps({"docker_engine": engine, "compose_version": compose, "python_image": PINS["python_image"], "postgres_image": PINS["postgres_image"]}), flush=True)


class Project:
    def __init__(self, instance):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", instance):
            raise DriverError("instance must be 1–31 lowercase letters, digits or hyphens, starting with a letter.")
        self.name = "game-census-" + instance
        self.home = ROOT / "data" / "instances" / instance
        self.config = self.home / "config"
        self.state = self.home / "state"
        self.image = self.name + ":local"

    def build(self):
        run(["docker", "build", "--build-arg", "PYTHON_IMAGE=" + PINS["python_image"],
             "--build-arg", "POSTGRES_IMAGE=" + PINS["postgres_image"], "--tag", self.image, "."])

    def config_init(self, app_ids=None, port=8000):
        self.config.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        owner = ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
        command = ["docker", "run", "--rm", *owner, "--mount", f"type=bind,source={self.config},target=/config", self.image,
                   "--config", "/config/local.json", "config", "init", "--port", str(port)]
        for app_id in app_ids or []:
            command += ["--app-id", str(app_id)]
        run(command)

    def environment(self):
        target = self.config / "local.json"
        if not target.is_file():
            raise DriverError("Instance configuration is missing. Run quickstart --once first.")
        # Validate before interpreting untrusted configuration or placing values in Compose.
        validated = json.loads(run(["docker", "run", "--rm", "--mount", f"type=bind,source={self.config},target=/config,readonly", self.image,
             "--config", "/config/local.json", "config", "describe"], capture=True, safe_error=True))
        settings = json.loads(target.read_text(encoding="utf-8"))
        if validated["web"]["bind"] != "0.0.0.0":
            raise DriverError("web.bind must be 0.0.0.0 inside the local Docker container. The published host address remains 127.0.0.1.")
        url = urlsplit(settings["storage"]["database_url"])
        if url.hostname != "db" or url.port != 5432:
            raise DriverError("The local Docker wrapper requires storage.database_url host db and port 5432. Use the game-census CLI directly for an external PostgreSQL server.")
        env = os.environ.copy()
        env.update(GC_POSTGRES_IMAGE=PINS["postgres_image"], GC_APP_IMAGE=self.image,
                   GC_DB_USER=unquote(url.username), GC_DB_PASSWORD=unquote(url.password), GC_DB_NAME=unquote(url.path[1:]),
                   GC_CONFIG_DIR=str(self.config), GC_STATE_DIR=str(self.state), GC_WEB_PORT=str(validated["web"]["port"]))
        return env

    def compose(self, args, *, capture=False):
        return run(["docker", "compose", "--project-name", self.name, "--file", "compose.yaml", *args], env=self.environment(), capture=capture)

    def app(self, args):
        self.compose(["run", "--rm", "--no-deps", "web", *args])

    def quickstart(self, args):
        collect = args.command == "quickstart"
        print(json.dumps({"operation": args.command, "instance": self.name, "touches": [str(self.home), self.name + "_postgres", "local Docker images and containers"],
                          "app_ids": args.app_id or [570], "collection": "one manual run" if collect else "none; zero Steam requests", "scheduler": "disabled"}), flush=True)
        doctor()
        self.build()
        self.config_init(args.app_id, args.port)
        self.compose(["up", "--detach", "--wait", "db"])
        self.app(["initialize"])
        if collect:
            self.app(["collect", "--once"])
        self.compose(["up", "--detach", "--wait", "web"])
        port = self.environment()["GC_WEB_PORT"]
        print(json.dumps({"status": "ready", "url": f"http://127.0.0.1:{port}", "configuration": str(self.config / "local.json"),
                          "next_collection": f"python tools/dev.py --instance {args.instance} app collect --once", "scheduler": "disabled"}), flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance", default="local", help="Isolated local instance name")
    sub = p.add_subparsers(dest="command", required=True)
    quick = sub.add_parser("quickstart", help="Build, initialize, collect once and serve a bounded local cohort")
    quick.add_argument("--once", action="store_true", required=True)
    quick.add_argument("--app-id", type=int, action="append")
    quick.add_argument("--port", type=int, default=8000)
    setup = sub.add_parser("setup", help="Build, generate configuration, initialize and serve without contacting Steam")
    setup.add_argument("--app-id", type=int, action="append")
    setup.add_argument("--port", type=int, default=8000)
    sub.add_parser("doctor")
    sub.add_parser("build")
    sub.add_parser("start", help="Start existing database and website without collecting")
    sub.add_parser("stop", help="Stop instance containers; retain database and configuration")
    sub.add_parser("status")
    app = sub.add_parser("app", help="Run an application command in the pinned container")
    app.add_argument("arguments", nargs=argparse.REMAINDER)
    test = sub.add_parser("test", help="Run fixture and database tests against this instance")
    test.add_argument("--capacity", action="store_true", help="Include the bounded 25-app, 90-day synthetic capacity workload")
    test.add_argument("paths", nargs="*", default=["tests"])
    args = p.parse_args(argv)
    try:
        project = Project(args.instance)
        if args.command in ("quickstart", "setup"):
            project.quickstart(args)
        elif args.command == "doctor":
            doctor()
        elif args.command == "build":
            project.build()
        elif args.command == "app":
            project.app(args.arguments)
        elif args.command == "start":
            project.compose(["up", "--detach", "--wait", "db"])
            project.app(["initialize"])
            project.compose(["up", "--detach", "--wait", "web"])
        elif args.command == "stop":
            project.compose(["stop"])
        elif args.command == "status":
            project.compose(["ps"])
            project.app(["report"])
        elif args.command == "test":
            extra = ["--env", "GAME_CENSUS_CAPACITY=1"] if args.capacity else []
            if args.capacity:
                print(json.dumps({"operation": "synthetic_capacity_test", "maximum_apps": 25, "history_days": 90,
                                  "maximum_samples": 648000, "destination": "new scratch schema", "steam_requests": 0}), flush=True)
            project.compose(["run", "--rm", "--no-deps", *extra, "--entrypoint", "python", "web", "-m", "pytest",
                             *(["-s"] if args.capacity else []), *args.paths])
        return 0
    except (DriverError, OSError, ValueError):
        # DriverError is authored safe text; never stringify JSON/OS exceptions.
        error = sys.exception()
        message = str(error) if isinstance(error, DriverError) else "Cannot run local setup. Check configuration syntax, Docker, file permissions and available disk space."
        print(json.dumps({"status": "failed", "error": message}), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
