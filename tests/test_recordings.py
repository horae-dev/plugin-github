# SPDX-License-Identifier: Apache-2.0
"""Replay every recorded call (`tests/recordings/*.json`) through MCP, as the core's host
makes it. `HORAE_SDK_RECORD=1 uv run pytest` rewrites the expectations: review the diff."""

from pathlib import Path

from horae_sdk import Plugin
from horae_sdk.testing import replay_all

from conftest import TOKEN

RECORDINGS = Path(__file__).parent / "recordings"


def test_recordings(plugin: Plugin) -> None:
    replayed = replay_all(plugin, RECORDINGS)
    assert len(replayed) >= 15, "the recordings directory looks empty"


def test_no_recording_or_fixture_holds_the_token() -> None:
    for path in [*RECORDINGS.glob("*.json"), *(RECORDINGS.parent / "fixtures").glob("*")]:
        assert TOKEN not in path.read_text("utf-8"), path
