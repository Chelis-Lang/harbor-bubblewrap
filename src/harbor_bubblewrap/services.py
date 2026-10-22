"""The running sandboxes of one Harbor environment, one for each service."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .compose import Healthcheck
from .holder import Holder
from .process import Completed, run
from .sandbox import Bind, Sandbox


@dataclass(frozen=True)
class Plan:
    """One service, ready to start."""

    name: str
    sandbox: Sandbox
    command: tuple[str, ...]
    workdir: str
    environment: Mapping[str, str]
    healthcheck: Healthcheck | None


class Runtime:
    """Start, use and stop the sandboxes of the services, in dependency order.

    Each service starts after the services before it in the order. A service
    with a healthcheck must become healthy before the next one starts. This is
    the result of ``docker compose up --wait`` with ``service_healthy`` order.
    """

    def __init__(self, plans: Sequence[Plan]) -> None:
        self.plans = {plan.name: plan for plan in plans}
        self.order = [plan.name for plan in plans]
        self.holders: dict[str, Holder] = {}

    async def start(self) -> None:
        for name in self.order:
            plan = self.plans[name]
            holder = Holder(plan.sandbox)
            self.holders[name] = holder
            await holder.start(
                plan.command, workdir=plan.workdir, environment=plan.environment
            )
            if plan.healthcheck is not None:
                await self._wait_until_healthy(plan, holder, plan.healthcheck)

    async def _wait_until_healthy(
        self, plan: Plan, holder: Holder, check: Healthcheck
    ) -> None:
        """Run the healthcheck with the Docker rules for interval, retries and start period."""
        started = time.monotonic()
        failures = 0
        while True:
            if not holder.running:
                raise RuntimeError(
                    f"service {plan.name} ended before it was healthy: {holder.log_tail()}"
                )
            try:
                result = await holder.exec(
                    check.argv,
                    uid=plan.sandbox.uid,
                    gid=plan.sandbox.gid,
                    cwd=plan.workdir,
                    environment=plan.environment,
                    timeout_sec=check.timeout,
                )
            except RuntimeError as error:
                detail = str(error)
            else:
                if result.return_code == 0:
                    return
                detail = (
                    f"exit code {result.return_code}: {result.stdout} {result.stderr}"
                )
            if time.monotonic() - started >= check.start_period:
                failures += 1
                if failures >= check.retries:
                    raise RuntimeError(
                        f"service {plan.name} is unhealthy after {failures} checks: {detail}"
                    )
            await asyncio.sleep(check.interval)

    async def exec(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: str,
        environment: Mapping[str, str],
        timeout_sec: float | None,
    ) -> Completed:
        plan = self.plans[name]
        holder = self.holders.get(name)
        if holder is None:
            raise RuntimeError(f"service {name} is not started")
        return await holder.exec(
            argv,
            uid=plan.sandbox.uid,
            gid=plan.sandbox.gid,
            cwd=cwd,
            environment=environment,
            timeout_sec=timeout_sec,
        )

    async def copy(
        self, name: str, script: str, args: Sequence[str], bind: Bind
    ) -> None:
        """Copy files with the tools of the sandbox, in a copy process of its own.

        A copy also works after the service stops, as ``docker cp`` does.
        """
        sandbox = self.plans[name].sandbox
        argv = sandbox.transfer_command(
            [str(sandbox.environment / "bin/bash"), "-c", script, "transfer", *args],
            [bind],
        )
        result = await run(argv, None, env={})
        if result.return_code != 0:
            raise RuntimeError(
                f"bubblewrap copy {list(args)} in service {name} failed with exit "
                f"code {result.return_code}: {result.stderr}"
            )

    async def stop_service(self, name: str) -> None:
        holder = self.holders.get(name)
        if holder is not None:
            await holder.stop()

    async def stop(self) -> None:
        for name in reversed(self.order):
            await self.stop_service(name)
