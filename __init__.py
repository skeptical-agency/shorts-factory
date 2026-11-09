"""Shorts Factory — the 9:16 short-video pipeline as executable tools.

Registration and wiring. No credentials, no accounts, no cloud: everything
runs locally through ffmpeg + Pillow. Zero-credential by design (Rule 11
trivially satisfied — there is nothing to hold).
"""

from . import schemas, tools


def register(ctx):
    tools.bind_ctx(ctx)

    for name, schema in schemas.ALL.items():
        ctx.register_tool(
            name=name,
            toolset="shorts-factory",
            schema=schema,
            handler=tools.HANDLERS[name],
        )
