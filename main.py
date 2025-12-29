import os
import requests
import math
from collections import defaultdict
import asyncio
from typing import Iterable
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from mavsdk import System
from mavsdk.server_utility import StatusTextType


LAST_VERSION_STATE = "version.state"
FLOAT_TOLERANCE = 6
REL_FLOAT_TOLERANCE = 10**-FLOAT_TOLERANCE
REAPPLICATION_TIMEOUT = 2
MAX_REAPPLICATIONS = 5


class InAir(Exception):
    pass


class NotArmable(Exception):
    pass


class Config(BaseSettings):
    param_file: str = Field(
        "https://caseparams.sandbox.aviant.no/reference.params",
        description="Param file. System path or URL",
    )

    connection: str = Field(
        "udpin://127.0.0.1:14540",
        description="Connection string (e.g. udpin://0.0.0.0:14540)",
    )
    skip_version_check: bool = False

    model_config = SettingsConfigDict(cli_parse_args=True, env_prefix="AVNT_")


class Component(BaseModel):
    vehicle_id: int
    component_id: int

    def __hash__(self):
        return hash((self.vehicle_id, self.component_id))


class Param[T](BaseModel):
    name: str
    value: T
    type: int | None = None


async def main(args: Config):
    version, params = read_param_file(args.param_file)
    if not args.skip_version_check and not version_is_new(version):
        print(f"Version {version} didn't change")
        return
    params = {k: _skip_settings(v) for k, v in params.items()}
    for component, component_params in params.items():
        try:
            await process_component_or_revert(
                component, component_params, args.connection
            )
        except NotArmable:
            print("NotArmable. Reverted")
            return
        except InAir:
            print("InAir. Skipped")
            return
    if not args.skip_version_check:
        save_version(version)
    print(f"Updated to version {version}.")


async def process_component_or_revert(
    component: Component, component_params: list[Param], addr: str
):
    drone = System(sysid=component.vehicle_id, compid=component.component_id)
    await drone.connect(system_address=addr)
    current_params = list((await read_param_spec(drone)).values())
    await process_component_until_complete(drone, component_params)
    try:
        await check_is_armable(drone)
    except NotArmable as e:
        # revert
        print("Reverting")
        await process_component_until_complete(drone, current_params)
        raise e


async def process_component_until_complete(
    drone: System, component_params: list[Param]
):
    # Re-apply parameters until no changes neccessary
    for _ in range(MAX_REAPPLICATIONS):
        changed_params = await _process_one_time(drone, component_params)
        if not changed_params:
            return
        print(f"Wait {REAPPLICATION_TIMEOUT} sec before reapplying")
        await asyncio.sleep(REAPPLICATION_TIMEOUT)


async def _process_one_time(drone: System, new_params: list[Param]):
    await check_in_air(drone)
    current_params = await read_param_spec(drone)
    old_values, changed_params = find_changed_params(current_params, new_params)
    if not changed_params:
        # nothing to update for the component
        return changed_params
    await notify_changed_params(drone, old_values, changed_params)
    await set_params(drone, changed_params)
    return changed_params


def find_changed_params(
    current_params: dict[str, Param], new_params: list[Param]
) -> tuple[list[Param], list[Param]]:
    old_values = []
    changed_params = []
    for p in new_params:
        if (current_param := current_params.get(p.name)) is None:
            # skipping, param not in spec
            continue
        validated_param = current_param.model_validate(p.model_dump())
        if _params_equal(validated_param, current_param):
            # value didn't change
            continue
        old_values.append(current_param)
        changed_params.append(validated_param)
    return (old_values, changed_params)


def _params_equal(a: Param, b: Param):
    if a.value == b.value:
        return True
    if isinstance(a, Param[float]) or isinstance(b, Param[float]):
        return math.isclose(a.value, b.value, rel_tol=REL_FLOAT_TOLERANCE)


def read_param_file(path: str):
    if path.startswith("https://"):
        lines = read_file_from_url(path)
    else:
        lines = read_file_from_fs(path)
    return parse_param_file(lines)


def read_file_from_url(url: str):
    response = requests.get(
        url, headers={"Content-Type": "text/plain", "charset": "utf-8"}
    )
    return response.text.split("\n")


def read_file_from_fs(path: str):
    with open(path, "r", encoding="utf-8") as param_file:
        return param_file.read().split("\n")


def parse_param_file(lines: Iterable[str]) -> tuple[str, dict[Component, list[Param]]]:
    res = defaultdict(list)
    version = ""
    for line in lines:
        if line.startswith("#"):
            if "Version" in line:
                version = line.strip().rsplit(maxsplit=1)[-1]
            continue
        values = line.strip().split("\t")
        component = Component.model_validate(
            {
                "vehicle_id": values[0],
                "component_id": values[1],
            }
        )
        data = Param.model_validate(
            {
                "name": values[2],
                "value": values[3],
                "type": values[4],
            }
        )
        res[component].append(data)
    return version, res


async def read_param_spec(drone: System) -> dict[str, Param]:
    params = await drone.param.get_all_params()
    res = {}
    for p in params.int_params:
        res[p.name] = Param[int](name=p.name, value=p.value)
    for p in params.float_params:
        res[p.name] = Param[float](name=p.name, value=round(p.value, FLOAT_TOLERANCE))
    for p in params.custom_params:
        res[p.name] = Param[str](name=p.name, value=p.value)
    return res


async def set_params(drone: System, params: list[Param]):
    for param in params:
        if isinstance(param, Param[int]):
            await drone.param.set_param_int(param.name, param.value)
        elif isinstance(param, Param[float]):
            await drone.param.set_param_float(param.name, param.value)
        elif isinstance(param, Param[str]):
            await drone.param.set_param_custom(param.name, param.value)
        else:
            raise ValueError("Unknown param type")
        print(f"Set param `{param.name}` to value: `{param.value}`")


async def check_in_air(drone: System):
    async for is_in_air in drone.telemetry.in_air():
        if not is_in_air:
            return
    raise InAir()


async def check_is_armable(drone: System):
    is_armable = False
    async for health in drone.telemetry.health():
        is_armable = health.is_armable
        break
    if not is_armable:
        raise NotArmable()


async def notify_changed_params(
    drone: System, old_params: list[Param], changed_params: list[Param]
):
    if not changed_params:
        return
    if len(changed_params) == 1:
        old_param = old_params[0]
        new_param = changed_params[0]
        await drone.server_utility.send_status_text(
            StatusTextType.ERROR,
            f"{new_param.name}: chng {old_param.value} -> {new_param.value}",
        )
        return
    await drone.server_utility.send_status_text(
        StatusTextType.ERROR, f"updating {len(changed_params)} params"
    )
    if len(changed_params) <= 5:
        for old_param, new_param in zip(old_params, changed_params):
            await drone.server_utility.send_status_text(
                StatusTextType.INFO,
                f"{new_param.name}: chng {old_param.value} -> {new_param.value}",
            )
            await asyncio.sleep(0.1)


async def send_status_text(drone: System, severity: StatusTextType, msg: str):
    await drone.server_utility.send_status_text(severity, msg)


def version_is_new(version):
    if not os.path.exists(LAST_VERSION_STATE):
        # we are not aware of any previous versions. consider this to be new
        return True
    with open(LAST_VERSION_STATE, "r", encoding="utf-8") as fin:
        last_version = fin.read().strip()
    return version != last_version


def save_version(version):
    with open(LAST_VERSION_STATE, "w", encoding="utf-8") as fout:
        fout.write(f"{version}\n")


def _skip_settings(params: list[Param]) -> list[Param]:
    # for demo, px4 sitl fails with some settings
    res = []
    for p in params:
        if p.name.startswith("CAL_MAG"):
            continue
        if p.name in ["LNDMC_Z_VEL_MAX", "MPC_Z_V_AUTO_UP", "COM_PARACHUTE"]:
            continue
        if p.name.startswith("SYS_HAS_"):
            continue
        if p.name.startswith("SENS_"):
            if p.name.endswith("_AUTOCAL") or p.name.endswith("_MODE"):
                continue
        res.append(p)
    return res


if __name__ == "__main__":
    asyncio.run(main(Config()))  # ty: ignore[missing-argument]
