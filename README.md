# PX4rger

Live update of px4 parameters.

## Features:

* supports importing a version from file or from a remote url
* if the version defined in the file's header didn't change since the last successful update - skip
* do not send update requests for parameters that didn't change. check first.
* re-apply parameters until no changes neccessary. some parameters lead to new parameters appearing in the spec.
* revert if not ready to fly after applying


## How to run

You will need `uv` to setup environment:

```
uv sync
```

A virtual env folder was now created: `.venv`.

While developing, you can run as follows:

```
uv run main.py --skip_version_check True --param_file test/reference.params --loglevel DEBUG
```

In production, systemd timer is recommended for process management.
Find `px4rger.service` and `px4rger.timer` in [deploy/systemd](/deploy/systemd/),
put them into `/etc/systemd/system/`.

Adjust paths in the service files (absolute paths are typically required for systemd). Then schedule for execution:

```
systemctl daemon-reload
systemctl start px4rger.timer
```

Follow the logs with:

```
journalctl -f -u px4rger.service
```
