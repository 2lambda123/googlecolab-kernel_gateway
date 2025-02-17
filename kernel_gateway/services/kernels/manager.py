# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
"""Kernel manager that optionally seeds kernel memory."""

import os
from typing import List, Optional

from jupyter_client.ioloop import AsyncIOLoopKernelManager
from jupyter_server.services.kernels.kernelmanager import AsyncMappingKernelManager
from traitlets import default


class SeedingMappingKernelManager(AsyncMappingKernelManager):
    """Extends the server's kernel manager to optionally execute the contents
    of a notebook on a kernel when it starts.
    """

    _seed_source: Optional[List]
    _seed_kernelspec: Optional[str]

    @default("root_dir")
    def _default_root_dir(self):
        return os.getcwd()

    def _kernel_manager_class_default(self):
        return "kernel_gateway.services.kernels.manager.KernelGatewayIOLoopKernelManager"

    @property
    def seed_kernelspec(self) -> Optional[str]:
        """Gets the kernel spec name for run the seed notebook.

        Prefers the spec name forced by configuration over the spec in the
        seed notebook itself.

        Returns
        -------
        str
            Name of the notebook kernelspec or None if no seed notebook exists
        """
        if hasattr(self, "_seed_kernelspec"):
            return self._seed_kernelspec

        if self.parent.seed_notebook:
            if self.parent.force_kernel_name:
                self._seed_kernelspec = self.parent.force_kernel_name
            else:
                self._seed_kernelspec = self.parent.seed_notebook["metadata"]["kernelspec"]["name"]
        else:
            self._seed_kernelspec = None

        return self._seed_kernelspec

    @property
    def seed_source(self) -> Optional[List]:
        """Gets the source of the seed notebook in cell order.

        Returns
        -------
        list
            Notebook code cell contents or None if no seed notebook exists
        """
        if hasattr(self, "_seed_source"):
            return self._seed_source

        if self.parent.seed_notebook:
            self._seed_source = [
                cell["source"]
                for cell in self.parent.seed_notebook.cells
                if cell["cell_type"] == "code"
            ]
        else:
            self._seed_source = None

        return self._seed_source

    async def start_seeded_kernel(self, *args, **kwargs):
        """Start a kernel using the language specified in the seed notebook.

        Run synchronously so that any exceptions thrown while seed rise up
        to the caller.
        """
        kwargs["kernel_name"] = self.seed_kernelspec
        await self.start_kernel(*args, **kwargs)

    async def start_kernel(self, *args, **kwargs):
        """Starts a kernel and then executes a list of code cells on it if a
        seed notebook exists.
        """
        if self.parent.force_kernel_name:
            kwargs["kernel_name"] = self.parent.force_kernel_name
        kernel_id = await super().start_kernel(*args, **kwargs)

        if kernel_id and self.seed_source is not None:
            # Only run source if the kernel spec matches the notebook kernel spec
            kernel = self.get_kernel(kernel_id)
            if kernel.kernel_name == self.seed_kernelspec:
                # Create a client to talk to the kernel
                client = kernel.client()
                # Clone client session. Workaround duplicate signatures due to shared digest_history
                # This shouldn't be necessary after upstream fixes.
                client.session = type(client.session)(
                    config=kernel.session.config,
                    key=kernel.session.key,
                )
                # Only start channels and wait for ready in HTTP mode
                client.start_channels()
                await client.wait_for_ready()
                for code in self.seed_source:
                    # Check with the personality whether it wants the cell
                    # executed
                    if self.parent.personality.should_seed_cell(code):
                        client.execute(code)
                        msg_type = "kernel_info_reply"
                        while msg_type == "kernel_info_reply":
                            msg = await client.get_shell_msg()
                            msg_type = msg["msg_type"]
                            if msg["content"]["status"] != "ok":
                                # Shutdown the channels to remove any lingering ZMQ messages
                                client.stop_channels()
                                # Shutdown the kernel
                                await self.shutdown_kernel(kernel_id)
                                raise RuntimeError("Error seeding kernel memory", msg["content"])
                # Shutdown the channels to remove any lingering ZMQ messages
                client.stop_channels()
        return kernel_id


class KernelGatewayIOLoopKernelManager(AsyncIOLoopKernelManager):
    """Extends the IOLoopKernelManager used by the SeedingMappingKernelManager.

    Sets the environment variable 'KERNEL_GATEWAY' to '1' to indicate that the
    kernel is executing within a Jupyter Kernel Gateway instance. Removes the
    KG_AUTH_TOKEN from the environment variables passed to the kernel when it
    starts.
    """

    async def _async_launch_kernel(self, kernel_cmd, **kw):
        # TODO - should probably figure out a better place to deal with this
        env = kw["env"]
        env["KERNEL_GATEWAY"] = "1"
        if "KG_AUTH_TOKEN" in env:
            del env["KG_AUTH_TOKEN"]
        return await super()._async_launch_kernel(kernel_cmd, **kw)
