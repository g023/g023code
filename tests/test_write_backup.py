"""
WriteFile backups.

Every overwrite of an existing file should snapshot the previous version under
.g023/backups/ (mirroring the file tree, pruned to the newest few per file),
so a bad write — e.g. one based on a ReadFile summary — is one copy away.
"""

from __future__ import annotations

import json

from g023_code.tools.registry import ToolRegistry


async def test_overwrite_keeps_previous_version(isolated_project):
    target = isolated_project / "app.py"
    target.write_text("v1\n", encoding="utf-8")

    reg = ToolRegistry()
    res = json.loads(await reg._exec_write_file(str(target), "v2\n"))

    assert res["ok"]
    assert res["backup"].startswith(".g023/backups/")
    assert target.read_text(encoding="utf-8") == "v2\n"
    assert (isolated_project / res["backup"]).read_text(encoding="utf-8") == "v1\n"


async def test_backup_layout_mirrors_the_file_tree(isolated_project):
    target = isolated_project / "g023_code" / "orchestrator.py"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")

    reg = ToolRegistry()
    res = json.loads(await reg._exec_write_file(str(target), "new\n"))

    assert res["backup"].startswith(".g023/backups/g023_code/orchestrator.py/")


async def test_new_file_gets_no_backup(isolated_project):
    target = isolated_project / "brand_new.py"
    reg = ToolRegistry()
    res = json.loads(await reg._exec_write_file(str(target), "x\n"))

    assert res["ok"]
    assert "backup" not in res
    assert not (isolated_project / ".g023" / "backups").exists()


async def test_identical_write_is_a_noop(isolated_project):
    target = isolated_project / "app.py"
    target.write_text("same\n", encoding="utf-8")

    reg = ToolRegistry()
    res = json.loads(await reg._exec_write_file(str(target), "same\n"))

    assert res["unchanged"] is True
    assert "backup" not in res
    assert not (isolated_project / ".g023" / "backups").exists()


async def test_backups_are_pruned_to_the_configured_keep(isolated_project, monkeypatch):
    from g023_code import config

    monkeypatch.setattr(config.settings, "write_backup_keep", 2)
    target = isolated_project / "app.py"
    target.write_text("v0\n", encoding="utf-8")

    reg = ToolRegistry()
    for i in range(1, 6):
        await reg._exec_write_file(str(target), f"v{i}\n")

    backup_dir = isolated_project / ".g023" / "backups" / "app.py"
    versions = sorted(backup_dir.glob("*.bak"))
    assert len(versions) == 2


async def test_disabling_backups_skips_the_snapshot(isolated_project, monkeypatch):
    from g023_code import config

    monkeypatch.setattr(config.settings, "write_backup", False)
    target = isolated_project / "app.py"
    target.write_text("v1\n", encoding="utf-8")

    reg = ToolRegistry()
    res = json.loads(await reg._exec_write_file(str(target), "v2\n"))

    assert res["ok"]
    assert "backup" not in res
    assert not (isolated_project / ".g023" / "backups").exists()


async def test_files_inside_g023_are_never_backed_up(isolated_project):
    target = isolated_project / ".g023" / "scratch.txt"
    target.parent.mkdir(parents=True)
    target.write_text("a\n", encoding="utf-8")

    reg = ToolRegistry()
    res = json.loads(await reg._exec_write_file(str(target), "b\n"))

    assert res["ok"]
    assert "backup" not in res


async def test_write_outside_project_is_still_refused(isolated_project):
    outside = isolated_project.parent / "elsewhere.txt"
    outside.write_text("keep\n", encoding="utf-8")

    reg = ToolRegistry()
    res = json.loads(await reg._exec_write_file(str(outside), "nope\n"))

    assert "error" in res
    assert outside.read_text(encoding="utf-8") == "keep\n"
