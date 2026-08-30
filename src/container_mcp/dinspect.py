from __future__ import annotations

import asyncio

from .dexec import terminate_process


TIMEOUT_SEC = 10

_FORMAT = """
name={{.Name}}
image={{.Config.Image}}
status={{.State.Status}}
user={{.Config.User}}
cwd={{.Config.WorkingDir}}
cmd={{json .Config.Cmd}}

runtime={{.HostConfig.Runtime}}
privileged={{.HostConfig.Privileged}}
caps_add={{json .HostConfig.CapAdd}}
caps_drop={{json .HostConfig.CapDrop}}
security={{json .HostConfig.SecurityOpt}}
apparmor={{json .AppArmorProfile}}
readonly_rootfs={{.HostConfig.ReadonlyRootfs}}

network={{.HostConfig.NetworkMode}}
pid_ns={{.HostConfig.PidMode}}
ipc_ns={{.HostConfig.IpcMode}}
uts_ns={{.HostConfig.UTSMode}}
user_ns={{.HostConfig.UsernsMode}}
cgroup_ns={{.HostConfig.CgroupnsMode}}

devices={{json .HostConfig.Devices}}
device_rules={{json .HostConfig.DeviceCgroupRules}}
device_requests={{json .HostConfig.DeviceRequests}}
mounts={{json .Mounts}}

ports={{json (index .Config "ExposedPorts")}}
port_bindings={{json .HostConfig.PortBindings}}
publish_all_ports={{.HostConfig.PublishAllPorts}}
extra_hosts={{json .HostConfig.ExtraHosts}}
dns={{json .HostConfig.Dns}}

pids_limit={{json .HostConfig.PidsLimit}}
oom_kill_disable={{json .HostConfig.OomKillDisable}}
ulimits={{json .HostConfig.Ulimits}}
""".strip()


async def dinspect(container: str) -> str:
    from . import server as runtime

    selected_container = runtime.resolve_container(container)
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            selected_container,
            "--format",
            _FORMAT,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RuntimeError(f"Could not inspect container {selected_container}: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_SEC)
    except asyncio.CancelledError:
        if process.returncode is None:
            await terminate_process(process)
        raise
    except asyncio.TimeoutError as exc:
        if process.returncode is None:
            await terminate_process(process)
        raise RuntimeError(
            f"Inspecting container {selected_container} timed out after {TIMEOUT_SEC}s"
        ) from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Could not inspect container {selected_container}: "
            f"{detail or f'docker inspect exited with code {process.returncode}'}"
        )

    metadata = stdout.decode("utf-8", errors="replace").strip()
    status = next(
        (
            line.removeprefix("status=")
            for line in metadata.splitlines()
            if line.startswith("status=")
        ),
        "",
    )
    if status != "running":
        raise ValueError(
            f"container {selected_container!r} is not running (status: {status or 'unknown'})"
        )
    return metadata
